"""sync_events: mirror source events onto each destination (spec §7)."""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional, Set

from gundi_core.events import LogLevel
from redis.exceptions import RedisError

from app import settings
from app.services.action_scheduler import trigger_action
from app.services.activity_logger import log_action_activity
from app.services.errors import IntegrationError
from .clients import auth_config_of, halts_site, open_site, refused_by_destination, source_url_of
from .destinations import Destination, resolve_destinations
from .event_types import TYPE_NOT_READY, EventTypeSync
from .events import (
    clean_event, copied_from, dest_is_newer, event_label, incident_ids, is_copy_of, is_echo, is_newer, member_ids,
    wanted,
)
from .geo import Area, load_area
from .modes import EventSettings, event_settings
from .notes_files import EPOCH, adopt_record, adopt_unrecorded, apply_files, apply_incidents, apply_notes, plan_files, plan_notes
from .reporting import DestinationReport, is_stalled, log_skipped_destinations, publish_report
from .state import EVENTS_ACTION, EventRecord, Marker, ShareState
from .timeutil import parse_dt, utcnow

logger = logging.getLogger(__name__)

BACKFILL = timedelta(days=10)
RECONCILE_WINDOW = timedelta(days=30)
# How far back events skipped for their event type are read again once the type can be set up.
CATCH_UP_LIMIT = timedelta(days=30)
REJECTED_RETRY_EVERY_SECONDS = 24 * 60 * 60  # events the destination turned down are tried again daily
HALT_LIMIT = 3  # runs in a row that stop on the same event before it is set aside
INCIDENT_LINKS_WINDOW = timedelta(days=30)
# The destination's copy changed after this source change, so it is left alone (spec §7.3).
DEST_IS_NEWER = object()


@dataclass
class EventsRun:
    integration_id: str
    source: object  # ERSite
    settings: EventSettings
    area: Optional[Area]
    state: ShareState
    deadline: float  # time.monotonic() value
    pending: Dict[str, Set[str]] = field(default_factory=dict)  # per destination, copies still unfinished
    tried: Dict[str, Set[str]] = field(default_factory=dict)    # per destination, events handled this run
    rejected: Dict[str, Dict[str, datetime]] = field(default_factory=dict)  # per destination, events to retry
    halts: Dict[str, dict] = field(default_factory=dict)        # per destination, the event the last halt stopped on

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline


def marker_key(destination_id: str) -> str:
    return f"{destination_id}:marker"


async def sync_destination(run: EventsRun, destination: Destination, dest, report: DestinationReport) -> None:
    now = utcnow()
    marker = await run.state.get_marker(EVENTS_ACTION, marker_key(destination.id))
    since = marker.since if marker else (run.settings.start_from or now - BACKFILL)
    types = EventTypeSync(run.source, dest, run.settings)
    types.waiting = await run.state.get_waiting_types(destination.id)
    waiting_before = dict(types.waiting)
    pending = run.pending[destination.id] = set(await run.state.get_pending_events(destination.id))
    pending_before = set(pending)
    rejected = run.rejected[destination.id] = await run.state.get_rejected_events(destination.id)
    rejected_before = dict(rejected)
    run.halts[destination.id] = await run.state.get_halt(destination.id)
    try:
        async for page in run.source.iter_events_updated_since(since):
            for event in page:
                if run.out_of_time():
                    report.timed_out = True
                    return
                await _sync_event(run, destination, dest, types, event, report)
                since = max(since, parse_dt(event["updated_at"]))
        await catch_up_waiting_types(run, destination, dest, types, report)
        await finish_pending_copies(run, destination, dest, types, report, pending_before)
        await retry_rejected_events(run, destination, dest, types, report, rejected_before)
    finally:
        # The lists of events to revisit are saved before the bookmark moves past them, so a
        # failed write can't leave the bookmark ahead of what they record.
        if types.waiting != waiting_before:
            await run.state.set_waiting_types(destination.id, types.waiting)
        if pending != pending_before:
            await run.state.set_pending_events(destination.id, list(pending))
        if rejected != rejected_before:
            await run.state.set_rejected_events(destination.id, rejected)
        # Events arrive oldest first, so `since` is the last fully handled update.
        if marker is None or since != marker.since:
            await run.state.set_marker(EVENTS_ACTION, marker_key(destination.id),
                                       Marker(since=since, advanced_at=now))
        report.marker = since
    await sync_incident_links(run, destination, dest, report)


