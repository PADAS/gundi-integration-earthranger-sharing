"""Fixes from Chris's review of PR 4 (17 November)."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.actions.configurations import SyncEventsConfig, SyncObservationsConfig
from app.ersharing import sync_events, sync_observations
from app.ersharing.event_types import EventTypeSync
from app.ersharing.modes import event_settings, observation_settings
from app.ersharing.notes_files import adopt_unrecorded
from app.ersharing.reporting import DestinationReport
from app.ersharing.state import OBSERVATIONS_ACTION, EventRecord, Marker, NoteRecord, ShareState
from app.ersharing.sync_observations import _refresh_details, _share_subject
from app.ersharing.tests.fakes import FakeSite, InMemoryStateManager, make_event, make_observation
from app.ersharing.tests.test_sync_events import setup, sync, wildlife_type
from app.services.errors import IntegrationBadResponseError, IntegrationError

SOURCE_URL = "https://sitea.pamdas.org"


# --- 2. A v2 source type whose destination type is v1 -------------------------------------

@pytest.mark.asyncio
async def test_a_v2_source_type_is_shared_into_the_destinations_v1_type():
    source, dest = FakeSite(SOURCE_URL, role="source"), FakeSite()
    source.event_types["fire"] = {"value": "fire", "display": "Fire", "category": "monitoring",
                                  "schema": {"json": {"properties": {}}, "ui": {}}}
    dest.event_types_v1["sitea_fire"] = {"value": "sitea_fire", "display": "Sitea-Fire"}
    types = EventTypeSync(source, dest, event_settings(SOURCE_URL, SyncEventsConfig()))
    assert (await types.ensure("fire"))["value"] == "sitea_fire"
    assert dest.calls_to("post_event_type_v2") == []


# --- 3 and 4. Turned-down events are tried again; one failing event can't block the rest ---

@pytest.mark.asyncio
async def test_an_event_the_destination_turned_down_is_tried_again_the_next_day():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    dest.failures["post_event"] = lambda event: (
        IntegrationBadResponseError("Destination site siteb.pamdas.org: bad", status_code=400)
        if event["id"] == "e1" else None)
    await sync(run, dest)
    assert set(await state.get_rejected_events("d1")) == {"e1"} and "e2" in dest.events
    dest.failures.pop("post_event")
    run.tried.clear()
    await sync(run, dest)  # the bookmark is past e1: it comes back through the daily retry
    assert "e1" in dest.events and await state.get_rejected_events("d1") == {}


@pytest.mark.asyncio
async def test_an_event_that_keeps_failing_is_set_aside_after_three_runs():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    dest.failures["post_event"] = lambda event: (
        IntegrationError("Destination site siteb.pamdas.org: server error", status_code=500)
        if event["id"] == "e1" else None)
    for _ in range(2):
        with pytest.raises(IntegrationError):
            await sync(run, dest)
        run.tried.clear()
    report = await sync(run, dest)
    assert sync_events.SET_ASIDE in report.errors and "e2" in dest.events
    assert set(await state.get_rejected_events("d1")) == {"e1"} and await state.get_halt("d1") == {}


# --- 5. Notes and files that landed before a timeout aren't posted again ------------------

def test_an_unfinished_copy_adopts_notes_and_files_that_already_landed():
    record = EventRecord(dest_event_id="e1", updated_at=datetime(1970, 1, 1, tzinfo=timezone.utc), pending=True,
                         notes={"n1": NoteRecord(dest_id="d-n1", digest="x")})
    src = {"notes": [{"id": "n1", "text": "a"}, {"id": "n2", "text": "b"}],
           "files": [{"id": "f1", "filename": "a.jpg"}]}
    dest = {"notes": [{"id": "d-n1", "text": "a"}, {"id": "d-n2", "text": "b"}],
            "files": [{"id": "d-f1", "filename": "a.jpg"}]}
    adopt_unrecorded(record, src, dest)
    assert record.notes["n2"].dest_id == "d-n2" and record.files == {"f1": "d-f1"}


# --- A source edit made during a run isn't taken for a destination edit ------------------

@pytest.mark.asyncio
async def test_a_copy_unchanged_since_the_runners_own_write_is_updated():
    source, dest, state, run = setup()
    event = make_event("e1", "2026-10-01T00:01:00+00:00")
    dest.events["e1"] = {**event, "updated_at": "2026-10-01T00:05:00+00:00"}
    record = EventRecord(dest_event_id="e1", updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                         dest_written_at=datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc))
    result = await sync_events._write_event(run, dest, event, {"id": "e1"}, record, DestinationReport("B", "Events"))
    assert result is record and dest.calls_to("patch_event")


# --- 6. Fewer reads per subject ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_subject_is_reread_12_hours_back_only_once_an_hour():
    state = ShareState("i1", manager=InMemoryStateManager())
    source, dest = FakeSite(SOURCE_URL, role="source"), FakeSite()
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    await state.set_marker(OBSERVATIONS_ACTION, "d1:subject:s1", Marker(since=now, advanced_at=now))
    await state.once_per(OBSERVATIONS_ACTION, "d1:details-check:s1", 3600)  # details checked today
    share = observation_settings(SOURCE_URL, SyncObservationsConfig())
    args = (source, {"d1": dest}, {"d1": DestinationReport("B", "Tracks")}, {}, state, share, None, {},
            {"id": "s1", "name": "Leo"}, "site:sitea.pamdas.org#user:svc", now)
    await _share_subject(*args)
    await _share_subject(*args)
    starts = [call[1] for call in source.calls_to("get_observations")]
    assert starts == [now - timedelta(hours=12), now - timedelta(minutes=5)]
    assert source.calls_to("get_subject_sources") == []  # nothing to send, no details check due yet


# --- 7. The first details check takes what the destination has --------------------------

@pytest.mark.asyncio
async def test_the_first_details_check_records_without_writing():
    dest = FakeSite()
    state = ShareState("i1", manager=InMemoryStateManager())
    report = DestinationReport("B", "Tracks")
    assert await _refresh_details(dest, "d1", state, {"id": "s1", "additional": {"a": 1}}, {}, report, "Leo") is True
    assert dest.calls_to("patch_subject") == []
    assert await state.get_value(OBSERVATIONS_ACTION, "d1:details:s1") is not None


# --- Device assignments that can't be read skip only those subjects ---------------------

@pytest.mark.asyncio
async def test_a_failed_assignments_read_skips_only_that_chunk(monkeypatch):
    source = FakeSite(SOURCE_URL, role="source")
    source.failures["get_source_assignments"] = lambda ids: (
        IntegrationBadResponseError("Source site sitea.pamdas.org: bad", status_code=400) if "s1" in ids else None)
    monkeypatch.setattr(sync_observations, "SUBJECT_ID_CHUNK", 1)
    assignments, unread = await sync_observations._load_assignments(source, ["s1", "s2"])
    assert set(unread) == {"s1"}


# --- Catch-up older than 30 days is logged ----------------------------------------------

@pytest.mark.asyncio
async def test_a_catch_up_cut_at_30_days_says_so():
    source, dest, state, run = setup()
    types = EventTypeSync(source, dest, run.settings)
    source.event_types["wildlife_sighting"] = wildlife_type()
    types.waiting = {"wildlife_sighting": datetime.now(timezone.utc) - timedelta(days=45)}
    report = DestinationReport("B", "Events")
    from app.ersharing.destinations import Destination
    from app.actions.configurations import AuthenticateConfig
    await sync_events.catch_up_waiting_types(run, Destination("d1", "B", "https://siteb.pamdas.org",
                                                               AuthenticateConfig(token="t")), dest, types, report)
    assert sync_events.CATCH_UP_CUT in report.errors
