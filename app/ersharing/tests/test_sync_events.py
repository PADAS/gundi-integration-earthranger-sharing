import time
from unittest.mock import MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.actions.configurations import AuthenticateConfig, SyncEventsConfig
from app.ersharing.clients import ERSite
from app.ersharing.destinations import Destination
from app.ersharing.event_types import TYPE_NOT_READY
from app.ersharing.modes import event_settings
from app.ersharing.reporting import DestinationReport
from app.ersharing.state import EVENTS_ACTION, EventRecord, ShareState
from app.ersharing import sync_events
from app.ersharing.sync_events import EventsRun, marker_key, sync_destination
from app.ersharing.tests.fakes import FakeSite, InMemoryStateManager, make_event
from app.ersharing.timeutil import parse_dt
from app.services.errors import IntegrationBadResponseError, IntegrationConnectionError

SOURCE_URL = "https://sitea.pamdas.org"
DESTINATION = Destination("d1", "Site B", "https://siteb.pamdas.org", AuthenticateConfig(token="t"))


def wildlife_type():
    return {"value": "wildlife_sighting", "display": "Wildlife Sighting", "category": "monitoring",
            "schema": {"json": {"properties": {}}, "ui": {}}}


def provenance(src_id, site_url, system="sitea"):
    return {"er2er_src_id": src_id, "er2er_src_system": system, "er2er_src_service_root": f"{site_url}/api/v1.0"}


def setup(*events, config=None, deadline=None):
    source = FakeSite(SOURCE_URL, role="source")
    source.event_types["wildlife_sighting"] = wildlife_type()
    for event in events:
        source.events[event["id"]] = event
    dest = FakeSite()
    dest.write_time = "2026-09-30T00:00:00+00:00"  # copies look older than the source edits below
    state = ShareState("i1", manager=InMemoryStateManager())
    settings = event_settings(SOURCE_URL, config or SyncEventsConfig.parse_obj({"start_from": "2026-09-01T00:00:00Z"}))
    run = EventsRun("i1", source, settings, None, state, deadline or time.monotonic() + 60)
    return source, dest, state, run


async def sync(run, dest):
    report = DestinationReport("Site B", "Events")
    await sync_destination(run, DESTINATION, dest, report)
    return report


@pytest.mark.asyncio
async def test_creates_copies_with_same_uuid_and_advances_marker():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    report = await sync(run, dest)
    assert set(dest.events) == {"e1", "e2"}
    assert dest.events["e1"]["event_details"]["er2er_src_id"] == "e1"
    assert report.created == 2
    marker = await state.get_marker(EVENTS_ACTION, marker_key("d1"))
    assert marker.since == parse_dt("2026-10-02T00:00:00Z")


@pytest.mark.asyncio
async def test_rerun_does_not_rewrite_unchanged_events():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"))
    await sync(run, dest)
    calls_before = len(dest.calls)
    report = await sync(run, dest)  # re-reads e1 at the marker boundary (update_date >= marker)
    assert report.skipped == 1 and report.created == report.updated == 0
    assert not [c for c in dest.calls[calls_before:] if c[0] in ("post_event", "patch_event")]


