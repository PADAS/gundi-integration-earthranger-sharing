from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.actions.configurations import AuthenticateConfig, SyncObservationsConfig
from app.ersharing import sync_observations
from app.ersharing.destinations import Destination
from app.ersharing.state import OBSERVATIONS_ACTION, ShareState
from app.ersharing.sync_observations import run_sync_observations, select_subjects, subject_marker_key
from app.ersharing.tests.fakes import FakeSite, InMemoryStateManager, make_observation
from app.ersharing.timeutil import parse_dt
from app.services.errors import IntegrationAuthError, IntegrationBadResponseError, IntegrationConnectionError

SOURCE_URL = "https://sitea.pamdas.org"
INTEGRATION = SimpleNamespace(
    id="i1", base_url=SOURCE_URL,
    configurations=[SimpleNamespace(action=SimpleNamespace(value="auth"), data={"token": "t"})],
)
CONFIG = SyncObservationsConfig.parse_obj({"start_from": "2026-09-30T00:00:00Z"})
LION = {"id": "s1", "name": "Lion 1", "subject_type": "wildlife", "subject_subtype": "lion"}
ZEBRA = {"id": "s2", "name": "Zebra 1", "subject_type": "wildlife", "subject_subtype": "zebra"}
NOW = parse_dt("2026-10-01T06:00:00Z")


def destination(n):
    return Destination(f"d{n}", f"Site {n}", f"https://site{n}.pamdas.org", AuthenticateConfig(token="t"))


def observed(source_id, recorded_at, **kwargs):
    # ER reports created_at; without it is_new() can't tell a repeat from a late arrival.
    return make_observation(source_id, recorded_at, created_at=recorded_at, **kwargs)


@pytest.fixture
def world(monkeypatch):
    source = FakeSite(SOURCE_URL, role="source")
    source.subject_groups = [{"id": "g1", "name": "Lions", "subjects": [LION]},
                             {"id": "g2", "name": "Zebras", "subjects": [ZEBRA, LION]}]
    source.assignments = [{"subject": s, "source": f"src-{s}",
                           "assigned_range": {"lower": "2026-01-01T00:00:00Z", "upper": None}} for s in ("s1", "s2")]
    source.observations = {
        "s1": [observed("src-s1", "2026-10-01T00:00:00Z"), observed("src-s1", "2026-10-01T01:00:00Z"),
               observed("src-s1", "2026-10-01T02:00:00Z", lat=0, lon=0)],
        "s2": [observed("src-s2", "2026-10-01T00:00:00Z")],
    }
    sites = {SOURCE_URL: source, "https://site1.pamdas.org": FakeSite("https://site1.pamdas.org"),
             "https://site2.pamdas.org": FakeSite("https://site2.pamdas.org")}
    manager, published = InMemoryStateManager(), []
    monkeypatch.setattr(sync_observations, "open_site", lambda url, auth, role: sites[url])
    monkeypatch.setattr(sync_observations, "ShareState",
                        lambda integration_id: ShareState(integration_id, manager=manager))
    monkeypatch.setattr(sync_observations, "resolve_destinations",
                        AsyncMock(return_value=([destination(1), destination(2)], [])))
    monkeypatch.setattr(sync_observations, "publish_report",
                        AsyncMock(side_effect=lambda i, a, r: published.append(r)))
    monkeypatch.setattr(sync_observations, "log_action_activity", AsyncMock())
    monkeypatch.setattr(sync_observations, "trigger_action", AsyncMock())
    monkeypatch.setattr(sync_observations, "utcnow", lambda: NOW)
    return SimpleNamespace(source=source, sites=sites, manager=manager, published=published)


def marker_of(world, d_id, subject_id):
    return ShareState("i1", manager=world.manager).get_marker(OBSERVATIONS_ACTION, subject_marker_key(d_id, subject_id))


def source_error(cls, status_code, subject_id="s1"):
    def fail(sid, start):
        if sid != subject_id:
            return None
        err = cls("Source site sitea.pamdas.org: no", status_code=status_code)
        err.site_role = "source"
        return err
    return fail


