import pytest

from app.actions.configurations import SyncEventsConfig, SyncObservationsConfig


def field_at(schema: dict, path: str) -> dict:
    """The schema of the field at a dotted path, following pydantic's allOf/$ref wrapping."""
    node = schema
    for part in path.split("."):
        refs = [s["$ref"] for s in node.get("allOf", []) if "$ref" in s] + ([node["$ref"]] if "$ref" in node else [])
        if refs:
            node = schema["definitions"][refs[0].rsplit("/", 1)[-1]]
        node = node["properties"][part]
    return node


@pytest.mark.parametrize("model", [SyncEventsConfig, SyncObservationsConfig])
def test_every_raw_view_target_is_a_form_field(model):
    schema = model.schema()
    raw = model.ui_schema()["gundi:raw_view"]
    assert raw["title"] == "Legacy View"
    for v1_key, path in raw["map"].items():
        assert field_at(schema, path), f"{v1_key} -> {path}"


def test_events_raw_view_maps_v1_keys():
    raw_map = SyncEventsConfig.ui_schema()["gundi:raw_view"]["map"]
    assert raw_map["within_featuregroups"] == "area_feature_groups"
    assert raw_map["update_schema"] == "compatibility.update_schema"
    assert set(raw_map) == {
        "prepend_system_to_categories", "prepend_system_to_event_types", "prepend_system_to_event_titles",
        "update_schema", "delete_unmatched_events", "source_system_value", "source_system_name",
        "within_featuregroups",
    }


def test_observations_raw_view_maps_v1_keys():
    raw_map = SyncObservationsConfig.ui_schema()["gundi:raw_view"]["map"]
    assert raw_map == {
        "subject_group": "destination_subject_group",
        "feature_groups": "area_feature_groups",
        "provider_key": "compatibility.provider_key",
        "subject_additional": "compatibility.subject_additional",
        "src_user": "compatibility.src_user",
    }


def test_events_raw_view_drops_v1_run_status_and_masks_the_token():
    raw = SyncEventsConfig.ui_schema()["gundi:raw_view"]
    assert raw["drop"] == ["days_to_sync", "error", "last_run", "last_successful_run", "warning"]
    assert raw["mask"] == ["dest_token"]


def test_observations_raw_view_drops_v1_run_settings_and_masks_the_token():
    raw = SyncObservationsConfig.ui_schema()["gundi:raw_view"]
    assert raw["drop"] == [
        "batch_size", "error", "last_run", "last_successful_run", "look_back_window_hours", "schedule_interval_minutes",
    ]
    assert raw["mask"] == ["dest_token"]


def test_observations_raw_view_unwraps_v1_feature_group_objects():
    raw = SyncObservationsConfig.ui_schema()["gundi:raw_view"]
    assert raw["unwrap"] == {"feature_groups": "id"}
    assert raw["map"]["feature_groups"] == "area_feature_groups"


def test_events_raw_view_unwraps_nothing():
    # v1 stores within_featuregroups as plain ids.
    assert "unwrap" not in SyncEventsConfig.ui_schema()["gundi:raw_view"]