@pytest.mark.asyncio
async def test_source_edit_patches_copy_and_adds_only_new_notes():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", notes=[{"id": "n1", "text": "a"}]))
    await sync(run, dest)
    source.events["e1"].update(state="resolved", updated_at="2026-10-03T00:00:00Z",
                               notes=[{"id": "n1", "text": "a"}, {"id": "n2", "text": "b"}])
    report = await sync(run, dest)
    assert report.updated == 1
    assert dest.events["e1"]["state"] == "resolved"
    assert [n["text"] for n in dest.events["e1"]["notes"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_adopts_v1_copy_without_duplicating_notes_or_files():
    event = make_event("e1", "2026-10-01T00:00:00Z", notes=[{"id": "n1", "text": "hello"}],
                       files=[{"id": "f1", "url": "u1", "filename": "a.jpg"}])
    source, dest, state, run = setup(event)
    source.files["u1"] = b"img"
    dest.events["e1"] = {"id": "e1", "event_type": "sitea_wildlife_sighting", "updated_at": "2026-09-30T00:05:00Z",
                         "notes": [{"id": "dn1", "text": "hello"}], "files": [{"id": "df1", "filename": "a.jpg"}],
                         "is_contained_in": [], "event_details": provenance("e1", SOURCE_URL)}
    report = await sync(run, dest)
    assert report.updated == 1 and report.created == 0
    assert len(dest.events["e1"]["notes"]) == 1 and len(dest.events["e1"]["files"]) == 1
    assert not source.calls_to("download_file")


@pytest.mark.asyncio
async def test_recreates_copy_deleted_at_destination():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", notes=[{"id": "n1", "text": "a"}]))
    await sync(run, dest)
    del dest.events["e1"]
    source.events["e1"]["updated_at"] = "2026-10-02T00:00:00Z"
    report = await sync(run, dest)
    assert report.created == 1 and report.failed == 0
    assert [n["text"] for n in dest.events["e1"]["notes"]] == ["a"]


@pytest.mark.asyncio
async def test_rejected_event_is_reported_and_skipped():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    dest.failures["post_event"] = lambda event: (
        IntegrationBadResponseError("Destination site siteb.pamdas.org: bad location", status_code=400)
        if event["id"] == "e1" else None)
    report = await sync(run, dest)
    assert report.failed == 1 and report.created == 1
    assert list(report.errors) == [sync_events.REJECTED]
    assert (await state.get_marker(EVENTS_ACTION, marker_key("d1"))).since == parse_dt("2026-10-02T00:00:00Z")


@pytest.mark.asyncio
async def test_event_type_failure_only_affects_that_type():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", event_type="unknown_v1_type"),
                                     make_event("e2", "2026-10-02T00:00:00Z"))
    dest.failures["post_event_type_v2"] = lambda data: (
        IntegrationBadResponseError("Destination site siteb.pamdas.org: bad schema", status_code=400)
        if data["value"].endswith("fire") else None)
    source.event_types["fire"] = {**wildlife_type(), "value": "fire"}
    source.events["e3"] = make_event("e3", "2026-10-03T00:00:00Z", event_type="fire")
    report = await sync(run, dest)
    assert report.failed == 2 and report.created == 1
    group = report.errors[TYPE_NOT_READY]
    assert list(report.errors) == [TYPE_NOT_READY] and group.count == 2
    assert "unknown_v1_type is missing at the destination" in group.examples[0] and "bad schema" in group.examples[1]


@pytest.mark.asyncio
async def test_filtered_and_echoed_events_are_skipped():
    config = SyncEventsConfig.parse_obj({"start_from": "2026-09-01T00:00:00Z", "event_types": ["wildlife_sighting"]})
    echoed = make_event("e3", "2026-10-03T00:00:00Z",
                        event_details={"er2er_src_service_root": "https://siteb.pamdas.org/api/v1.0"})
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", event_type="fire"),
                                     make_event("e2", "2026-10-02T00:00:00Z"), echoed, config=config)
    report = await sync(run, dest)
    assert set(dest.events) == {"e2"} and report.skipped == 2


@pytest.mark.asyncio
async def test_halting_destination_error_keeps_progress_up_to_last_success():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    dest.failures["post_event"] = lambda event: (
        IntegrationConnectionError("Destination site siteb.pamdas.org: down") if event["id"] == "e2" else None)
    with pytest.raises(IntegrationConnectionError):
        await sync(run, dest)
    assert (await state.get_marker(EVENTS_ACTION, marker_key("d1"))).since == parse_dt("2026-10-01T00:00:00Z")
    assert await state.get_event_record("d1", "e1") is not None


@pytest.mark.asyncio
async def test_out_of_time_stops_and_flags_continuation():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), deadline=time.monotonic() - 1)
    report = await sync(run, dest)
    assert report.timed_out and not dest.events


