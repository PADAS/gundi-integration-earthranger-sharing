"""sync_observations: share subject tracks with each destination (spec §8)."""
import logging
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Dict, FrozenSet, List

from gundi_core.events import LogLevel

from app import settings
from app.services.action_scheduler import trigger_action
from app.services.activity_logger import log_action_activity
from app.services.errors import IntegrationError
from .clients import auth_config_of, halts_site, open_site, source_url_of
from .destinations import resolve_destinations
from .geo import load_area
from .modes import observation_settings
from .naming import site_host
from .observations import (
    add_lineage, advance, batches, build_payload, has_valid_location, in_assignment, is_new, parse_assignments,
    read_start,
)
from .reporting import DestinationReport, is_stalled, publish_report
from .state import OBSERVATIONS_ACTION, Marker, ShareState
from .timeutil import parse_dt, utcnow

logger = logging.getLogger(__name__)

BACKFILL = timedelta(hours=12)
SUBJECT_ID_CHUNK = 25
BUDGET_FRACTION = 0.8
LEASE_MARGIN_SECONDS = 30
SUBJECT_CURSOR = "subject-cursor"
PROBE_REJECTIONS = 7  # the batch, its two halves and its four quarters

SOURCE_DENIED = "Some subjects' tracks weren't shared because the source site didn't allow reading them."
SOURCE_FAILED = "Some subjects' tracks weren't shared because the source site couldn't return them."
REJECTED = "Some track points weren't shared because the destination turned them down."
NOT_ACCEPTED = "Some track points weren't shared because the destination couldn't take them."
ALL_REJECTED = "Some track points weren't shared because the destination turned down every point in the batch."


def subject_marker_key(destination_id: str, subject_id: str) -> str:
    return f"{destination_id}:subject:{subject_id}"


def waiting_key(destination_id: str) -> str:
    return f"{destination_id}:waiting"


async def select_subjects(source, group_ids: FrozenSet[str]) -> List[dict]:
    subjects: Dict[str, dict] = {}
    for group in await source.list_subject_groups():
        if group_ids and str(group.get("id")) not in group_ids:
            continue
        for subject in group.get("subjects") or []:
            subjects.setdefault(str(subject["id"]), subject)
    # A stable order lets a follow-up run resume after the saved cursor.
    return [subjects[k] for k in sorted(subjects)]


async def _load_assignments(source, subject_ids: List[str]):
    raw = []
    for i in range(0, len(subject_ids), SUBJECT_ID_CHUNK):
        raw.extend(await source.get_source_assignments(subject_ids[i:i + SUBJECT_ID_CHUNK]))
    return parse_assignments(raw)


async def run_sync_observations(integration, config) -> dict:
    integration_id = str(integration.id)
    state = ShareState(integration_id)
    if not await state.try_lease(OBSERVATIONS_ACTION, settings.MAX_ACTION_EXECUTION_TIME + LEASE_MARGIN_SECONDS):
        return {"status": "skipped", "reason": "Another sync_observations run is still in progress."}
    try:
        result, needs_more_time = await _run(integration, config, state)
    finally:
        await state.release_lease(OBSERVATIONS_ACTION)
    if needs_more_time:
        await trigger_action(integration_id, OBSERVATIONS_ACTION)
    return result


