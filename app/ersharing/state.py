"""Per-destination progress kept in Redis (spec §7.1, §8)."""
import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

import stamina

from app.services.retry_policies import REDIS_RETRY
from app.services.state import IntegrationStateManager, _skip_on_ephemeral_run

EVENTS_ACTION = "sync_events"
OBSERVATIONS_ACTION = "sync_observations"


class Marker(BaseModel):
    since: datetime                          # resume reading here
    advanced_at: datetime                    # when `since` last moved (stall detection)
    created_since: Optional[datetime] = None  # observations: high-water mark of created_at
    # observations: (run time, read start) of each short read since the last full re-read
    short_reads: List[List[datetime]] = Field(default_factory=list)


class NoteRecord(BaseModel):
    dest_id: str
    digest: str


class EventRecord(BaseModel):
    dest_event_id: str
    updated_at: datetime
    notes: Dict[str, NoteRecord] = Field(default_factory=dict)  # source note id -> copy
    files: Dict[str, str] = Field(default_factory=dict)         # source file id -> dest file id ("" = not copied)
    incidents: List[str] = Field(default_factory=list)          # incident ids linked at the destination
    members: List[str] = Field(default_factory=list)            # incidents: source event ids held, as last checked
    pending: bool = False  # notes, files or incident links still to finish after the event itself was written
    written_at: Optional[datetime] = None  # updated_at of the source version last written to the copy
    dest_written_at: Optional[datetime] = None  # the copy's updated_at right after that write


_DELETE_IF_EQUAL_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class LeaseStateManager(IntegrationStateManager):
    """The template's state manager plus what a run lease needs: set a key to a chosen
    value only if absent, and delete it only while it still holds that value. Kept here
    rather than in app/services so the template file stays as it is."""

    def _key(self, integration_id: str, action_id: str, source_id: str) -> str:
        return f"integration_state.{integration_id}.{action_id}.{source_id}"

    async def set_if_absent(self, integration_id: str, action_id: str, *, ttl_seconds: int,
                            source_id: str = "no-source", value: str = "1") -> bool:
        if _skip_on_ephemeral_run("set_if_absent", integration_id, action_id):
            return False
        async for attempt in stamina.retry_context(**REDIS_RETRY):
            with attempt:
                was_set = await self.db_client.set(self._key(integration_id, action_id, source_id), value,
                                                   ex=ttl_seconds, nx=True)
        return bool(was_set)

    async def delete_if_equal(self, integration_id: str, action_id: str, value: str,
                              source_id: str = "no-source") -> bool:
        """Atomically delete the key only while it still holds `value` (as stored)."""
        if _skip_on_ephemeral_run("delete_if_equal", integration_id, action_id):
            return False
        async for attempt in stamina.retry_context(**REDIS_RETRY):
            with attempt:
                deleted = await self.db_client.eval(_DELETE_IF_EQUAL_SCRIPT, 1,
                                                    self._key(integration_id, action_id, source_id), value)
        return bool(deleted)


def _dump(model: BaseModel) -> dict:
    return json.loads(model.json())