@pytest.mark.asyncio
async def test_notes_copied_before_a_failure_are_not_posted_again():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", notes=[{"id": "n1", "text": "a"}],
                                                is_contained_in=[{"type": "contains", "related_event": {"id": "inc"}}]))
    dest.failures["add_to_incident"] = IntegrationConnectionError("Destination site siteb.pamdas.org: down")
    with pytest.raises(IntegrationConnectionError):
        await sync(run, dest)
    assert (await state.get_event_record("d1", "e1")).updated_at < parse_dt("2026-10-01T00:00:00Z")
    del dest.failures["add_to_incident"]
    source.events["e1"]["updated_at"] = "2026-10-02T00:00:00Z"
    await sync(run, dest)
    assert [n["text"] for n in dest.events["e1"]["notes"]] == ["a"]


@pytest.mark.asyncio
async def test_failed_event_stays_due_after_a_partial_copy():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", notes=[{"id": "n1", "text": "a"}],
                                                files=[{"id": "f1", "url": "u1", "filename": "a.jpg"}]))
    source.files["u1"] = b"img"
    dest.failures["post_file"] = IntegrationBadResponseError("Destination site siteb.pamdas.org: no", status_code=400)
    report = await sync(run, dest)
    assert report.failed == 1
    record = await state.get_event_record("d1", "e1")
    assert set(record.notes) == {"n1"} and record.updated_at < parse_dt("2026-10-01T00:00:00Z")


@pytest.mark.asyncio
async def test_rejections_group_under_one_title_with_details_in_examples():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    dest.failures["post_event"] = lambda event: IntegrationBadResponseError(
        f"Destination site siteb.pamdas.org: bad {event['id']}", status_code=400)
    report = await sync(run, dest)
    group = report.errors[sync_events.REJECTED]
    assert len(report.errors) == 1 and group.count == 2
    assert "bad e1" in group.examples[0] and "bad e2" in group.examples[1]


@pytest.mark.asyncio
async def test_one_broken_attachment_is_skipped_and_the_event_still_completes():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", notes=[{"id": "n1", "text": "a"}],
                                                files=[{"id": "f1", "url": "u1", "filename": "a.jpg"}]))
    err = IntegrationBadResponseError("Source site sitea.pamdas.org: the attachment wasn't found", status_code=404)
    err.site_role = "source"
    source.failures["download_file"] = err
    report = await sync(run, dest)
    assert report.failed == 0 and report.created == 1
    assert [n["text"] for n in dest.events["e1"]["notes"]] == ["a"]
    assert len(report.errors) == 1 and "a.jpg" in next(iter(report.errors.values())).examples[0]


@pytest.mark.asyncio
async def test_unexpected_error_in_one_event_spares_the_others():
    broken = make_event("e1", "2026-10-01T00:00:00Z")
    del broken["event_type"]
    source, dest, state, run = setup(broken, make_event("e2", "2026-10-02T00:00:00Z"))
    report = await sync(run, dest)
    assert report.failed == 1 and report.created == 1 and "e2" in dest.events
    assert list(report.errors) == [sync_events.UNEXPECTED]


@pytest.mark.asyncio
async def test_same_uuid_event_from_another_site_is_not_overwritten():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", state="resolved"),
                                     make_event("e2", "2026-10-02T00:00:00Z"))
    dest.events["e1"] = {"id": "e1", "state": "new", "notes": [], "files": [], "is_contained_in": [],
                         "event_details": provenance("e1", "https://sitec.pamdas.org", system="sitec")}
    dest.events["e2"] = {"id": "e2", "state": "new", "notes": [], "files": [], "is_contained_in": [],
                         "event_details": {}}  # the destination's own original
    report = await sync(run, dest)
    assert not dest.calls_to("patch_event") and dest.events["e1"]["state"] == "new"
    assert report.skipped == 2 and report.updated == 0
    assert report.errors[sync_events.FOREIGN_COPY].count == 2