async def _run(integration, config, state: ShareState):
    integration_id = str(integration.id)
    deadline = time.monotonic() + settings.MAX_ACTION_EXECUTION_TIME * BUDGET_FRACTION
    auth = auth_config_of(integration.configurations)
    source_url = source_url_of(integration, auth)
    share = observation_settings(source_url, config)
    destinations, skipped = await resolve_destinations(integration_id, source_url)
    for item in skipped:
        await log_action_activity(integration_id=integration_id, action_id=OBSERVATIONS_ACTION,
                                  title=f"Destination {item.name} skipped: {item.reason}", level=LogLevel.WARNING)
    if not destinations:
        await log_action_activity(integration_id=integration_id, action_id=OBSERVATIONS_ACTION,
                                  title="No EarthRanger destinations to share subject tracks with.",
                                  level=LogLevel.WARNING)
        return {"destinations": []}, False

    reports = {d.id: DestinationReport(destination=d.name, kind="Tracks") for d in destinations}
    freshest: Dict[str, Marker] = {}  # per destination, the subject marker that moved most recently
    async with AsyncExitStack() as stack:
        source = await stack.enter_async_context(
            open_site(source_url, auth, role="source"))
        sites = {}
        for d in destinations:
            try:
                sites[d.id] = await stack.enter_async_context(open_site(d.url, d.auth, role="destination"))
            except IntegrationError as e:
                reports[d.id].error(e.message)
        area = await load_area(source, share.area_feature_groups, polygons_only=True)
        subjects = await select_subjects(source, share.subject_groups)
        assignments = await _load_assignments(source, [str(s["id"]) for s in subjects])
        src_user = share.src_user or (await source.get_me()).get("username", "")
        now = utcnow()
        prefix = f"site:{site_host(source.url)}#user:{src_user}"
        cursor = await state.get_cursor(OBSERVATIONS_ACTION, SUBJECT_CURSOR)
        last = cursor
        for subject in subjects:
            subject_id = str(subject["id"])
            if cursor is not None and subject_id <= cursor:
                continue
            if not sites:
                break
            if time.monotonic() > deadline:
                for d_id in sites:
                    reports[d_id].timed_out = True
                if last is not None:
                    await state.set_cursor(OBSERVATIONS_ACTION, SUBJECT_CURSOR, last)
                break
            await _share_subject(source, sites, reports, freshest, state, share, area, assignments, subject,
                                 prefix, now)
            last = subject_id
        else:
            await state.delete_marker(OBSERVATIONS_ACTION, SUBJECT_CURSOR)

    for d in destinations:
        report = reports[d.id]
        try:
            await publish_report(integration_id, OBSERVATIONS_ACTION, report)
            marker = await _progress_marker(state, d.id, freshest.get(d.id), report)
            if report.errors and is_stalled(marker, utcnow()):
                await log_action_activity(
                    integration_id=integration_id, action_id=OBSERVATIONS_ACTION, level=LogLevel.WARNING,
                    title=f"Tracks → {d.name}: no progress since {marker.advanced_at:%Y-%m-%d %H:%M} UTC. "
                          f"Check the errors above.",
                )
        except Exception:
            logger.exception("Couldn't publish the tracks report for %s", d.name)
    return ({"destinations": [r.as_dict() for r in reports.values()]},
            any(r.timed_out for r in reports.values()))


async def _progress_marker(state: ShareState, destination_id: str, freshest, report: DestinationReport):
    """The marker the stall check reads. A destination that hasn't taken a point yet
    has no subject markers, so the time its errors started stands in for one."""
    key = waiting_key(destination_id)
    if freshest is not None or not report.errors:
        await state.delete_marker(OBSERVATIONS_ACTION, key)
        return freshest
    waiting = await state.get_marker(OBSERVATIONS_ACTION, key)
    if waiting is None:
        now = utcnow()
        waiting = Marker(since=now, advanced_at=now)
        await state.set_marker(OBSERVATIONS_ACTION, key, waiting)
    return waiting


async def _share_subject(source, sites, reports, freshest, state, share, area, assignments, subject, prefix, now):
    subject_id = str(subject["id"])
    label = subject.get("name") or subject_id
    markers = {d_id: await state.get_marker(OBSERVATIONS_ACTION, subject_marker_key(d_id, subject_id))
               for d_id in sites}
    backfill_from = now - BACKFILL
    start = read_start(list(markers.values()),
                       max(parse_dt(share.start_from), backfill_from) if share.start_from else backfill_from)
    try:
        observations = await source.get_observations(subject_id, start)
        source_additional = {}
        if share.subject_additional:
            source_additional = {str(s.get("id")): s.get("additional") or {}
                                 for s in await source.get_subject_sources(subject_id)}
    except IntegrationError as e:
        denied = e.status_code == 403
        if halts_site(e) and not denied:
            raise
        for d_id in sites:
            reports[d_id].error(SOURCE_DENIED if denied else SOURCE_FAILED, item=f"{label}: {e.message}")
        return

    candidates = sorted(
        (o for o in observations
         if has_valid_location(o)
         and in_assignment(o, assignments.get(subject_id, []))
         and (area is None or area.contains_point(o["location"]["longitude"], o["location"]["latitude"]))),
        key=lambda o: parse_dt(o["recorded_at"]),
    )
    fingerprint = f"{prefix}#subject:{subject_id}#rts:{now:%Y%m%d%H%M%S}"
    for d_id, dest in list(sites.items()):
        key = subject_marker_key(d_id, subject_id)
        marker = markers[d_id]
        complete = True
        for batch in batches([o for o in candidates if is_new(o, marker)]):
            payloads = [add_lineage(build_payload(subject, o, share, source_additional), fingerprint) for o in batch]
            try:
                complete = await _send(dest, share.provider_key, payloads, reports[d_id], label)
            except IntegrationError as e:
                reports[d_id].error(e.message)
                del sites[d_id]
                complete = False
            if not complete:
                break
            marker = advance(marker, batch, now)
            await state.set_marker(OBSERVATIONS_ACTION, key, marker)
        if complete and observations:
            # Points that will never be shared (filtered out) still move the marker, so they aren't re-read.
            moved = advance(marker, observations, now)
            if moved != marker:
                marker = moved
                await state.set_marker(OBSERVATIONS_ACTION, key, marker)
        if marker is not None:
            current = reports[d_id].marker
            reports[d_id].marker = marker.since if current is None else max(current, marker.since)
            if d_id not in freshest or marker.advanced_at > freshest[d_id].advanced_at:
                freshest[d_id] = marker


