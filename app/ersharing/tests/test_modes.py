from datetime import datetime, timezone

import pytest

from app.actions.configurations import AuthenticateConfig, SyncEventsConfig, SyncObservationsConfig
from app.ersharing.modes import compatibility_overrides, event_settings, observation_settings
from app.ersharing.naming import SourceSystem, derive_source_system, same_site, site_host, site_root
from app.ersharing.timeutil import parse_dt

SOURCE = "https://sitea.pamdas.org"


def test_parse_dt_makes_naive_values_utc():
    assert parse_dt("2026-10-01T10:00:00") == datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    assert parse_dt("2026-10-01T10:00:00-02:00") == datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def test_source_system_is_derived_from_host():
    assert derive_source_system(SOURCE) == SourceSystem(value="sitea", name="Sitea")


def test_source_system_overrides_win():
    assert derive_source_system(SOURCE, "spa", "Spa Park") == SourceSystem(value="spa", name="Spa Park")


@pytest.mark.parametrize("a, b, expected", [
    ("https://SiteA.pamdas.org/", "sitea.pamdas.org", True),
    ("https://sitea.pamdas.org", "https://siteb.pamdas.org", False),
])
def test_same_site(a, b, expected):
    assert same_site(a, b) is expected


def test_site_root_normalises():
    assert site_root("SiteA.pamdas.org/api/v1.0") == "https://sitea.pamdas.org"


def test_site_host_rejects_url_without_host():
    with pytest.raises(ValueError):
        site_host("https://")


def test_simple_event_settings_defaults():
    s = event_settings(SOURCE, SyncEventsConfig())
    assert s.source == SourceSystem("sitea", "Sitea")
    assert (s.prepend_categories, s.prepend_event_types, s.prepend_titles) == (True, True, True)
    assert s.update_schema and s.delete_unmatched
    assert s.event_types == frozenset() and s.area_feature_groups == ()
    assert compatibility_overrides(SyncEventsConfig(), SOURCE) == []


def test_compatibility_values_are_applied_and_reported():
    config = SyncEventsConfig.parse_obj(
        {"compatibility": {"prepend_system_to_event_titles": False, "source_system_value": "spa"}}
    )
    s = event_settings(SOURCE, config)
    assert s.prepend_titles is False
    assert s.source.value == "spa"
    assert compatibility_overrides(config, SOURCE) == ["prepend_system_to_event_titles", "source_system_value"]


def test_override_equal_to_default_is_not_compatibility_mode():
    config = SyncEventsConfig.parse_obj({"compatibility": {"update_schema": True, "source_system_value": "sitea"}})
    assert compatibility_overrides(config, SOURCE) == []


def test_observation_settings_defaults():
    s = observation_settings(SOURCE, SyncObservationsConfig())
    assert s.destination_subject_group == "Sitea"
    assert s.provider_key == "das_provider"
    assert s.subject_additional is True and s.src_user is None
    assert compatibility_overrides(SyncObservationsConfig(), SOURCE) == []


def test_subject_additional_off_is_a_compatibility_override():
    # v1 copies subject and source "additional" data unless told not to; v2 keeps that default.
    config = SyncObservationsConfig.parse_obj({"compatibility": {"subject_additional": False}})
    assert observation_settings(SOURCE, config).subject_additional is False
    assert compatibility_overrides(config, SOURCE) == ["subject_additional"]


def test_empty_destination_group_means_no_group():
    s = observation_settings(SOURCE, SyncObservationsConfig(destination_subject_group=""))
    assert s.destination_subject_group is None


def test_share_toggles_are_titled_for_admins():
    assert SyncEventsConfig.schema()["properties"]["run_on_schedule"]["title"] == "Share events"
    assert SyncObservationsConfig.schema()["properties"]["run_on_schedule"]["title"] == "Share subject tracks"


def test_pick_lists_use_reference_actions():
    ui = SyncEventsConfig.ui_schema()
    assert ui["event_types"]["items"]["gundi:reference"]["action"] == "list_event_types"
    assert ui["area_feature_groups"]["items"]["gundi:reference"]["action"] == "list_feature_groups"
    assert ui["start_from"] == {"ui:widget": "hidden"}
    ui = SyncObservationsConfig.ui_schema()
    assert ui["subject_groups"]["items"]["gundi:reference"]["action"] == "list_subject_groups"
    assert ui["area_feature_groups"]["items"]["gundi:reference"]["action"] == "list_feature_groups"


def test_destination_auth_config_parses():
    # The shape the earth_ranger integration type stores for its auth action.
    auth = AuthenticateConfig.parse_obj({"authentication_type": "token", "token": "abc", "username": "", "password": ""})
    assert auth.token.get_secret_value() == "abc"