async def catch_up_waiting_types(run: EventsRun, destination: Destination, dest, types: EventTypeSync,
                                 report: DestinationReport) -> None:
    """Copy the events skipped while their event type couldn't be set up, once it can.
    The bookmark moved past them, so they are read again from the oldest one skipped,
    going back at most CATCH_UP_LIMIT. A type that still fails keeps waiting."""
    if not types.waiting:
        return
    ready = set()
    for value in list(types.waiting):
        if value in types.failures:
            continue  # failed again on this run
        if await types.ensure(value) is not None:
            ready.add(value)
    if not ready:
        return
    oldest = min(types.waiting[v] for v in ready)
    start = max(oldest, utcnow() - CATCH_UP_LIMIT)
    if oldest < start:
        report.error(CATCH_UP_CUT, item=f"{', '.join(sorted(ready))}: from {start:%Y-%m-%d %H:%M} UTC",
                     level=LogLevel.WARNING)
    async for page in run.source.iter_events_updated_since(start):
        for event in page:
            if run.out_of_time():
                report.timed_out = True
                return  # still waiting: the next run reads them again
            if event.get("event_type") in ready:
                await _sync_event(run, destination, dest, types, event, report)
    for value in ready:
        types.waiting.pop(value, None)


async def finish_pending_copies(run: EventsRun, destination: Destination, dest, types: EventTypeSync,
                                report: DestinationReport, carried_over: Set[str]) -> None:
    """Finish copies left unfinished by an earlier run. The bookmark may have moved past
    their event, so each is read again by its id; events already handled in this run wait."""
    pending = run.pending.get(destination.id) or set()
    tried = run.tried.get(destination.id) or set()
    for event_id in sorted((carried_over & pending) - tried):
        if run.out_of_time():
            report.timed_out = True
            return
        try:
            event = await run.source.get_event(event_id, with_notes_and_files=True)
        except IntegrationError as e:
            if halts_site(e):
                raise
            continue  # tried again next run
        if event is None:
            pending.discard(event_id)  # gone at the source; the daily check handles the copy
            continue
        await _sync_event(run, destination, dest, types, event, report)


async def retry_rejected_events(run: EventsRun, destination: Destination, dest, types: EventTypeSync,
                                report: DestinationReport, carried_over: Dict[str, datetime]) -> None:
    """Once a day, try again the events the destination turned down or kept failing on, for
    example after an admin fixed the destination's event type. Each is read again by its
    id, for up to CATCH_UP_LIMIT after it was first turned down."""
    rejected = run.rejected.get(destination.id)
    if not carried_over or rejected is None:
        return
    if not await run.state.once_per(EVENTS_ACTION, f"{destination.id}:rejected-retry", REJECTED_RETRY_EVERY_SECONDS):
        return
    oldest_kept = utcnow() - CATCH_UP_LIMIT
    tried = run.tried.get(destination.id) or set()
    for event_id, since in sorted(carried_over.items()):
        if event_id not in rejected or event_id in tried:
            continue
        if since < oldest_kept:
            rejected.pop(event_id, None)
            continue
        if run.out_of_time():
            report.timed_out = True
            return
        try:
            event = await run.source.get_event(event_id, with_notes_and_files=True)
        except IntegrationError as e:
            if halts_site(e):
                raise
            continue
        if event is None:
            rejected.pop(event_id, None)  # gone at the source
            continue
        await _sync_event(run, destination, dest, types, event, report)


def incident_cursor_key(destination_id: str) -> str:
    return f"{destination_id}:incident-cursor"


