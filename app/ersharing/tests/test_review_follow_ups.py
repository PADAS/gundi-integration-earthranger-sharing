"""Behaviour added after the 10 November review of the open questions (PR description,
"Decisions from review"): bounce guard for tracks, per-type refusals, destinations
with no credentials, catch-up of events skipped for their type, v1 types converted
to v2, and subject details kept up to date."""
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.actions.configurations import AuthenticateConfig, SyncEventsConfig
from app.ersharing import reporting, sync_events
from app.ersharing.destinations import Destination, SkippedDestination, resolve_destinations
from app.ersharing.event_types import EventTypeSync, has_provenance_fields
from app.ersharing.modes import event_settings
from app.ersharing.observations import passed_through
from app.ersharing.reporting import DestinationReport, log_skipped_destinations
from app.ersharing.state import OBSERVATIONS_ACTION, ShareState
from app.ersharing.sync_events import EventsRun, sync_destination
from app.ersharing.sync_observations import DETAILS_NOT_UPDATED, _refresh_details
from app.ersharing.tests.fakes import FakeSite, InMemoryStateManager, make_event, make_observation
from app.ersharing.tests.test_destinations import FakeGundi, dest, details
from app.services.errors import (
    IntegrationAuthError, IntegrationBadResponseError, IntegrationRateLimitError,
)

SOURCE_URL = "https://sitea.pamdas.org"
SETTINGS = event_settings(SOURCE_URL, SyncEventsConfig())
DESTINATION = Destination("d1", "Site B", "https://siteb.pamdas.org", AuthenticateConfig(token="t"))


# --- 13. Track points don't bounce back to a site they came from -------------------------

def test_a_point_that_passed_through_the_destination_is_held_back():
    stamped = make_observation("src", "2026-10-01T00:00:00Z", details={"sharing_lineage": [
        "site:siteb.pamdas.org#user:svc#subject:s1#rts:20261001000000",
        "site:sitea.pamdas.org#user:svc#subject:s1#rts:20261001000500",
    ]})
    assert passed_through(stamped, "siteb.pamdas.org")
    assert not passed_through(stamped, "sitec.pamdas.org")
    # A host that only starts the same way is a different site.
    assert not passed_through(stamped, "siteb.pamdas")
    assert not passed_through(make_observation("src", "2026-10-01T00:00:00Z"), "siteb.pamdas.org")


# --- 3. A refusal while setting up a type stops only that type ---------------------------

def type_sites():
    source = FakeSite(SOURCE_URL, role="source")
    source.event_types["wildlife_sighting"] = {
        "value": "wildlife_sighting", "display": "Wildlife", "category": {"value": "monitoring"},
        "schema": {"json": {"properties": {}}, "ui": {}}}
    source.categories["monitoring"] = {"value": "monitoring", "display": "Monitoring"}
    return source, FakeSite()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    IntegrationRateLimitError("Destination site x: EarthRanger rate-limited the request", status_code=409),
    IntegrationAuthError("Destination site x: EarthRanger denied access with these credentials", status_code=403),
    IntegrationBadResponseError("Destination site x: already exists", status_code=400),
])
async def test_a_refused_type_or_category_is_a_problem_with_that_type_only(error):
    source, dest = type_sites()
    dest.failures["post_event_type_v2"] = error
    sync = EventTypeSync(source, dest, SETTINGS)
    assert await sync.ensure("wildlife_sighting") is None
    assert error.message in sync.failures["wildlife_sighting"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    IntegrationRateLimitError("Destination site x: EarthRanger rate-limited the request", status_code=429),
    IntegrationAuthError("Destination site x: EarthRanger rejected the credentials", status_code=401),
])
async def test_a_rate_limit_or_rejected_token_still_stops_the_destination(error):
    source, dest = type_sites()
    dest.failures["post_event_category"] = error
    with pytest.raises(type(error)):
        await EventTypeSync(source, dest, SETTINGS).ensure("wildlife_sighting")


# --- 8. Destinations with no credentials are skipped, and said once every few hours -------