def points(site):
    return [(p["subject_id"], p["recorded_at"]) for _, batch in site.sharing_posts for p in batch]


@pytest.mark.asyncio
async def test_select_subjects_dedupes_and_filters_by_group(world):
    assert [s["id"] for s in await select_subjects(world.source, frozenset())] == ["s1", "s2"]
    assert [s["id"] for s in await select_subjects(world.source, frozenset({"g1"}))] == ["s1"]


@pytest.mark.asyncio
async def test_shares_valid_points_with_every_destination_once(world):
    await run_sync_observations(INTEGRATION, CONFIG)
    expected = [("s1", "2026-10-01T00:00:00Z"), ("s1", "2026-10-01T01:00:00Z"), ("s2", "2026-10-01T00:00:00Z")]
    for url in ("https://site1.pamdas.org", "https://site2.pamdas.org"):
        assert points(world.sites[url]) == expected
    assert len(world.source.calls_to("get_observations")) == 2  # one read per subject, not per destination
    await run_sync_observations(INTEGRATION, CONFIG)
    assert points(world.sites["https://site1.pamdas.org"]) == expected  # nothing re-sent
    assert [r.created for r in world.published[:2]] == [3, 3]


@pytest.mark.asyncio
async def test_markers_are_per_destination_and_subject(world):
    await run_sync_observations(INTEGRATION, CONFIG)
    # The (0, 0) point at 02:00 is never shared but still read, so the marker moves past it.
    assert (await marker_of(world, "d1", "s1")).since == parse_dt("2026-10-01T02:00:00Z")
    assert (await marker_of(world, "d1", "s2")).since == parse_dt("2026-10-01T00:00:00Z")


@pytest.mark.asyncio
async def test_unreachable_destination_is_dropped_for_the_run_and_others_continue(world):
    world.sites["https://site1.pamdas.org"].failures["post_sharing_status"] = IntegrationConnectionError(
        "Destination site site1.pamdas.org: down")
    await run_sync_observations(INTEGRATION, CONFIG)
    assert len(world.sites["https://site1.pamdas.org"].calls_to("post_sharing_status")) == 1
    assert len(points(world.sites["https://site2.pamdas.org"])) == 3
    state = ShareState("i1", manager=world.manager)
    assert await state.get_marker(OBSERVATIONS_ACTION, subject_marker_key("d1", "s1")) is None


@pytest.mark.asyncio
async def test_rejected_batch_is_reported_and_skipped(world):
    bad = "2026-10-01T01:00:00Z"
    site1 = world.sites["https://site1.pamdas.org"]
    site1.failures["post_sharing_status"] = lambda key, payloads: (
        IntegrationBadResponseError("Destination site site1.pamdas.org: bad", status_code=400)
        if any(p["recorded_at"] == bad and p["subject_id"] == "s1" for p in payloads) else None)
    await run_sync_observations(INTEGRATION, CONFIG)
    report = world.published[0]
    assert report.failed == 1 and report.created == 2
    assert points(site1) == [("s1", "2026-10-01T00:00:00Z"), ("s2", "2026-10-01T00:00:00Z")]
    assert report.errors[sync_observations.REJECTED].examples == [
        f"Lion 1 at {bad}: Destination site site1.pamdas.org: bad"]
    assert (await marker_of(world, "d1", "s1")).since == parse_dt("2026-10-01T02:00:00Z")


@pytest.mark.asyncio
async def test_batch_the_destination_cannot_take_holds_the_marker(world):
    world.sites["https://site1.pamdas.org"].failures["post_sharing_status"] = IntegrationBadResponseError(
        "Destination site site1.pamdas.org: odd", status_code=404)
    await run_sync_observations(INTEGRATION, CONFIG)
    assert len(world.sites["https://site1.pamdas.org"].calls_to("post_sharing_status")) == 2  # no splitting
    assert world.published[0].failed == 3
    assert await marker_of(world, "d1", "s1") is None
    assert len(points(world.sites["https://site2.pamdas.org"])) == 3


