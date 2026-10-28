import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.actions.configurations import AuthenticateConfig, SyncEventsConfig
from app.ersharing import sync_events
from app.ersharing.destinations import Destination, SkippedDestination
from app.ersharing.modes import event_settings
from app.ersharing.reporting import DestinationReport
from app.ersharing.state import EVENTS_ACTION, Marker, ShareState
from app.ersharing.sync_events import EventsRun, marker_key, reconcile_destination, run_sync_events
from app.ersharing.tests.fakes import FakeSite, InMemoryStateManager, make_event
from app.ersharing.timeutil import parse_dt, utcnow
from app.services.errors import IntegrationAuthError, IntegrationConnectionError

SOURCE_URL = "https://sitea.pamdas.org"
INTEGRATION = SimpleNamespace(
    id="i1", base_url=SOURCE_URL,
    configurations=[SimpleNamespace(action=SimpleNamespace(value="auth"), data={"token": "t"})],
)
CONFIG = SyncEventsConfig.parse_obj({"start_from": "2026-09-01T00:00:00Z"})


def provenance(src_id, system="sitea", site_url=SOURCE_URL):
    return {"er2er_src_id": src_id, "er2er_src_system": system, "er2er_src_service_root": f"{site_url}/api/v1.0"}


def reconcile_run(source, deadline=None):
    state = ShareState("i1", manager=InMemoryStateManager())
    return EventsRun("i1", source, event_settings(SOURCE_URL, CONFIG), None, state, deadline or time.monotonic() + 60)


def destination(n):
    return Destination(f"d{n}", f"Site {n}", f"https://site{n}.pamdas.org", AuthenticateConfig(token="t"))


@pytest.fixture
def world(monkeypatch):
    source = FakeSite(SOURCE_URL, role="source")
    source.event_types["wildlife_sighting"] = {"value": "wildlife_sighting", "display": "W", "category": "c",
                                               "schema": {}}
    source.events["e1"] = make_event("e1", "2026-10-01T00:00:00Z")
    sites = {SOURCE_URL: source, "https://site1.pamdas.org": FakeSite("https://site1.pamdas.org"),
             "https://site2.pamdas.org": FakeSite("https://site2.pamdas.org")}
    manager = InMemoryStateManager()
    published, logs = [], AsyncMock()
    monkeypatch.setattr(sync_events, "open_site", lambda url, auth, role: sites[url])
    monkeypatch.setattr(sync_events, "ShareState", lambda integration_id: ShareState(integration_id, manager=manager))
    monkeypatch.setattr(sync_events, "resolve_destinations",
                        AsyncMock(return_value=([destination(1), destination(2)], [])))
    monkeypatch.setattr(sync_events, "publish_report", AsyncMock(side_effect=lambda i, a, r: published.append(r)))
    monkeypatch.setattr(sync_events, "log_action_activity", logs)
    monkeypatch.setattr(sync_events, "trigger_action", AsyncMock())
    return SimpleNamespace(source=source, sites=sites, manager=manager, published=published, logs=logs)


@pytest.mark.asyncio
async def test_syncs_every_destination_and_publishes_one_report_each(world):
    result = await run_sync_events(INTEGRATION, CONFIG)
    assert "e1" in world.sites["https://site1.pamdas.org"].events
    assert "e1" in world.sites["https://site2.pamdas.org"].events
    assert [r.destination for r in world.published] == ["Site 1", "Site 2"]
    assert len(result["destinations"]) == 2


@pytest.mark.asyncio
async def test_unreachable_destination_keeps_progress_and_spares_others(world):
    world.sites["https://site1.pamdas.org"].failures["get_event_type_v2"] = IntegrationConnectionError(
        "Destination site site1.pamdas.org: EarthRanger could not be reached")
    await run_sync_events(INTEGRATION, CONFIG)
    assert "e1" in world.sites["https://site2.pamdas.org"].events
    failed = world.published[0]
    assert "Destination site site1.pamdas.org: EarthRanger could not be reached" in failed.errors
    state = ShareState("i1", manager=world.manager)
    assert (await state.get_marker(EVENTS_ACTION, marker_key("d1"))).since == parse_dt("2026-09-01T00:00:00Z")


@pytest.mark.asyncio
async def test_source_failure_aborts_the_run(world):
    world.source.failures["iter_events_updated_since"] = IntegrationAuthError(
        "Source site sitea.pamdas.org: EarthRanger rejected the credentials")
    world.source.failures["iter_events_updated_since"].site_role = "source"
    with pytest.raises(IntegrationAuthError):
        await run_sync_events(INTEGRATION, CONFIG)