@pytest.mark.asyncio
async def test_an_attachment_the_source_forbids_is_skipped_and_the_event_completes():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z",
                                                files=[{"id": "f1", "url": "u1", "filename": "a.jpg"}]),
                                     make_event("e2", "2026-10-02T00:00:00Z"))
    real_source = ERSite(MagicMock(), SOURCE_URL, role="source")
    source.failures["download_file"] = real_source._attachment_failure(403)
    report = await sync(run, dest)
    assert set(dest.events) == {"e1", "e2"} and report.failed == 0
    record = await state.get_event_record("d1", "e1")
    assert record.updated_at == parse_dt("2026-10-01T00:00:00Z") and "f1" not in record.files
    (group,) = report.errors.values()
    assert "a.jpg" in group.examples[0] and "didn't allow" in group.examples[0]
    assert (await state.get_marker(EVENTS_ACTION, marker_key("d1"))).since == parse_dt("2026-10-02T00:00:00Z")


@pytest.mark.asyncio
async def test_state_store_failure_stops_the_run_instead_of_skipping_the_event():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    calls = []

    async def flaky_get_event_record(destination_id, src_event_id):
        calls.append(src_event_id)
        if src_event_id == "e2":
            raise RedisConnectionError("down")
        return None

    state.get_event_record = flaky_get_event_record
    with pytest.raises(RedisConnectionError):
        await sync(run, dest)
    assert (await state.get_marker(EVENTS_ACTION, marker_key("d1"))).since == parse_dt("2026-10-01T00:00:00Z")


@pytest.mark.asyncio
async def test_adopts_an_old_v1_copy_recorded_without_a_service_root():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", state="resolved"))
    dest.events["e1"] = {"id": "e1", "state": "new", "notes": [], "files": [], "is_contained_in": [],
                         "event_details": {"er2er_src_id": "e1", "er2er_src_system": "sitea"}}
    report = await sync(run, dest)
    assert report.updated == 1 and not report.errors and dest.events["e1"]["state"] == "resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize("details", [
    {"er2er_src_id": "e1", "er2er_src_system": "sitec"},
    {"er2er_src_id": "other", "er2er_src_system": "sitea"},
    {"er2er_src_system": "sitea"},
])
async def test_old_v1_copy_without_a_service_root_must_match_id_and_system(details):
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", state="resolved"))
    dest.events["e1"] = {"id": "e1", "state": "new", "notes": [], "files": [], "is_contained_in": [],
                         "event_details": details}
    report = await sync(run, dest)
    assert not dest.calls_to("patch_event") and report.errors[sync_events.FOREIGN_COPY].count == 1


def test_source_failed_title_is_gone():
    assert not hasattr(sync_events, "SOURCE_FAILED")


@pytest.mark.asyncio
async def test_copy_edited_at_destination_after_the_source_change_is_left_alone():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"))
    await sync(run, dest)
    source.events["e1"].update(state="resolved", updated_at="2026-10-03T00:00:00Z")
    dest.events["e1"].update(title="edited at Site B", updated_at="2026-10-04T00:00:00Z")
    report = await sync(run, dest)
    assert report.skipped == 1 and report.updated == 0
    assert dest.events["e1"]["title"] == "edited at Site B"
    source.events["e1"]["updated_at"] = "2026-10-05T00:00:00Z"  # a later source change wins again
    report = await sync(run, dest)
    assert report.updated == 1 and dest.events["e1"]["state"] == "resolved"


@pytest.mark.asyncio
async def test_v1_copy_newer_than_the_source_change_is_not_rewritten():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"))
    dest.events["e1"] = {"id": "e1", "event_type": "sitea_wildlife_sighting", "updated_at": "2026-10-01T00:05:00Z",
                         "notes": [], "files": [], "is_contained_in": [], "event_details": provenance("e1", SOURCE_URL)}
    report = await sync(run, dest)
    assert report.skipped == 1 and not dest.calls_to("patch_event")
