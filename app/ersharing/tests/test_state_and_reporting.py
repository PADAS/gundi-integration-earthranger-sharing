from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from gundi_core.events import LogLevel

from app.ersharing import reporting
from app.ersharing.reporting import DestinationReport, is_stalled, publish_report
from app.ersharing.state import EVENTS_ACTION, EventRecord, Marker, NoteRecord, ShareState
from app.ersharing.tests.fakes import InMemoryStateManager

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_markers_and_event_records_round_trip():
    state = ShareState("i1", manager=InMemoryStateManager())
    assert await state.get_marker(EVENTS_ACTION, "d1:marker") is None
    await state.set_marker(EVENTS_ACTION, "d1:marker", Marker(since=T0, advanced_at=T0))
    assert (await state.get_marker(EVENTS_ACTION, "d1:marker")).since == T0
    await state.delete_marker(EVENTS_ACTION, "d1:marker")
    assert await state.get_marker(EVENTS_ACTION, "d1:marker") is None

    record = EventRecord(dest_event_id="e1", updated_at=T0, notes={"n1": NoteRecord(dest_id="dn1", digest="x")})
    await state.set_event_record("d1", "e1", record)
    assert await state.get_event_record("d1", "e1") == record
    await state.delete_event_record("d1", "e1")
    assert await state.get_event_record("d1", "e1") is None


@pytest.mark.asyncio
async def test_lease_and_once_per_gates():
    state = ShareState("i1", manager=InMemoryStateManager())
    assert await state.try_lease(EVENTS_ACTION, 60) is True
    assert await state.try_lease(EVENTS_ACTION, 60) is False
    await state.release_lease(EVENTS_ACTION)
    assert await state.try_lease(EVENTS_ACTION, 60) is True

    assert await state.once_per(EVENTS_ACTION, "d1:reconcile", 86400) is True
    assert await state.once_per(EVENTS_ACTION, "d1:reconcile", 86400) is False


def test_errors_are_grouped_by_title_with_a_few_examples():
    report = DestinationReport(destination="Site B", kind="Events")
    for i in range(7):
        report.error("EarthRanger rejected an event", item=f"#{i}")
    group = report.errors["EarthRanger rejected an event"]
    assert group.count == 7 and group.examples == ["#0", "#1", "#2", "#3", "#4"]


def test_summary_titles():
    report = DestinationReport(destination="Site B", kind="Events", created=2, updated=1, marker=T0)
    assert report.summary_title() == (
        "Events → Site B: 2 created, 1 updated, 0 deleted, 0 skipped, 0 failed; synced up to 2026-10-01 00:00 UTC"
    )
    tracks = DestinationReport(destination="Site B", kind="Tracks", created=40, timed_out=True)
    assert tracks.summary_title() == "Tracks → Site B: 40 points shared, 0 failed; continuing in a follow-up run"


@pytest.mark.asyncio
async def test_publish_report_logs_summary_then_one_entry_per_error_group(monkeypatch):
    log = AsyncMock()
    monkeypatch.setattr(reporting, "log_action_activity", log)
    report = DestinationReport(destination="Site B", kind="Events", failed=2)
    report.error("Type X couldn't be set up", item="#1")
    report.error("Type X couldn't be set up", item="#2")
    await publish_report("i1", EVENTS_ACTION, report)
    titles = [call.kwargs["title"] for call in log.await_args_list]
    levels = [call.kwargs["level"] for call in log.await_args_list]
    assert titles[1] == "Events → Site B: Type X couldn't be set up"
    assert levels == [LogLevel.WARNING, LogLevel.ERROR]
    assert log.await_args_list[1].kwargs["data"] == {"count": 2, "examples": ["#1", "#2"]}


def test_stall_detection():
    assert is_stalled(None, T0) is False
    assert is_stalled(Marker(since=T0, advanced_at=T0), T0 + timedelta(hours=5)) is False
    assert is_stalled(Marker(since=T0, advanced_at=T0), T0 + timedelta(hours=7)) is True


@pytest.mark.asyncio
async def test_release_leaves_a_lease_a_later_run_now_holds():
    manager = InMemoryStateManager()
    first, later = ShareState("i1", manager=manager), ShareState("i1", manager=manager)
    assert await first.try_lease(EVENTS_ACTION, 60) is True
    manager.data.clear()  # the first run's lease expired
    assert await later.try_lease(EVENTS_ACTION, 60) is True
    await first.release_lease(EVENTS_ACTION)
    assert await ShareState("i1", manager=manager).try_lease(EVENTS_ACTION, 60) is False
    await later.release_lease(EVENTS_ACTION)
    assert await ShareState("i1", manager=manager).try_lease(EVENTS_ACTION, 60) is True


@pytest.mark.asyncio
async def test_lease_release_is_a_single_atomic_compare_and_delete():
    manager = MagicMock()
    manager.set_if_absent = AsyncMock(return_value=True)
    manager.delete_if_equal = AsyncMock(return_value=True)
    state = ShareState("i1", manager=manager)
    await state.try_lease(EVENTS_ACTION, 60)
    stored = manager.set_if_absent.await_args.kwargs["value"]
    await state.release_lease(EVENTS_ACTION)
    manager.delete_if_equal.assert_awaited_once_with("i1", EVENTS_ACTION, stored, source_id="lease")
    assert not manager.get_state.called and not manager.delete_state.called