async def sync_incident_links(run: EventsRun, destination: Destination, dest, report: DestinationReport) -> None:
    """Adding an event to an incident, or removing it, at the source changes neither event's
    updated_at, so the events held by recent incidents are compared on every run (spec §7.5)."""
    # A scan cut short by the time budget resumes where it stopped, so a long window is
    # covered across runs instead of restarting at its beginning every time.
    key = incident_cursor_key(destination.id)
    window_start = utcnow() - INCIDENT_LINKS_WINDOW
    cursor = await run.state.get_marker(EVENTS_ACTION, key)
    checked = max(window_start, cursor.since) if cursor else window_start
    async for page in run.source.iter_events_updated_since(checked, with_notes_and_files=False, incidents_only=True):
        for incident in page:
            if run.out_of_time():
                report.timed_out = True
                await run.state.set_marker(EVENTS_ACTION, key, Marker(since=checked, advanced_at=utcnow()))
                return
            if incident.get("updated_at"):
                checked = max(checked, parse_dt(incident["updated_at"]))
            incident_id = incident["id"]
            incident_record = await run.state.get_event_record(destination.id, incident_id)
            if incident_record is None:
                continue  # the incident itself isn't copied
            members = member_ids(incident)
            for event_id in dict.fromkeys([*members, *incident_record.members]):
                record = await run.state.get_event_record(destination.id, event_id)
                if record is None or (event_id in members) == (incident_id in record.incidents):
                    continue
                wanted_ids = ([*record.incidents, incident_id] if event_id in members
                              else [i for i in record.incidents if i != incident_id])
                await apply_incidents(dest, record.dest_event_id, wanted_ids, record, report)
                await run.state.set_event_record(destination.id, event_id, record)
            if incident_record.members != members:
                incident_record.members = members
                await run.state.set_event_record(destination.id, incident_id, incident_record)
    await run.state.delete_marker(EVENTS_ACTION, key)


FOREIGN_COPY = "Some events weren't copied because the destination already has them from another site."
REJECTED = "Some events weren't copied because the destination turned them down."
UNEXPECTED = "Some events weren't copied because something in them couldn't be read."
CATCH_UP_CUT = ("Events that waited for their event type were copied only from 30 days back; "
                "older ones weren't copied.")
SET_ASIDE = (f"Some events were set aside because the destination failed on each of them in "
             f"{HALT_LIMIT} runs in a row.")
NOT_REMOVED = "Some copies weren't removed because the destination wouldn't delete them."
NOT_CHECKED = "Some copies weren't checked for removal because the source site didn't allow reading their original."


async def _sync_event(run, destination, dest, types, event, report):
    event_id = str(event.get("id"))
    run.tried.setdefault(destination.id, set()).add(event_id)
    rejected = run.rejected.setdefault(destination.id, {})
    try:
        await _copy_event(run, destination, dest, types, event, report)
    except IntegrationError as e:
        halts = halts_site(e) and not refused_by_destination(e)
        if halts and not await _set_aside_after_repeated_halts(run, destination, event_id, e):
            raise
        report.failed += 1
        report.error(SET_ASIDE if halts else REJECTED, item=f"{event_label(event)}: {e.message}")
        rejected.setdefault(event_id, utcnow())
        return
    except RedisError:
        # Progress can't be saved, so moving past the event would lose it.
        raise
    except Exception:
        logger.exception("Couldn't copy event %s", event.get("id"))
        report.failed += 1
        report.error(UNEXPECTED, item=event_label(event))
        rejected.setdefault(event_id, utcnow())
        return
    rejected.pop(event_id, None)
    if run.halts.get(destination.id, {}).get("event") == event_id:
        run.halts[destination.id] = {}
        await run.state.set_halt(destination.id, {})


def _fails_on_this_event(error: IntegrationError) -> bool:
    """A conflict or server error that may come from this event's own payload. Unreachable
    sites (502, 503, 504), rejected credentials and rate limits concern every event."""
    status = error.status_code or 0
    return status == 409 or (status >= 500 and status not in (502, 503, 504))


