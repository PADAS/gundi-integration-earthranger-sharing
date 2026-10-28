from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

import pytest

from app.actions import action_handlers, handlers
from app.actions.configurations import (
    AuthenticateConfig, ListEventTypesQuery, ListFeatureGroupsQuery, ListSubjectGroupsQuery,
    SyncEventsConfig, SyncObservationsConfig,
)
from app.ersharing import clients
from app.ersharing.tests.fakes import FakeSite
from app.services.errors import IntegrationAuthError

INTEGRATION = SimpleNamespace(
    id="i1", base_url="https://sitea.pamdas.org",
    configurations=[SimpleNamespace(action=SimpleNamespace(value="auth"), data={"token": "t"})],
)


@pytest.fixture
def source(monkeypatch):
    site = FakeSite("https://sitea.pamdas.org", role="source")
    monkeypatch.setattr(clients, "open_site", lambda url, auth, role: site)
    return site


def test_all_actions_are_registered():
    assert sorted(action_handlers) == [
        "auth", "list_event_types", "list_feature_groups", "list_subject_groups", "sync_events", "sync_observations",
    ]


def test_sharing_actions_have_portal_names_and_descriptions():
    assert handlers.action_sync_events.action_title == "Share Events"
    assert handlers.action_sync_observations.action_title == "Share Subjects"
    # The portal shows the config's description under the section's title.
    assert SyncEventsConfig.schema()["description"].startswith("Shares this site's events")
    assert SyncObservationsConfig.schema()["description"].startswith("Shares this site's subjects")


@pytest.mark.asyncio
async def test_auth_reports_capabilities(source):
    result = await handlers.action_auth(INTEGRATION, AuthenticateConfig(token="t"))
    assert result == {"valid_credentials": True, "capabilities": {"events": True, "subject_tracks": True}}


@pytest.mark.asyncio
async def test_auth_observations_probe_is_recent_and_cursor_based(source):
    await handlers.action_auth(INTEGRATION, AuthenticateConfig(token="t"))
    (params,) = [args[1] for args in source.calls_to("can_read") if args[0] == "observations"]
    assert params["use_cursor"] == "true"
    since = datetime.fromisoformat(params["since"])
    assert timedelta(hours=23) < datetime.now(timezone.utc) - since < timedelta(hours=25)


@pytest.mark.asyncio
async def test_auth_warns_when_the_token_cannot_read_subjects_or_observations(source):
    source.unreadable = {"subjects", "observations"}
    result = await handlers.action_auth(INTEGRATION, AuthenticateConfig(token="t"))
    assert result["valid_credentials"] is True
    assert result["warning"].startswith("This token can't read: subject_tracks")


@pytest.mark.asyncio
async def test_auth_rejects_bad_credentials(source):
    source.failures["get_me"] = IntegrationAuthError("Source site sitea.pamdas.org: EarthRanger rejected the credentials")
    result = await handlers.action_auth(INTEGRATION, AuthenticateConfig(token="t"))
    assert result == {"valid_credentials": False,
                      "error": "Source site sitea.pamdas.org: EarthRanger rejected the credentials"}


@pytest.mark.asyncio
async def test_auth_requires_a_token_or_login(source):
    result = await handlers.action_auth(INTEGRATION, AuthenticateConfig())
    assert result == {"valid_credentials": False, "error": "Please provide a token."}


@pytest.mark.asyncio
async def test_reference_actions_list_source_options(source):
    source.event_types = {"b": {"value": "b", "display": "Bravo", "category": {"display": "Security"}},
                          "a": {"value": "a", "display": "Alpha", "category": "monitoring"}}
    source.subject_groups = [{"id": "g1", "name": "Lions"}]
    source.feature_groups = {"fg1": {"id": "fg1", "name": "Park"}}
    event_types = await handlers.action_list_event_types(INTEGRATION, ListEventTypesQuery())
    assert [(o["value"], o["label"], o["group"]) for o in event_types["options"]] == [
        ("a", "Alpha", "monitoring"), ("b", "Bravo", "Security")]
    groups = await handlers.action_list_subject_groups(INTEGRATION, ListSubjectGroupsQuery())
    assert [(o["value"], o["label"]) for o in groups["options"]] == [("g1", "Lions")]
    areas = await handlers.action_list_feature_groups(INTEGRATION, ListFeatureGroupsQuery())
    assert [(o["value"], o["label"]) for o in areas["options"]] == [("fg1", "Park")]