@pytest.mark.asyncio
async def test_overlapping_run_is_skipped(world):
    await ShareState("i1", manager=world.manager).try_lease(EVENTS_ACTION, 60)
    assert (await run_sync_events(INTEGRATION, CONFIG))["status"] == "skipped"


@pytest.mark.asyncio
async def test_lease_is_released_before_continuation_is_triggered(world, monkeypatch):
    monkeypatch.setattr(sync_events, "BUDGET_FRACTION", -1)  # out of time immediately
    lease_free = []

    async def trigger(integration_id, action_id):
        lease_free.append(await ShareState("i1", manager=world.manager).try_lease(EVENTS_ACTION, 60))

    monkeypatch.setattr(sync_events, "trigger_action", trigger)
    await run_sync_events(INTEGRATION, CONFIG)
    assert lease_free == [True]


@pytest.mark.asyncio
async def test_skipped_and_missing_destinations_are_logged(world, monkeypatch):
    monkeypatch.setattr(sync_events, "resolve_destinations",
                        AsyncMock(return_value=([], [SkippedDestination("x", "Smart", "not an EarthRanger destination")])))
    await run_sync_events(INTEGRATION, CONFIG)
    titles = [c.kwargs["title"] for c in world.logs.await_args_list]
    assert titles == ["Destination Smart skipped: not an EarthRanger destination",
                      "No EarthRanger destinations to share events with."]


@pytest.mark.asyncio
async def test_reconcile_deletes_copies_whose_source_is_gone():
    source, dest = FakeSite(SOURCE_URL, role="source"), FakeSite()
    source.events["kept"] = make_event("kept", "2026-10-01T00:00:00Z")
    for copy_id, src_id, system in (("kept", "kept", "sitea"), ("gone", "gone", "sitea"), ("other", "x", "sitec")):
        dest.events[copy_id] = make_event(copy_id, utcnow().isoformat(), event_details=provenance(src_id, system))
    state = ShareState("i1", manager=InMemoryStateManager())
    run = EventsRun("i1", source, event_settings(SOURCE_URL, CONFIG), None, state, time.monotonic() + 60)
    report = DestinationReport("Site B", "Events")
    assert await reconcile_destination(run, destination(1), dest, report) is True
    assert set(dest.events) == {"kept", "other"} and report.deleted == 1


@pytest.mark.asyncio
async def test_reconcile_runs_at_most_daily_and_respects_the_setting(world, monkeypatch):
    calls = AsyncMock(return_value=True)
    monkeypatch.setattr(sync_events, "reconcile_destination", calls)
    await run_sync_events(INTEGRATION, CONFIG)
    await run_sync_events(INTEGRATION, CONFIG)
    assert calls.await_count == 2  # once per destination, not again on the second run
    off = SyncEventsConfig.parse_obj({"start_from": "2026-09-01T00:00:00Z",
                                      "compatibility": {"delete_unmatched_events": False}})
    world.manager.data.clear()
    await run_sync_events(INTEGRATION, off)
    assert calls.await_count == 2


@pytest.mark.asyncio
async def test_stall_warning_when_errors_persist(world, monkeypatch):
    world.sites["https://site1.pamdas.org"].failures["get_event_type_v2"] = IntegrationConnectionError(
        "Destination site site1.pamdas.org: down")
    monkeypatch.setattr(sync_events, "is_stalled", lambda marker, now: True)
    await run_sync_events(INTEGRATION, CONFIG)
    assert any("no progress since" in c.kwargs["title"] for c in world.logs.await_args_list)


@pytest.mark.asyncio
async def test_reconcile_spares_copies_from_another_site_with_the_same_system_name():
    source, dest = FakeSite(SOURCE_URL, role="source"), FakeSite()
    dest.events["dev"] = make_event("dev", utcnow().isoformat(),
                                    event_details=provenance("dev", site_url="https://sitea.dev.pamdas.org"))
    report = DestinationReport("Site B", "Events")
    assert await reconcile_destination(reconcile_run(source), destination(1), dest, report) is True
    assert "dev" in dest.events and report.deleted == 0


@pytest.mark.asyncio
async def test_unfinished_reconcile_resumes_from_its_cursor():
    source, dest = FakeSite(SOURCE_URL, role="source"), FakeSite()
    first, second = utcnow() - timedelta(days=2), utcnow() - timedelta(days=1)
    dest.events["gone1"] = make_event("gone1", first.isoformat(), event_details=provenance("gone1"))
    dest.events["gone2"] = make_event("gone2", second.isoformat(), event_details=provenance("gone2"))
    run = reconcile_run(source)
    checks = iter([False, True])
    run.out_of_time = lambda: next(checks)
    report = DestinationReport("Site B", "Events")
    assert await reconcile_destination(run, destination(1), dest, report) is False
    assert set(dest.events) == {"gone2"}

    run.out_of_time = lambda: False
    assert await reconcile_destination(run, destination(1), dest, report) is True
    assert not dest.events
    assert dest.calls_to("iter_events_updated_since")[1] == (first,)
    assert await run.state.get_marker(EVENTS_ACTION, sync_events.reconcile_cursor_key("d1")) is None