@pytest.mark.asyncio
@pytest.mark.parametrize("auth, reason", [
    ({"token": ""}, "it has no token in Gundi"),
    ({"token": "   ", "authentication_type": "token"}, "it has no token in Gundi"),
    ({"authentication_type": "username_password", "username": "u", "password": ""},
     "it has no username and password in Gundi"),
])
async def test_a_destination_with_empty_credentials_is_skipped_with_a_clear_reason(auth, reason):
    gundi = FakeGundi([dest("d1", "https://siteb.pamdas.org", name="Site B")],
                      {"d1": details("https://siteb.pamdas.org", auth)})
    destinations, skipped = await resolve_destinations("c1", SOURCE_URL, gundi=gundi)
    assert destinations == []
    assert [(s.name, s.reason) for s in skipped] == [("Site B", reason)]


@pytest.mark.asyncio
async def test_a_skipped_destination_is_logged_at_most_once_per_window(monkeypatch):
    logs = AsyncMock()
    monkeypatch.setattr(reporting, "log_action_activity", logs)
    state = ShareState("i1", manager=InMemoryStateManager())
    no_token = [SkippedDestination("d1", "Site B", "it has no token in Gundi")]
    await log_skipped_destinations("i1", "sync_events", state, no_token)
    await log_skipped_destinations("i1", "sync_events", state, no_token)
    # A different reason for the same destination is news, so it is logged straight away.
    await log_skipped_destinations("i1", "sync_events", state,
                                   [SkippedDestination("d1", "Site B", "it's turned off in Gundi")])
    assert [c.kwargs["title"] for c in logs.await_args_list] == [
        "Destination Site B skipped: it has no token in Gundi",
        "Destination Site B skipped: it's turned off in Gundi",
    ]


# --- 1. Events skipped for their type are copied once the type can be set up -------------

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)


def events_run(source, deadline=None):
    state = ShareState("i1", manager=InMemoryStateManager())
    settings = event_settings(SOURCE_URL, SyncEventsConfig.parse_obj({"start_from": "2026-09-01T00:00:00Z"}))
    return EventsRun("i1", source, settings, None, state, deadline or time.monotonic() + 60), state


@pytest.mark.asyncio
async def test_events_skipped_for_their_type_are_copied_once_it_works(monkeypatch):
    monkeypatch.setattr(sync_events, "utcnow", lambda: NOW)
    source, dest = type_sites()
    old = (NOW - timedelta(days=40)).isoformat()
    for event_id, updated in (("too_old", old), ("e1", "2026-10-01T00:00:00+00:00"),
                              ("e2", "2026-10-02T00:00:00+00:00")):
        source.events[event_id] = make_event(event_id, updated)
    run, state = events_run(source)
    dest.failures["post_event_type_v2"] = IntegrationBadResponseError("Destination site x: no", status_code=400)
    first = DestinationReport("Site B", "Events")
    await sync_destination(run, DESTINATION, dest, first)
    assert first.failed == 3 and not dest.events
    waiting = await state.get_waiting_types("d1")
    assert waiting == {"wildlife_sighting": datetime.fromisoformat(old)}

    # The type is fixed; nothing changed at the source, so the bookmark alone would never return.
    del dest.failures["post_event_type_v2"]
    second = DestinationReport("Site B", "Events")
    await sync_destination(run, DESTINATION, dest, second)
    # Read again from the oldest skipped event, but no further back than the 30-day limit.
    assert set(dest.events) == {"e1", "e2"}
    assert await state.get_waiting_types("d1") == {}


@pytest.mark.asyncio
async def test_a_type_that_still_fails_keeps_waiting(monkeypatch):
    monkeypatch.setattr(sync_events, "utcnow", lambda: NOW)
    source, dest = type_sites()
    source.events["e1"] = make_event("e1", "2026-10-01T00:00:00+00:00")
    run, state = events_run(source)
    dest.failures["post_event_type_v2"] = IntegrationBadResponseError("Destination site x: no", status_code=400)
    await sync_destination(run, DESTINATION, dest, DestinationReport("Site B", "Events"))
    await sync_destination(run, DESTINATION, dest, DestinationReport("Site B", "Events"))
    assert "wildlife_sighting" in await state.get_waiting_types("d1")
    assert not dest.events


# --- 2. A v1 type missing at the destination is created there as v2 ---------------------

V1_TYPE = {"value": "cluster", "display": "Cluster", "category": {"value": "monitoring", "display": "Monitoring"},
           "is_active": True}