class _WholeBatchRejected(Exception):
    """The batch, both halves and all four quarters were turned down, so the batch itself is the problem."""


class _Split:
    def __init__(self):
        self.rejected: List[tuple] = []
        self.rejection = ""  # the destination's message for the latest 400


async def _send(dest, provider_key: str, payloads: List[dict], report: DestinationReport, label: str) -> bool:
    """Post points, halving a rejected batch until each bad point is found and skipped.
    False when the destination didn't take them for another reason, or turned down
    the batch as a whole (every point, or the batch, its halves and its quarters),
    so the marker holds. Raises the errors that stop the destination for this run."""
    split = _Split()
    try:
        if len(payloads) >= 4:
            complete = await _send_probing(dest, provider_key, payloads, report, label, split)
        else:
            complete = await _send_splitting(dest, provider_key, payloads, report, label, split)
    except _WholeBatchRejected as whole:
        report.failed += len(payloads)
        report.error(ALL_REJECTED, item=f"{label}: {whole}")
        return False
    except IntegrationError:
        _report_rejected(report, label, split.rejected)
        raise
    if complete and len(payloads) > 1 and len(split.rejected) == len(payloads):
        report.failed += len(payloads)
        report.error(ALL_REJECTED, item=f"{label}: {split.rejected[0][1]}")
        return False
    _report_rejected(report, label, split.rejected)
    return complete


def _report_rejected(report: DestinationReport, label: str, rejected: List[tuple]):
    for recorded_at, message in rejected:
        report.failed += 1
        report.error(REJECTED, item=f"{label} at {recorded_at}: {message}")


async def _post(dest, provider_key: str, payloads: List[dict], report: DestinationReport, label: str,
                split: _Split):
    """True when taken, None when turned down (400), False when not taken for another reason."""
    try:
        await dest.post_sharing_status(provider_key, payloads)
        report.created += len(payloads)
        return True
    except IntegrationError as e:
        if halts_site(e):
            raise
        if e.status_code != 400:
            report.failed += len(payloads)
            report.error(NOT_ACCEPTED, item=f"{label}: {e.message}")
            return False
        split.rejection = e.message
        return None


async def _send_probing(dest, provider_key: str, payloads: List[dict], report: DestinationReport, label: str,
                        split: _Split) -> bool:
    """Tries the batch, its halves and its quarters before descending, so a batch-level
    rejection costs at most seven posts instead of a full split."""
    outcome = await _post(dest, provider_key, payloads, report, label, split)
    if outcome is not None:
        return outcome
    batch_message = split.rejection
    rejected_nodes, pending = 1, []
    middle = len(payloads) // 2
    for half in (payloads[:middle], payloads[middle:]):
        outcome = await _post(dest, provider_key, half, report, label, split)
        if outcome is True:
            continue
        if outcome is False:
            return False
        rejected_nodes += 1
        quarter_at = len(half) // 2
        for quarter in (half[:quarter_at], half[quarter_at:]):
            outcome = await _post(dest, provider_key, quarter, report, label, split)
            if outcome is True:
                continue
            if outcome is False:
                return False
            rejected_nodes += 1
            pending.append((quarter, split.rejection))
    if rejected_nodes == PROBE_REJECTIONS:
        raise _WholeBatchRejected(batch_message)
    for quarter, message in pending:
        if not await _split_rejected(dest, provider_key, quarter, report, label, split, message):
            return False
    return True


async def _split_rejected(dest, provider_key, payloads, report, label, split, message) -> bool:
    """Split a batch already known to be turned down."""
    if len(payloads) == 1:
        split.rejected.append((payloads[0]["recorded_at"], message))
        return True
    middle = len(payloads) // 2
    return (await _send_splitting(dest, provider_key, payloads[:middle], report, label, split)
            and await _send_splitting(dest, provider_key, payloads[middle:], report, label, split))


async def _send_splitting(dest, provider_key: str, payloads: List[dict], report: DestinationReport, label: str,
                          split: _Split) -> bool:
    outcome = await _post(dest, provider_key, payloads, report, label, split)
    if outcome is not None:
        return outcome
    return await _split_rejected(dest, provider_key, payloads, report, label, split, split.rejection)
