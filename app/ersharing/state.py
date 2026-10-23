"""Per-destination progress kept in Redis (spec §7.1, §8)."""
import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.services.state import IntegrationStateManager

EVENTS_ACTION = "sync_events"
OBSERVATIONS_ACTION = "sync_observations"


class Marker(BaseModel):
    since: datetime                          # resume reading here
    advanced_at: datetime                    # when `since` last moved (stall detection)
    created_since: Optional[datetime] = None  # observations: high-water mark of created_at


class NoteRecord(BaseModel):
    dest_id: str
    digest: str


class EventRecord(BaseModel):
    dest_event_id: str
    updated_at: datetime
    notes: Dict[str, NoteRecord] = Field(default_factory=dict)  # source note id -> copy
    files: Dict[str, str] = Field(default_factory=dict)         # source file id -> dest file id ("" = not copied)
    incidents: List[str] = Field(default_factory=list)          # incident ids linked at the destination


def _dump(model: BaseModel) -> dict:
    return json.loads(model.json())


class ShareState:
    def __init__(self, integration_id: str, manager=None):
        self.integration_id = integration_id
        self._manager = manager or IntegrationStateManager()
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

    async def once_per(self, action_id: str, key: str, ttl_seconds: int) -> bool:
        return await self._manager.set_if_absent(self.integration_id, action_id,
                                                 ttl_seconds=ttl_seconds, source_id=f"once:{key}")
