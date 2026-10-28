"""sync_events: mirror source events onto each destination (spec §7)."""
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from gundi_core.events import LogLevel
from redis.exceptions import RedisError

from app import settings
from app.services.action_scheduler import trigger_action
from app.services.activity_logger import log_action_activity
from app.services.errors import IntegrationError
from .clients import auth_config_of, halts_site, open_site, source_url_of
from .destinations import Destination, resolve_destinations
from .event_types import TYPE_NOT_READY, EventTypeSync
from .events import (
    clean_event, copied_from, dest_is_newer, event_label, incident_ids, is_copy_of, is_echo, is_newer, member_ids,
    wanted,
)
from .geo import Area, load_area
from .modes import EventSettings, event_settings
from .notes_files import EPOCH, adopt_record, apply_files, apply_incidents, apply_notes, plan_files, plan_notes
from .reporting import DestinationReport, is_stalled, publish_report
from .state import EVENTS_ACTION, EventRecord, Marker, ShareState
from .timeutil import parse_dt, utcnow

logger = logging.getLogger(__name__)

BACKFILL = timedelta(days=10)
RECONCILE_WINDOW = timedelta(days=30)
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

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline


def marker_key(destination_id: str) -> str:
    return f"{destination_id}:marker"


async def sync_destination(run: EventsRun, destination: Destination, dest, report: DestinationReport) -> None:
    now = utcnow()
    marker = await run.state.get_marker(EVENTS_ACTION, marker_key(destination.id))
    since = marker.since if marker else (run.settings.start_from or now - BACKFILL)
    types = EventTypeSync(run.source, dest, run.settings)
    try:
        async for page in run.source.iter_events_updated_since(since):
            for event in page:
                if run.out_of_time():
                    report.timed_out = True
                    return
                await _sync_event(run, destination, dest, types, event, report)
                since = max(since, parse_dt(event["updated_at"]))
    finally:
        # Events arrive oldest first, so `since` is the last fully handled update.
        if marker is None or since != marker.since:
            await run.state.set_marker(EVENTS_ACTION, marker_key(destination.id),
                                       Marker(since=since, advanced_at=now))
        report.marker = since
    await sync_incident_links(run, destination, dest, report)


async def sync_incident_links(run: EventsRun, destination: Destination, dest, report: DestinationReport) -> None:
    """Adding an event to an incident, or removing it, at the source changes neither event's
    updated_at, so the events held by recent incidents are compared on every run (spec §7.5)."""
    async for page in run.source.iter_events_updated_since(utcnow() - INCIDENT_LINKS_WINDOW,
                                                           with_notes_and_files=False, incidents_only=True):
        for incident in page:
            if run.out_of_time():
                report.timed_out = True
                return
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


FOREIGN_COPY = "Some events weren't copied because the destination already has them from another site."
REJECTED = "Some events weren't copied because the destination turned them down."
UNEXPECTED = "Some events weren't copied because something in them couldn't be read."
NOT_REMOVED = "Some copies weren't removed because the destination wouldn't delete them."
NOT_CHECKED = "Some copies weren't checked for removal because the source site didn't allow reading their original."


async def _sync_event(run, destination, dest, types, event, report):
    try:
        await _copy_event(run, destination, dest, types, event, report)
    except IntegrationError as e:
        if halts_site(e):
            raise
        report.failed += 1
        report.error(REJECTED, item=f"{event_label(event)}: {e.message}")
    except RedisError:
        # Progress can't be saved, so moving past the event would lose it.
        raise
    except Exception:
        logger.exception("Couldn't copy event %s", event.get("id"))
        report.failed += 1
        report.error(UNEXPECTED, item=event_label(event))


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
        if complete:
            record.updated_at = parse_dt(event["updated_at"])
        await run.state.set_event_record(destination.id, event["id"], record)


async def _write_event(run: EventsRun, dest, event: dict, copy: dict, record: Optional[EventRecord],
                       report: DestinationReport):
    """Create or update the copy. None when the destination's event came from another
    site; DEST_IS_NEWER when the copy changed after this source change."""
    existing = await dest.get_event(record.dest_event_id if record else event["id"],
                                    with_notes_and_files=record is None)
    if existing is None:
        # Never copied, or deleted at the destination: create it.
        created = await dest.post_event(copy)
        report.created += 1
        return EventRecord(dest_event_id=str(created["id"]), updated_at=EPOCH)
    if record is None and not is_copy_of(existing, event["id"], run.source.url, run.settings.source.value):
        return None
    if dest_is_newer(existing, event):
        return DEST_IS_NEWER
    await dest.patch_event(existing["id"], {k: v for k, v in copy.items() if k != "id"})
    report.updated += 1
    return record or adopt_record(event, existing)


RECONCILE_EVERY_SECONDS = 24 * 60 * 60
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
        async for page in dest.iter_events_updated_since(checked, with_notes_and_files=False):
            for copy in page:
                if run.out_of_time():
                    return False
                await _reconcile_copy(run, destination, dest, copy, report)
                if copy.get("updated_at"):
                    checked = max(checked, parse_dt(copy["updated_at"]))
        finished = True
        return True
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
    for item in skipped:
        await log_action_activity(integration_id=integration_id, action_id=EVENTS_ACTION,
                                  title=f"Destination {item.name} skipped: {item.reason}", level=LogLevel.WARNING)
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
