import os

import pytest
from gundi_core.events import LogLevel

from app.ersharing.notes_files import (
    MAX_FILE_BYTES, TOO_BIG, adopt_record, apply_files, apply_incidents, apply_notes, note_digest, plan_files, plan_notes,
)
from app.ersharing.reporting import DestinationReport
from app.ersharing.state import EventRecord, NoteRecord
from app.ersharing.tests.fakes import FakeSite, make_event
from app.ersharing.timeutil import parse_dt
from app.services.errors import IntegrationBadResponseError, IntegrationConnectionError

T0 = parse_dt("2026-10-01T00:00:00Z")


def test_plan_notes_adds_updates_and_deletes():
    recorded = {"n1": NoteRecord(dest_id="d1", digest=note_digest("same")),
                "n2": NoteRecord(dest_id="d2", digest=note_digest("old")),
                "n3": NoteRecord(dest_id="d3", digest=note_digest("gone"))}
    plan = plan_notes([{"id": "n1", "text": "same"}, {"id": "n2", "text": "new"}, {"id": "n4", "text": "added"}],
                      recorded)
    assert [n["id"] for n in plan.add] == ["n4"]
    assert [(n["id"], d) for n, d in plan.update] == [("n2", "d2")]
    assert plan.delete == [("n3", "d3")]


def test_plan_files_adds_new_and_deletes_removed():
    plan = plan_files([{"id": "f1"}, {"id": "f2"}], {"f1": "df1", "f9": "df9", "f8": ""})
    assert [f["id"] for f in plan.add] == ["f2"]
    assert sorted(plan.delete) == [("f8", ""), ("f9", "df9")]


def test_adopt_record_matches_existing_notes_and_files_once():
    src = make_event("e1", "2026-10-01T00:00:00Z",
                     notes=[{"id": "n1", "text": "hello"}, {"id": "n2", "text": "hello"}],
                     files=[{"id": "f1", "filename": "a.jpg"}],
                     is_contained_in=[{"type": "contains", "related_event": {"id": "inc1"}}])
    dest = {"id": "e1", "notes": [{"id": "dn1", "text": "hello"}], "files": [{"id": "df1", "filename": "a.jpg"}],
            "is_contained_in": [{"type": "contains", "related_event": {"id": "inc1"}}]}
    record = adopt_record(src, dest)
    assert record.notes == {"n1": NoteRecord(dest_id="dn1", digest=note_digest("hello"))}
    assert record.files == {"f1": "df1"}
    assert record.incidents == ["inc1"]


@pytest.mark.asyncio
async def test_apply_notes_writes_and_records():
    dest = FakeSite()
    await dest.post_event({"id": "e1"})
    record = EventRecord(dest_event_id="e1", updated_at=T0)
    await apply_notes(dest, "e1", plan_notes([{"id": "n1", "text": "hi"}], record.notes), record)
    assert [n["text"] for n in dest.events["e1"]["notes"]] == ["hi"]
    await apply_notes(dest, "e1", plan_notes([{"id": "n1", "text": "edited"}], record.notes), record)
    assert [n["text"] for n in dest.events["e1"]["notes"]] == ["edited"]
    await apply_notes(dest, "e1", plan_notes([], record.notes), record)
    assert dest.events["e1"]["notes"] == [] and record.notes == {}


@pytest.mark.asyncio
async def test_note_edit_falls_back_to_replace_when_patch_unsupported():
    dest = FakeSite()
    await dest.post_event({"id": "e1"})
    record = EventRecord(dest_event_id="e1", updated_at=T0)
    await apply_notes(dest, "e1", plan_notes([{"id": "n1", "text": "hi"}], record.notes), record)
    dest.failures["patch_note"] = IntegrationBadResponseError("Destination site x: no", status_code=405)
    await apply_notes(dest, "e1", plan_notes([{"id": "n1", "text": "edited"}], record.notes), record)
    assert [n["text"] for n in dest.events["e1"]["notes"]] == ["edited"]
    assert record.notes["n1"].dest_id == dest.events["e1"]["notes"][0]["id"]