@pytest.mark.asyncio
async def test_new_subject_backfills_twelve_hours_not_from_start_from(world):
    world.source.observations["s2"] = [observed("src-s2", "2026-09-30T12:00:00Z"),
                                       observed("src-s2", "2026-10-01T05:00:00Z")]
    await run_sync_observations(INTEGRATION, CONFIG)
    assert [p for p in points(world.sites["https://site1.pamdas.org"]) if p[0] == "s2"] == [
        ("s2", "2026-10-01T05:00:00Z")]
    assert world.source.calls_to("get_observations")[1] == ("s2", NOW - timedelta(hours=12))


@pytest.mark.asyncio
async def test_subject_with_nothing_shareable_still_moves_its_marker(world):
    world.source.assignments = [a for a in world.source.assignments if a["subject"] != "s2"]
    await run_sync_observations(INTEGRATION, CONFIG)
    assert [p for p in points(world.sites["https://site1.pamdas.org"]) if p[0] == "s2"] == []
    assert (await marker_of(world, "d1", "s2")).since == parse_dt("2026-10-01T00:00:00Z")


@pytest.mark.asyncio
async def test_source_denying_one_subject_skips_only_that_subject(world):
    world.source.failures["get_observations"] = source_error(IntegrationAuthError, 403)
    await run_sync_observations(INTEGRATION, CONFIG)
    assert points(world.sites["https://site1.pamdas.org"]) == [("s2", "2026-10-01T00:00:00Z")]
    group = world.published[0].errors[sync_observations.SOURCE_DENIED]
    assert group.count == 1 and group.examples == ["Lion 1: Source site sitea.pamdas.org: no"]


@pytest.mark.asyncio
async def test_source_rejecting_the_credentials_stops_the_run(world):
    world.source.failures["get_observations"] = source_error(IntegrationAuthError, 401)
    with pytest.raises(IntegrationAuthError):
        await run_sync_observations(INTEGRATION, CONFIG)


@pytest.mark.asyncio
async def test_stall_warning_when_errors_persist(world, monkeypatch):
    world.source.failures["get_observations"] = source_error(IntegrationBadResponseError, 400, subject_id="s2")
    monkeypatch.setattr(sync_observations, "is_stalled", lambda marker, now: marker is not None)
    await run_sync_observations(INTEGRATION, CONFIG)
    titles = [c.kwargs["title"] for c in sync_observations.log_action_activity.await_args_list]
    assert "Tracks → Site 1: no progress since 2026-10-01 06:00 UTC. Check the errors above." in titles


@pytest.mark.asyncio
async def test_tracks_area_filter(world):
    world.source.feature_groups["fg1"] = {"id": "fg1", "features": [{"features": [{"type": "Feature", "geometry": {
        "type": "Polygon", "coordinates": [[[5, 5], [5, 6], [6, 6], [6, 5], [5, 5]]]}}]}]}
    world.source.observations["s2"].append(make_observation("src-s2", "2026-10-01T03:00:00Z", lat=5.5, lon=5.5))
    config = SyncObservationsConfig.parse_obj({"start_from": "2026-09-30T00:00:00Z", "area_feature_groups": ["fg1"]})
    await run_sync_observations(INTEGRATION, config)
    assert points(world.sites["https://site1.pamdas.org"]) == [("s2", "2026-10-01T03:00:00Z")]


