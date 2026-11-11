"""Fixes from the Copilot code review of PR 4."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.actions import handlers
from app.actions.configurations import ListEventTypesQuery, SyncEventsConfig, SyncObservationsConfig
from app.ersharing import clients, sync_observations
from app.ersharing.event_types import _normalize_enum_names
from app.ersharing.modes import event_settings, observation_settings
from app.ersharing.observations import point_batches
from app.ersharing.reporting import DestinationReport
from app.ersharing.state import EventRecord, OBSERVATIONS_ACTION, ShareState
from app.ersharing.sync_events import DEST_IS_NEWER, _write_event
from app.ersharing.tests.fakes import FakeSite, InMemoryStateManager, make_event
from app.services.errors import IntegrationConnectionError

SOURCE_URL = "https://sitea.pamdas.org"


# --- Points recorded at the same moment stay in one batch ---------------------------------

def test_points_recorded_at_the_same_moment_are_never_split_across_batches():
    points = [{"recorded_at": f"2026-10-01T00:00:0{i}Z"} for i in (1, 2, 2, 2, 3)]
    assert [len(b) for b in point_batches(points, size=2)] == [4, 1]
    assert [len(b) for b in point_batches(points[:2], size=2)] == [2]


# --- An unfinished copy resumes after the runner's own write ------------------------------

def events_run():
    return SimpleNamespace(source=SimpleNamespace(url=SOURCE_URL),
                           settings=event_settings(SOURCE_URL, SyncEventsConfig()))


@pytest.mark.asyncio
async def test_an_unfinished_copy_finishes_its_notes_without_rewriting_the_event():
    dest = FakeSite()
    event = make_event("e1", "2026-10-01T00:00:00+00:00")
    dest.events["e1"] = {**event, "updated_at": "2026-10-01T00:05:00+00:00"}  # this runner's earlier write
    record = EventRecord(dest_event_id="e1", updated_at=datetime(1970, 1, 1, tzinfo=timezone.utc), pending=True)
    result = await _write_event(events_run(), dest, event, {"id": "e1", "title": "x"}, record, DestinationReport("d", "Events"))
    assert result is record
    assert dest.calls_to("patch_event") == []


@pytest.mark.asyncio
async def test_a_finished_copy_edited_at_the_destination_is_left_alone():
    dest = FakeSite()
    event = make_event("e1", "2026-10-01T00:00:00+00:00")
    dest.events["e1"] = {**event, "updated_at": "2026-10-01T00:05:00+00:00"}
    record = EventRecord(dest_event_id="e1", updated_at=datetime(1970, 1, 1, tzinfo=timezone.utc))
    result = await _write_event(events_run(), dest, event, {"id": "e1"}, record, DestinationReport("d", "Events"))
    assert result is DEST_IS_NEWER


@pytest.mark.asyncio
async def test_an_update_without_a_title_clears_the_destination_title():
    dest = FakeSite()
    event = make_event("e1", "2026-10-02T00:00:00+00:00")
    dest.events["e1"] = {**event, "title": "Old title", "updated_at": "2026-10-01T00:00:00+00:00"}
    record = EventRecord(dest_event_id="e1", updated_at=datetime(1970, 1, 1, tzinfo=timezone.utc))
    await _write_event(events_run(), dest, event, {"id": "e1", "priority": 0}, record, DestinationReport("d", "Events"))
    assert dest.calls_to("patch_event")[0][1]["title"] == ""


# --- Event type picker lists active v1 types ---------------------------------------------

@pytest.mark.asyncio
async def test_the_type_picker_lists_active_v1_types_and_prefers_v2(monkeypatch):
    site = FakeSite(SOURCE_URL, role="source")
    monkeypatch.setattr(clients, "open_site", lambda url, auth, role: site)
    site.event_types = {"a": {"value": "a", "display": "Alpha v2", "category": "monitoring"}}
    site.event_types_v1 = {
        "a": {"value": "a", "display": "Alpha v1", "category": {"display": "Monitoring"}, "is_active": True},
        "b": {"value": "b", "display": "Bravo", "category": {"display": "Security"}, "is_active": True},
        "c": {"value": "c", "display": "Charlie", "category": {"display": "Security"}, "is_active": False},
    }
    integration = SimpleNamespace(id="i1", base_url=SOURCE_URL, configurations=[
        SimpleNamespace(action=SimpleNamespace(value="auth"), data={"token": "t"})])
    options = (await handlers.action_list_event_types(integration, ListEventTypesQuery()))["options"]
    assert [(o["value"], o["label"]) for o in options] == [("a", "Alpha v2"), ("b", "Bravo")]


# --- Legacy enum labels keep their values -------------------------------------------------

def test_a_legacy_enum_names_list_is_paired_with_the_enum_values():
    properties = {"species": {"enum": ["lion", "zebra"], "enumNames": ["Lion", "Zebra"]},
                  "loose": {"enumNames": ["Only label"]}}
    _normalize_enum_names(properties)
    assert properties["species"]["enumNames"] == {"lion": "Lion", "zebra": "Zebra"}
    assert properties["loose"]["enumNames"] == {"Only label": "Only label"}


# --- Blank pick-list rows and naive start dates ------------------------------------------

def test_blank_pick_list_rows_are_ignored():
    events = event_settings(SOURCE_URL, SyncEventsConfig(event_types=["", " ", "a"], area_feature_groups=["", "fg"]))
    assert events.event_types == frozenset({"a"}) and events.area_feature_groups == ("fg",)
    assert event_settings(SOURCE_URL, SyncEventsConfig(event_types=[""])).event_types == frozenset()
    tracks = observation_settings(SOURCE_URL, SyncObservationsConfig(subject_groups=["", "g1"],
                                                                     area_feature_groups=[" "]))
    assert tracks.subject_groups == frozenset({"g1"}) and tracks.area_feature_groups == ()


def test_a_start_date_without_a_timezone_is_read_as_utc():
    s = event_settings(SOURCE_URL, SyncEventsConfig(start_from=datetime(2026, 9, 1)))
    assert s.start_from == datetime(2026, 9, 1, tzinfo=timezone.utc)


# --- A failing details update stops only that destination --------------------------------

@pytest.mark.asyncio
async def test_a_details_update_that_times_out_stops_only_that_destination(monkeypatch):
    state = ShareState("i1", manager=InMemoryStateManager())
    subject = {"id": "s1", "name": "Leo", "additional": {"rgb": "1,2,3"}}
    source = FakeSite(SOURCE_URL, role="source")
    source.observations = {"s1": []}
    failing, working = FakeSite("https://siteb.pamdas.org"), FakeSite("https://sitec.pamdas.org")
    failing.failures["patch_subject"] = IntegrationConnectionError("Destination site siteb: timed out")
    sites = {"d1": failing, "d2": working}
    reports = {"d1": DestinationReport("d", "Events"), "d2": DestinationReport("d", "Events")}
    share = observation_settings(SOURCE_URL, SyncObservationsConfig())
    # Markers exist, so the details check is due for both destinations.
    from app.ersharing.state import Marker
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    for d_id in sites:
        await state.set_marker(OBSERVATIONS_ACTION, f"{d_id}:subject:s1", Marker(since=now, advanced_at=now))
    finished = await sync_observations._share_subject(source, sites, reports, {}, state, share, None, {}, subject,
                                                      "site:sitea.pamdas.org#user:svc", now)
    assert finished is True
    assert list(sites) == ["d2"]
    assert reports["d1"].errors and not reports["d2"].errors
    assert working.calls_to("patch_subject")