@pytest.mark.asyncio
async def test_apply_files_copies_and_skips_oversized_once():
    source, dest = FakeSite("https://sitea.pamdas.org", role="source"), FakeSite()
    source.files = {"u1": b"img", "u2": b"x" * (MAX_FILE_BYTES + 1)}
    await dest.post_event({"id": "e1"})
    record, report = EventRecord(dest_event_id="e1", updated_at=T0), DestinationReport("Site B", "Events")
    src_files = [{"id": "f1", "url": "u1", "filename": "a.jpg"}, {"id": "f2", "url": "u2", "filename": "big.mp4"}]
    await apply_files(source, dest, "e1", plan_files(src_files, record.files), record, report)
    assert [f["filename"] for f in dest.events["e1"]["files"]] == ["a.jpg"]
    assert record.files["f2"] == ""
    assert report.errors[TOO_BIG].level == LogLevel.WARNING
    assert report.errors[TOO_BIG].examples == ["big.mp4 wasn't copied because it's bigger than 20 MB."]
    await apply_files(source, dest, "e1", plan_files(src_files, record.files), record, report)
    assert len(source.calls_to("download_file")) == 2  # nothing re-downloaded


@pytest.mark.asyncio
async def test_apply_files_uploads_from_a_temp_file_that_is_deleted_afterwards():
    source, dest = FakeSite("https://sitea.pamdas.org", role="source"), FakeSite()
    source.files = {"u1": b"img"}
    await dest.post_event({"id": "e1"})
    record, report = EventRecord(dest_event_id="e1", updated_at=T0), DestinationReport("Site B", "Events")
    src_files = [{"id": "f1", "url": "u1", "filename": "a.jpg"}]
    await apply_files(source, dest, "e1", plan_files(src_files, record.files), record, report)
    assert dest.uploaded == {"a.jpg": b"img"}
    assert source.downloaded_paths and not any(os.path.exists(p) for p in source.downloaded_paths)


@pytest.mark.asyncio
async def test_apply_files_skips_a_file_the_source_wont_send_and_copies_the_rest():
    source, dest = FakeSite("https://sitea.pamdas.org", role="source"), FakeSite()
    source.files = {"u1": b"one", "u2": b"two"}
    source.failures["download_file"] = lambda url: (
        IntegrationBadResponseError("Source site x: the attachment wasn't found", status_code=404)
        if url == "u1" else None)
    await dest.post_event({"id": "e1"})
    record, report = EventRecord(dest_event_id="e1", updated_at=T0), DestinationReport("Site B", "Events")
    src_files = [{"id": "f1", "url": "u1", "filename": "a.jpg"}, {"id": "f2", "url": "u2", "filename": "b.jpg"}]
    await apply_files(source, dest, "e1", plan_files(src_files, record.files), record, report)
    assert list(dest.uploaded) == ["b.jpg"] and "f1" not in record.files
    (group,) = report.errors.values()
    assert group.level == LogLevel.WARNING and "a.jpg" in group.examples[0]


@pytest.mark.asyncio
async def test_apply_files_still_halts_when_the_source_is_unreachable():
    source, dest = FakeSite("https://sitea.pamdas.org", role="source"), FakeSite()
    source.files = {"u1": b"one"}
    source.failures["download_file"] = IntegrationConnectionError("Source site x: EarthRanger could not be reached")
    await dest.post_event({"id": "e1"})
    record, report = EventRecord(dest_event_id="e1", updated_at=T0), DestinationReport("Site B", "Events")
    with pytest.raises(IntegrationConnectionError):
        await apply_files(source, dest, "e1", plan_files([{"id": "f1", "url": "u1", "filename": "a"}], {}),
                          record, report)


@pytest.mark.asyncio
async def test_apply_incidents_links_unlinks_and_warns_on_unshared_incident():
    dest = FakeSite()
    for event_id in ("e1", "inc1"):
        await dest.post_event({"id": event_id})
    record, report = EventRecord(dest_event_id="e1", updated_at=T0), DestinationReport("Site B", "Events")
    await apply_incidents(dest, "e1", ["inc1", "missing"], record, report)
    assert record.incidents == ["inc1"]
    assert report.errors and list(report.errors.values())[0].level == LogLevel.WARNING
    await apply_incidents(dest, "e1", [], record, report)
    assert record.incidents == [] and dest.events["e1"]["is_contained_in"] == []