@pytest.mark.asyncio
async def test_out_of_time_resumes_after_the_last_subject(world, monkeypatch):
    clock = iter([0, 0, 10 ** 9])  # deadline, s1 in time, s2 out of time
    monkeypatch.setattr(sync_observations, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    await run_sync_observations(INTEGRATION, CONFIG)
    sync_observations.trigger_action.assert_awaited_once_with("i1", OBSERVATIONS_ACTION)
    assert all(r.timed_out for r in world.published)
    assert {p[0] for p in points(world.sites["https://site1.pamdas.org"])} == {"s1"}

    monkeypatch.setattr(sync_observations, "time", SimpleNamespace(monotonic=lambda: 0))
    world.source.calls.clear()
    await run_sync_observations(INTEGRATION, CONFIG)
    assert [args[0] for args in world.source.calls_to("get_observations")] == ["s2"]
    assert {p[0] for p in points(world.sites["https://site1.pamdas.org"])} == {"s1", "s2"}
    assert sync_observations.trigger_action.await_count == 1

    world.source.calls.clear()
    await run_sync_observations(INTEGRATION, CONFIG)
    assert [args[0] for args in world.source.calls_to("get_observations")] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_batch_with_every_point_turned_down_holds_the_marker_and_is_reported_once(world):
    world.sites["https://site1.pamdas.org"].failures["post_sharing_status"] = IntegrationBadResponseError(
        "Destination site site1.pamdas.org: bad provider", status_code=400)
    await run_sync_observations(INTEGRATION, CONFIG)
    report = world.published[0]
    assert report.failed == 3 and report.created == 0
    assert report.errors[sync_observations.ALL_REJECTED].count == 1
    assert report.errors[sync_observations.ALL_REJECTED].examples == [
        "Lion 1: Destination site site1.pamdas.org: bad provider"]
    assert report.errors[sync_observations.REJECTED].count == 1  # s2's lone point can't tell batch from point
    assert await marker_of(world, "d1", "s1") is None


@pytest.mark.asyncio
async def test_stall_warning_for_a_destination_that_never_took_a_point(world, monkeypatch):
    world.sites["https://site1.pamdas.org"].failures["post_sharing_status"] = IntegrationBadResponseError(
        "Destination site site1.pamdas.org: odd", status_code=404)
    await run_sync_observations(INTEGRATION, CONFIG)
    titles = lambda: [c.kwargs["title"] for c in sync_observations.log_action_activity.await_args_list]
    assert not any("no progress" in t for t in titles())  # not yet past the stall window
    later = NOW + timedelta(hours=7)
    monkeypatch.setattr(sync_observations, "utcnow", lambda: later)
    await run_sync_observations(INTEGRATION, CONFIG)
    assert "Tracks → Site 1: no progress since 2026-10-01 06:00 UTC. Check the errors above." in titles()
    assert not any(t.startswith("Tracks → Site 2: no progress") for t in titles())
    del world.sites["https://site1.pamdas.org"].failures["post_sharing_status"]
    await run_sync_observations(INTEGRATION, CONFIG)
    state = ShareState("i1", manager=world.manager)
    assert await state.get_marker(OBSERVATIONS_ACTION, sync_observations.waiting_key("d1")) is None


def bulk_points(n, source_id="src-s1"):
    start = parse_dt("2026-10-01T00:00:00Z")
    return [observed(source_id, (start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")) for i in range(n)]


def reject_when(site, predicate):
    site.failures["post_sharing_status"] = lambda key, payloads: (
        IntegrationBadResponseError("Destination site site1.pamdas.org: bad", status_code=400)
        if predicate(payloads) else None)


@pytest.mark.asyncio
async def test_a_batch_level_rejection_costs_at_most_seven_posts_and_holds_the_marker(world):
    world.source.observations = {"s1": bulk_points(200), "s2": []}
    site1 = world.sites["https://site1.pamdas.org"]
    reject_when(site1, lambda payloads: True)
    await run_sync_observations(INTEGRATION, CONFIG)
    assert len(site1.calls_to("post_sharing_status")) <= sync_observations.PROBE_REJECTIONS
    report = world.published[0]
    assert report.failed == 200 and report.created == 0
    assert report.errors[sync_observations.ALL_REJECTED].count == 1
    assert sync_observations.REJECTED not in report.errors
    assert await marker_of(world, "d1", "s1") is None


@pytest.mark.asyncio
async def test_a_batch_whose_last_points_are_invalid_sends_the_rest_and_advances(world):
    obs = bulk_points(200)
    world.source.observations = {"s1": obs, "s2": []}
    bad = {obs[-1]["recorded_at"], obs[-2]["recorded_at"]}
    site1 = world.sites["https://site1.pamdas.org"]
    reject_when(site1, lambda payloads: any(p["recorded_at"] in bad for p in payloads))
    await run_sync_observations(INTEGRATION, CONFIG)
    report = world.published[0]
    assert report.created == 198 and report.failed == 2
    assert {p[1] for p in points(site1)} == {o["recorded_at"] for o in obs} - bad
    assert report.errors[sync_observations.REJECTED].count == 2
    assert sync_observations.ALL_REJECTED not in report.errors
    assert (await marker_of(world, "d1", "s1")).since == parse_dt(obs[-1]["recorded_at"])


@pytest.mark.asyncio
async def test_points_rejected_before_the_destination_stops_taking_the_rest_are_still_reported(world):
    site1 = world.sites["https://site1.pamdas.org"]
    first, second = "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z"

    def fail(key, payloads):
        times = [p["recorded_at"] for p in payloads if p["subject_id"] == "s1"]
        if len(times) == 2 or times == [first]:
            return IntegrationBadResponseError("Destination site site1.pamdas.org: bad", status_code=400)
        if times == [second]:
            return IntegrationBadResponseError("Destination site site1.pamdas.org: odd", status_code=404)
    site1.failures["post_sharing_status"] = fail
    await run_sync_observations(INTEGRATION, CONFIG)
    errors = world.published[0].errors
    assert errors[sync_observations.REJECTED].examples == [
        f"Lion 1 at {first}: Destination site site1.pamdas.org: bad"]
    assert errors[sync_observations.NOT_ACCEPTED].count == 1
    assert await marker_of(world, "d1", "s1") is None


@pytest.mark.asyncio
async def test_points_rejected_before_the_destination_goes_down_are_still_reported(world):
    site1 = world.sites["https://site1.pamdas.org"]
    first, second = "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z"

    def fail(key, payloads):
        times = [p["recorded_at"] for p in payloads if p["subject_id"] == "s1"]
        if len(times) == 2 or times == [first]:
            return IntegrationBadResponseError("Destination site site1.pamdas.org: bad", status_code=400)
        if times == [second]:
            return IntegrationConnectionError("Destination site site1.pamdas.org: down")
    site1.failures["post_sharing_status"] = fail
    await run_sync_observations(INTEGRATION, CONFIG)
    report = world.published[0]
    assert report.errors[sync_observations.REJECTED].count == 1
    assert report.failed == 1


@pytest.mark.asyncio
async def test_a_failing_publish_for_one_destination_does_not_skip_the_others(world, monkeypatch):
    published = []

    async def publish(i, a, report):
        if report.destination == "Site 1":
            raise RuntimeError("redis down")
        published.append(report)
    monkeypatch.setattr(sync_observations, "publish_report", publish)
    result = await run_sync_observations(INTEGRATION, CONFIG)
    assert [r.destination for r in published] == ["Site 2"]
    assert len(result["destinations"]) == 2


@pytest.mark.asyncio
async def test_a_single_bad_first_point_is_skipped_and_the_rest_are_sent(world):
    obs = bulk_points(200)
    world.source.observations = {"s1": obs, "s2": []}
    bad = obs[0]["recorded_at"]
    site1 = world.sites["https://site1.pamdas.org"]
    reject_when(site1, lambda payloads: any(p["recorded_at"] == bad for p in payloads))
    await run_sync_observations(INTEGRATION, CONFIG)
    report = world.published[0]
    assert report.created == 199 and report.failed == 1
    assert bad not in {p[1] for p in points(site1)} and len(points(site1)) == 199
    assert sync_observations.ALL_REJECTED not in report.errors
    assert (await marker_of(world, "d1", "s1")).since == parse_dt(obs[-1]["recorded_at"])