async def _set_aside_after_repeated_halts(run, destination, event_id: str, error: IntegrationError) -> bool:
    """True when runs have stopped on this same event HALT_LIMIT times in a row, so it is set
    aside for the daily retry and the run carries on with the events after it."""
    if not _fails_on_this_event(error):
        return False
    halt = run.halts.get(destination.id) or {}
    count = halt.get("count", 0) + 1 if halt.get("event") == event_id else 1
    if count >= HALT_LIMIT:
        run.halts[destination.id] = {}
        await run.state.set_halt(destination.id, {})
        return True
    run.halts[destination.id] = {"event": event_id, "count": count}
    await run.state.set_halt(destination.id, run.halts[destination.id])
    return False


async def _copy_event(run, destination, dest, types, event, report):
    if not wanted(event, run.settings, run.area) or is_echo(event, dest.url):
        report.skipped += 1
        return
    record = await run.state.get_event_record(destination.id, event["id"])
    if not is_newer(event, record):
        report.skipped += 1
        return
    dest_type = await types.ensure(event["event_type"])
    if dest_type is None:
        updated = parse_dt(event["updated_at"])
        waiting_since = types.waiting.get(event["event_type"])
        if waiting_since is None or updated < waiting_since:
            types.waiting[event["event_type"]] = updated
        report.failed += 1
        report.error(TYPE_NOT_READY, item=f"{event_label(event)}, event type {types.failures[event['event_type']]}")
        return
    copy = clean_event(event, run.settings, dest_type, run.source.service_root)
    record = await _write_event(run, dest, event, copy, record, report)
    if record is DEST_IS_NEWER:
        report.skipped += 1
        return
    if record is None:
        report.skipped += 1
        report.error(FOREIGN_COPY, item=event_label(event), level=LogLevel.WARNING)
        return
    complete = False
    try:
        await apply_notes(dest, record.dest_event_id, plan_notes(event.get("notes"), record.notes), record)
        await apply_files(run.source, dest, record.dest_event_id, plan_files(event.get("files"), record.files),
                          record, report)
        await apply_incidents(dest, record.dest_event_id, incident_ids(event), record, report)
        complete = True
    finally:
        # Saved on failure too so copied notes aren't re-posted; the old updated_at keeps the event due.
        record.pending = not complete
        unfinished = run.pending.setdefault(destination.id, set())
        if complete:
            unfinished.discard(str(event["id"]))
        else:
            unfinished.add(str(event["id"]))
        if complete:
            record.updated_at = parse_dt(event["updated_at"])
        await run.state.set_event_record(destination.id, event["id"], record)


async def _write_event(run: EventsRun, dest, event: dict, copy: dict, record: Optional[EventRecord],
                       report: DestinationReport):
    """Create or update the copy. None when the destination's event came from another
    site; DEST_IS_NEWER when the copy changed after this source change."""
    # An unfinished copy is read with its notes and files: one posted just before a timeout
    # may have landed without being recorded.
    existing = await dest.get_event(record.dest_event_id if record else event["id"],
                                    with_notes_and_files=record is None or record.pending)
    if existing is None:
        # Never copied, or deleted at the destination: create it.
        created = await dest.post_event(copy)
        report.created += 1
        return EventRecord(dest_event_id=str(created["id"]), updated_at=EPOCH, written_at=parse_dt(event["updated_at"]),
                           dest_written_at=_updated_at(created))
    if record is None and not is_copy_of(existing, event["id"], run.source.url, run.settings.source.value):
        return None
    if record is not None and record.pending:
        adopt_unrecorded(record, event, existing)
    if dest_is_newer(existing, event) and not _is_own_write(existing, record):
        if record is None or not record.pending:
            return DEST_IS_NEWER
        # The newer copy is this runner's own earlier write, whose notes, files or links
        # didn't all land. Same source version: finish those without rewriting the event.
        # A newer source version since then is written as usual.
        if record.written_at is not None and record.written_at == parse_dt(event["updated_at"]):
            return record
    changes = {k: v for k, v in copy.items() if k != "id"}
    # clean_event leaves out an empty title; on an update that would keep the old one.
    changes.setdefault("title", "")
    patched = await dest.patch_event(existing["id"], changes)
    report.updated += 1
    record = record or adopt_record(event, existing)
    record.written_at = parse_dt(event["updated_at"])
    record.dest_written_at = _updated_at(patched)
    return record