@pytest.mark.asyncio
async def test_unfinished_reconcile_runs_after_every_sync_and_continues_next_run(world, monkeypatch):
    order = []
    world.sites["https://site1.pamdas.org"].failures["get_event_type_v2"] = lambda value: order.append("sync1")
    world.sites["https://site2.pamdas.org"].failures["get_event_type_v2"] = lambda value: order.append("sync2")

    async def unfinished(run, destination, dest, report):
        order.append(f"reconcile {destination.id}")
        return False

    monkeypatch.setattr(sync_events, "reconcile_destination", unfinished)
    await ShareState("i1", manager=world.manager).set_marker(
        EVENTS_ACTION, sync_events.reconcile_cursor_key("d1"), Marker(since=utcnow(), advanced_at=utcnow()))
    await ShareState("i1", manager=world.manager).set_marker(
        EVENTS_ACTION, sync_events.reconcile_cursor_key("d2"), Marker(since=utcnow(), advanced_at=utcnow()))
    await run_sync_events(INTEGRATION, CONFIG)
    await run_sync_events(INTEGRATION, CONFIG)
    assert order[:4] == ["sync1", "sync2", "reconcile d1", "reconcile d2"]
    assert order.count("reconcile d1") == 2  # an unfinished pass isn't held back by the daily gate
    assert not sync_events.trigger_action.await_count
    assert not any(r.timed_out for r in world.published)


@pytest.mark.asyncio
async def test_reconcile_keeps_a_copy_whose_original_the_source_wont_show_and_moves_on():
    source, dest = FakeSite(SOURCE_URL, role="source"), FakeSite()
    first, second = utcnow() - timedelta(days=2), utcnow() - timedelta(days=1)
    dest.events["hidden"] = make_event("hidden", first.isoformat(), event_details=provenance("hidden"))
    dest.events["gone"] = make_event("gone", second.isoformat(), event_details=provenance("gone"))
    denied = IntegrationAuthError("Source site sitea.pamdas.org: EarthRanger denied access with these credentials",
                                  status_code=403)
    denied.site_role = "source"
    source.failures["get_event"] = lambda event_id: denied if event_id == "hidden" else None
    run = reconcile_run(source)
    report = DestinationReport("Site B", "Events")
    assert await reconcile_destination(run, destination(1), dest, report) is True
    assert set(dest.events) == {"hidden"} and report.deleted == 1
    group = report.errors[sync_events.NOT_CHECKED]
    assert group.count == 1 and group.examples[0].startswith("hidden: ")


@pytest.mark.asyncio
async def test_a_source_failure_mid_destination_still_publishes_that_destinations_report(world):
    world.sites["https://site2.pamdas.org"].failures["get_event_type_v2"] = None
    down = IntegrationConnectionError("Source site sitea.pamdas.org: EarthRanger could not be reached")
    down.site_role = "source"
    calls = iter([None, down])
    world.source.failures["list_event_types_v2"] = lambda: next(calls)
    world.source.events["e2"] = make_event("e2", "2026-10-02T00:00:00Z", event_type="other")
    with pytest.raises(IntegrationConnectionError):
        await run_sync_events(INTEGRATION, CONFIG)
    assert [r.destination for r in world.published] == ["Site 1", "Site 2"]


@pytest.mark.asyncio
async def test_a_failing_publish_does_not_hide_the_original_error_or_skip_other_reports(world, monkeypatch):
    published = []

    async def publish(i, a, report):
        if report.destination == "Site 1":
            raise RuntimeError("redis down")
        published.append(report)
    monkeypatch.setattr(sync_events, "publish_report", publish)
    down = IntegrationConnectionError("Source site sitea.pamdas.org: EarthRanger could not be reached")
    down.site_role = "source"
    calls = iter([None, down])
    world.source.failures["list_event_types_v2"] = lambda: next(calls)
    world.source.events["e2"] = make_event("e2", "2026-10-02T00:00:00Z", event_type="other")
    with pytest.raises(IntegrationConnectionError):
        await run_sync_events(INTEGRATION, CONFIG)
    assert [r.destination for r in published] == ["Site 2"]