V2_SCHEMA = {"json": {"properties": {"notes": {"type": "string", "title": "Notes"}}},
             "ui": {"fields": {}, "sections": {}, "order": []}}


def v1_sites():
    source, dest = type_sites()
    source.event_types_v1["cluster"] = dict(V1_TYPE)
    return source, dest


@pytest.mark.asyncio
async def test_a_missing_v1_type_is_created_as_v2_from_earthrangers_conversion():
    source, dest = v1_sites()
    source.v1_conversions["cluster"] = {"event_type": "cluster", "v2_schema": V2_SCHEMA, "errors": []}
    created = await EventTypeSync(source, dest, SETTINGS).ensure("cluster")
    assert created["value"] == "sitea_cluster" and created["category"] == "sitea_monitoring"
    stored = dest.event_types["sitea_cluster"]
    assert has_provenance_fields(stored) and "notes" in stored["schema"]
    assert source.calls_to("preview_v1_type_as_v2") == [("cluster",)]


@pytest.mark.asyncio
async def test_a_v1_type_earthranger_cant_convert_says_why():
    source, dest = v1_sites()
    source.v1_conversions["cluster"] = {"v2_schema": None, "errors": [{"code": "x", "message": "unsupported widget"}]}
    sync = EventTypeSync(source, dest, SETTINGS)
    assert await sync.ensure("cluster") is None
    assert "unsupported widget" in sync.failures["cluster"]
    assert "create it at the destination first" in sync.failures["cluster"]
    assert "sitea_cluster" not in dest.event_types


@pytest.mark.asyncio
async def test_a_site_without_the_converter_falls_back_to_the_old_message():
    source, dest = v1_sites()
    source.failures["preview_v1_type_as_v2"] = IntegrationBadResponseError("Source site x: no", status_code=405)
    sync = EventTypeSync(source, dest, SETTINGS)
    assert await sync.ensure("cluster") is None
    assert "create it at the destination first" in sync.failures["cluster"]


@pytest.mark.asyncio
async def test_without_type_updates_a_missing_v1_type_is_not_created():
    source, dest = v1_sites()
    source.v1_conversions["cluster"] = {"v2_schema": V2_SCHEMA, "errors": []}
    sync = EventTypeSync(source, dest, replace(SETTINGS, update_schema=False))
    assert await sync.ensure("cluster") is None
    assert not source.calls_to("preview_v1_type_as_v2")


# --- 6. Subject and device details changed at the source reach the destination ----------

@pytest.mark.asyncio
async def test_changed_details_are_sent_once_and_then_left_alone():
    dest = FakeSite()
    dest.subject_sources["s1"] = [{"id": "dsrc", "manufacturer_id": "src-1", "additional": {"collar": "old"}}]
    state = ShareState("i1", manager=InMemoryStateManager())
    subject = {"id": "s1", "name": "Kali", "additional": {"sex": "female"}}
    report = DestinationReport("Site B", "Tracks")
    for _ in range(2):
        await _refresh_details(dest, "d1", state, subject, {"src-1": {"collar": "new"}}, report, "Kali")
    assert dest.calls_to("patch_subject") == [("s1", {"additional": {"sex": "female"}})]
    assert dest.subject_sources["s1"][0]["additional"] == {"collar": "new"}
    assert len(dest.calls_to("patch_source")) == 1
    # Changed again at the source: sent again.
    subject["additional"] = {"sex": "female", "age": "adult"}
    await _refresh_details(dest, "d1", state, subject, {"src-1": {"collar": "new"}}, report, "Kali")
    assert len(dest.calls_to("patch_subject")) == 2 and not report.errors


@pytest.mark.asyncio
async def test_a_destination_that_wont_change_subjects_gets_a_warning_and_tracks_continue():
    dest = FakeSite()
    dest.failures["patch_subject"] = IntegrationAuthError(
        "Destination site x: EarthRanger denied access with these credentials", status_code=403)
    state = ShareState("i1", manager=InMemoryStateManager())
    report = DestinationReport("Site B", "Tracks")
    await _refresh_details(dest, "d1", state, {"id": "s1", "additional": {"a": 1}}, {}, report, "Kali")
    assert DETAILS_NOT_UPDATED in report.errors
    # Not recorded as sent, so the next daily check tries again.
    assert await state.get_value(OBSERVATIONS_ACTION, "d1:details:s1") is None