def _updated_at(dest_event) -> Optional[datetime]:
    value = dest_event.get("updated_at") if isinstance(dest_event, dict) else None
    return parse_dt(value) if value else None


def _is_own_write(existing: dict, record: Optional[EventRecord]) -> bool:
    """The copy hasn't changed since this runner last wrote it, so a newer timestamp there
    isn't a destination edit (for example a source edit made while the run was writing)."""
    if record is None or record.dest_written_at is None or not existing.get("updated_at"):
        return False
    return parse_dt(existing["updated_at"]) <= record.dest_written_at


RECONCILE_EVERY_SECONDS = 24 * 60 * 60
RECONCILE_CHUNK = 500  # copies listed before they are checked
BUDGET_FRACTION = 0.8
LEASE_MARGIN_SECONDS = 30


def reconcile_key(destination_id: str) -> str:
    return f"{destination_id}:reconcile"


def reconcile_cursor_key(destination_id: str) -> str:
    return f"{destination_id}:reconcile-cursor"


async def reconcile_destination(run: EventsRun, destination: Destination, dest, report: DestinationReport) -> bool:
    """Delete recent destination copies whose source event is gone (v1 delete_unmatched_events).
    Resumes from the saved cursor; returns True once the pass is complete."""
    key = reconcile_cursor_key(destination.id)
    cursor = await run.state.get_marker(EVENTS_ACTION, key)
    checked = cursor.since if cursor else utcnow() - RECONCILE_WINDOW
    finished = False
    try:
        # Copies are listed a chunk at a time and checked before the next chunk is listed,
        # starting again from the last one checked: deleting while paging by number would
        # shift unread copies onto pages already read, and a listing cut short by the time
        # budget would otherwise start over every run.
        while True:
            copies, listed_all = [], True
            async for page in dest.iter_events_updated_since(checked, with_notes_and_files=False):
                copies.extend(page)
                if len(copies) >= RECONCILE_CHUNK or run.out_of_time():
                    listed_all = False
                    break
            before = checked
            for copy in copies:
                if run.out_of_time():
                    return False
                await _reconcile_copy(run, destination, dest, copy, report)
                if copy.get("updated_at"):
                    checked = max(checked, parse_dt(copy["updated_at"]))
            if listed_all:
                finished = True
                return True
            if run.out_of_time() or checked == before:
                return False  # the saved position carries on next run
    finally:
        if finished:
            await run.state.delete_marker(EVENTS_ACTION, key)
        else:
            await run.state.set_marker(EVENTS_ACTION, key, Marker(since=checked, advanced_at=utcnow()))


async def _reconcile_copy(run: EventsRun, destination: Destination, dest, copy: dict, report: DestinationReport):
    details = copy.get("event_details") or {}
    src_id = details.get("er2er_src_id")
    if (not src_id or details.get("er2er_src_system") != run.settings.source.value
            or not copied_from(copy, run.source.url)):
        return
    try:
        if await run.source.get_event(src_id) is not None:
            return
    except IntegrationError as e:
        if e.status_code != 403:
            raise
        report.error(NOT_CHECKED, item=f"{copy['id']}: {e.message}", level=LogLevel.WARNING)
        return
    try:
        await dest.delete_event(copy["id"])
    except IntegrationError as e:
        if halts_site(e):
            raise
        if e.status_code != 404:
            report.error(NOT_REMOVED, item=f"{copy['id']}: {e.message}")
            return
    await run.state.delete_event_record(destination.id, src_id)
    report.deleted += 1