class ShareState:
    def __init__(self, integration_id: str, manager=None):
        self.integration_id = integration_id
        self._manager = manager or LeaseStateManager()
        self._lease_tokens: Dict[str, str] = {}

    async def get_marker(self, action_id: str, key: str) -> Optional[Marker]:
        data = await self._manager.get_state(self.integration_id, action_id, source_id=key)
        return Marker.parse_obj(data) if data else None

    async def set_marker(self, action_id: str, key: str, marker: Marker):
        await self._manager.set_state(self.integration_id, action_id, _dump(marker), source_id=key)

    async def delete_marker(self, action_id: str, key: str):
        await self._manager.delete_state(self.integration_id, action_id, source_id=key)

    async def get_cursor(self, action_id: str, key: str) -> Optional[str]:
        data = await self._manager.get_state(self.integration_id, action_id, source_id=key)
        return data.get("after") if data else None

    async def set_cursor(self, action_id: str, key: str, after: str):
        await self._manager.set_state(self.integration_id, action_id, {"after": after}, source_id=key)

    async def get_event_record(self, destination_id: str, src_event_id: str) -> Optional[EventRecord]:
        data = await self._manager.get_state(self.integration_id, EVENTS_ACTION,
                                             source_id=f"{destination_id}:event:{src_event_id}")
        return EventRecord.parse_obj(data) if data else None

    async def set_event_record(self, destination_id: str, src_event_id: str, record: EventRecord):
        await self._manager.set_state(self.integration_id, EVENTS_ACTION, _dump(record),
                                      source_id=f"{destination_id}:event:{src_event_id}")

    async def delete_event_record(self, destination_id: str, src_event_id: str):
        await self._manager.delete_state(self.integration_id, EVENTS_ACTION,
                                         source_id=f"{destination_id}:event:{src_event_id}")

    async def get_waiting_types(self, destination_id: str) -> Dict[str, datetime]:
        """Source event types whose events were skipped because the type couldn't be set up
        at this destination, each with the oldest skipped event's updated_at."""
        data = await self._manager.get_state(self.integration_id, EVENTS_ACTION,
                                             source_id=f"{destination_id}:waiting-types")
        return {value: datetime.fromisoformat(since) for value, since in (data or {}).items()}

    async def set_waiting_types(self, destination_id: str, waiting: Dict[str, datetime]):
        key = f"{destination_id}:waiting-types"
        if waiting:
            await self._manager.set_state(self.integration_id, EVENTS_ACTION,
                                          {value: since.isoformat() for value, since in waiting.items()},
                                          source_id=key)
        else:
            await self._manager.delete_state(self.integration_id, EVENTS_ACTION, source_id=key)

    async def get_pending_events(self, destination_id: str) -> List[str]:
        """Source events whose copy still misses notes, files or incident links."""
        data = await self._manager.get_state(self.integration_id, EVENTS_ACTION,
                                             source_id=f"{destination_id}:pending-events")
        return list((data or {}).get("ids") or [])

    async def set_pending_events(self, destination_id: str, event_ids: List[str]):
        key = f"{destination_id}:pending-events"
        if event_ids:
            await self._manager.set_state(self.integration_id, EVENTS_ACTION, {"ids": sorted(event_ids)}, source_id=key)
        else:
            await self._manager.delete_state(self.integration_id, EVENTS_ACTION, source_id=key)

    async def get_rejected_events(self, destination_id: str) -> Dict[str, datetime]:
        """Source events the destination turned down or kept failing on, each with when that
        first happened, so they are tried again later."""
        data = await self._manager.get_state(self.integration_id, EVENTS_ACTION,
                                             source_id=f"{destination_id}:rejected-events")
        return {event_id: datetime.fromisoformat(since) for event_id, since in (data or {}).items()}

    async def set_rejected_events(self, destination_id: str, rejected: Dict[str, datetime]):
        key = f"{destination_id}:rejected-events"
        if rejected:
            await self._manager.set_state(self.integration_id, EVENTS_ACTION,
                                          {event_id: since.isoformat() for event_id, since in rejected.items()},
                                          source_id=key)
        else:
            await self._manager.delete_state(self.integration_id, EVENTS_ACTION, source_id=key)

    async def get_halt(self, destination_id: str) -> dict:
        """The event the last halted run for this destination stopped on, and how many runs in a row did."""
        return await self._manager.get_state(self.integration_id, EVENTS_ACTION,
                                             source_id=f"{destination_id}:halt") or {}

    async def set_halt(self, destination_id: str, halt: dict):
        key = f"{destination_id}:halt"
        if halt:
            await self._manager.set_state(self.integration_id, EVENTS_ACTION, halt, source_id=key)
        else:
            await self._manager.delete_state(self.integration_id, EVENTS_ACTION, source_id=key)

    async def get_value(self, action_id: str, key: str) -> Optional[str]:
        data = await self._manager.get_state(self.integration_id, action_id, source_id=key)
        return data.get("value") if data else None

    async def set_value(self, action_id: str, key: str, value: str):
        await self._manager.set_state(self.integration_id, action_id, {"value": value}, source_id=key)

    async def try_lease(self, action_id: str, ttl_seconds: int) -> bool:
        token = uuid.uuid4().hex
        # JSON-encoded because get_state decodes stored values as JSON.
        taken = await self._manager.set_if_absent(self.integration_id, action_id, ttl_seconds=ttl_seconds,
                                                  source_id="lease", value=json.dumps(token))
        if taken:
            self._lease_tokens[action_id] = token
        return taken

    async def release_lease(self, action_id: str):
        """Delete the lease only if this run still holds it; after a TTL expiry it belongs to a later run."""
        token = self._lease_tokens.pop(action_id, None)
        if token is not None:
            await self._manager.delete_if_equal(self.integration_id, action_id, json.dumps(token), source_id="lease")

    async def is_due(self, action_id: str, key: str) -> bool:
        """Whether once_per for this key would go ahead now, without taking it."""
        return not await self._manager.get_state(self.integration_id, action_id, source_id=f"once:{key}")

    async def once_per(self, action_id: str, key: str, ttl_seconds: int) -> bool:
        return await self._manager.set_if_absent(self.integration_id, action_id,
                                                 ttl_seconds=ttl_seconds, source_id=f"once:{key}")