async def _maybe_reconcile(run: EventsRun, destination: Destination, dest, report: DestinationReport):
    """Continue an unfinished pass, or start a new one at most once a day."""
    if run.out_of_time():
        return
    in_progress = await run.state.get_marker(EVENTS_ACTION, reconcile_cursor_key(destination.id))
    if in_progress is None and not await run.state.once_per(EVENTS_ACTION, reconcile_key(destination.id),
                                                            RECONCILE_EVERY_SECONDS):
        return
    await reconcile_destination(run, destination, dest, report)


async def run_sync_events(integration, config) -> dict:
    integration_id = str(integration.id)
    state = ShareState(integration_id)
    if not await state.try_lease(EVENTS_ACTION, settings.MAX_ACTION_EXECUTION_TIME + LEASE_MARGIN_SECONDS):
        return {"status": "skipped", "reason": "Another sync_events run is still in progress."}
    try:
        result, needs_more_time = await _run(integration, config, state)
    finally:
        await state.release_lease(EVENTS_ACTION)
    if needs_more_time:
        await trigger_action(integration_id, EVENTS_ACTION)
    return result


async def _run(integration, config, state: ShareState):
    integration_id = str(integration.id)
    deadline = time.monotonic() + settings.MAX_ACTION_EXECUTION_TIME * BUDGET_FRACTION
    auth = auth_config_of(integration.configurations)
    source_url = source_url_of(integration, auth)
    share = event_settings(source_url, config)
    destinations, skipped = await resolve_destinations(integration_id, source_url)
    await log_skipped_destinations(integration_id, EVENTS_ACTION, state, skipped)
    if not destinations:
        await log_action_activity(integration_id=integration_id, action_id=EVENTS_ACTION,
                                  title="No EarthRanger destinations to share events with.", level=LogLevel.WARNING)
        return {"destinations": []}, False

    done = []
    async with open_site(source_url, auth, role="source") as source:
        run = EventsRun(integration_id, source, share, await load_area(source, share.area_feature_groups),
                        state, deadline)
        try:
            synced = []
            for destination in destinations:
                report = DestinationReport(destination=destination.name, kind="Events")
                done.append((destination, report))
                ok = await _on_destination(destination, report,
                                           lambda dest: sync_destination(run, destination, dest, report))
                if ok and not report.timed_out:
                    synced.append((destination, report))
            # Reconciling last means it only uses time the syncs left over.
            if share.delete_unmatched:
                for destination, report in synced:
                    await _on_destination(destination, report,
                                          lambda dest: _maybe_reconcile(run, destination, dest, report))
        finally:
            for destination, report in done:
                try:
                    await _publish(integration_id, state, destination, report)
                except Exception:
                    logger.exception("Couldn't publish the events report for %s", destination.name)
    reports = [report for _, report in done]
    return {"destinations": [r.as_dict() for r in reports]}, any(r.timed_out for r in reports)


async def _on_destination(destination: Destination, report: DestinationReport, step) -> bool:
    """Run one step against a destination; its failure stops only that destination."""
    try:
        async with open_site(destination.url, destination.auth, role="destination") as dest:
            await step(dest)
        return True
    except IntegrationError as e:
        if getattr(e, "site_role", None) == "source":
            raise
        report.error(e.message)
        return False


async def _publish(integration_id: str, state: ShareState, destination: Destination, report: DestinationReport):
    await publish_report(integration_id, EVENTS_ACTION, report)
    marker = await state.get_marker(EVENTS_ACTION, marker_key(destination.id))
    if report.errors and is_stalled(marker, utcnow()):
        await log_action_activity(
            integration_id=integration_id, action_id=EVENTS_ACTION, level=LogLevel.WARNING,
            title=f"Events → {destination.name}: no progress since "
                  f"{marker.advanced_at:%Y-%m-%d %H:%M} UTC. Check the errors above.",
        )
