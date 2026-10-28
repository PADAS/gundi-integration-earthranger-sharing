from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Sequence

from pydantic import BaseModel, Field, SecretStr

from app.services.utils import GlobalUISchemaOptions
from .core import (
    AuthActionConfiguration,
    ExecutableActionMixin,
    PullActionConfiguration,
    ReferenceActionConfiguration,
)


class ERAuthenticationType(str, Enum):
    TOKEN = "token"
    USERNAME_PASSWORD = "username_password"


AUTH_TYPE_DISPLAY_NAMES = {
    ERAuthenticationType.TOKEN: "Token",
    ERAuthenticationType.USERNAME_PASSWORD: "Username & Password",
}


# Credentials for one EarthRanger site. Field names match the earth_ranger
# integration type's auth config, so this model parses both the source
# credentials and each destination's stored ones. Destinations store no
# base_url, so it is optional here and required only in the published form.
class AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin):
    base_url: Optional[str] = Field(
        None, title="EarthRanger site URL", description="For example https://mysite.pamdas.org.",
    )
    authentication_type: ERAuthenticationType = Field(
        ERAuthenticationType.TOKEN, title="Authentication type",
        description="Sign in with a token, or with a username and password.",
    )
    token: Optional[SecretStr] = Field(None, title="Token", format="password")
    username: Optional[str] = Field(None, title="Username")
    password: Optional[SecretStr] = Field(None, title="Password", format="password")

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=["base_url", "authentication_type", "token", "username", "password"],
    )

    class Config:
        @staticmethod
        def schema_extra(schema: dict):
            # Same form contract as gundi-integration-earthranger: labelled
            # auth types, and token OR username & password, each required.
            properties = schema["properties"]
            credentials = {name: properties.pop(name) for name in ("token", "username", "password")}
            schema["required"] = ["base_url"]
            auth_type = properties["authentication_type"]
            auth_type.pop("allOf", None)
            auth_type.pop("$ref", None)
            auth_type["type"] = "string"
            auth_type["oneOf"] = [{"const": member.value, "title": AUTH_TYPE_DISPLAY_NAMES[member]}
                                  for member in ERAuthenticationType]
            schema.update({
                "if": {"properties": {"authentication_type": {"const": ERAuthenticationType.TOKEN.value}}},
                "then": {"required": ["token"], "properties": {"token": credentials["token"]}},
                "else": {
                    "required": ["username", "password"],
                    "properties": {"username": credentials["username"], "password": credentials["password"]},
                },
            })

def reference(action: str) -> dict:
    """gundi:reference annotation: the portal fills each list item from this
    connection's own reference action (same contract as gundi-integration-earthranger)."""
    return {"action": action, "target": "self", "params": {}, "allow_free_text": True}


RAW_VIEW_TITLE = "Legacy View"

# v1 er2er state key -> form field path ("a.b" is a nested field). The portal's
# raw view translates a pasted v1 state into the form, and the form back, with these.
EVENTS_V1_KEYS = {
    "prepend_system_to_categories": "compatibility.prepend_system_to_categories",
    "prepend_system_to_event_types": "compatibility.prepend_system_to_event_types",
    "prepend_system_to_event_titles": "compatibility.prepend_system_to_event_titles",
    "update_schema": "compatibility.update_schema",
    "delete_unmatched_events": "compatibility.delete_unmatched_events",
    "source_system_value": "compatibility.source_system_value",
    "source_system_name": "compatibility.source_system_name",
    "within_featuregroups": "area_feature_groups",
}
OBSERVATIONS_V1_KEYS = {
    "subject_group": "destination_subject_group",
    "feature_groups": "area_feature_groups",
    "provider_key": "compatibility.provider_key",
    "subject_additional": "compatibility.subject_additional",
    "src_user": "compatibility.src_user",
}
# v1 state keys with no form field are kept in the config as they are, except
# these. Dropped: v1's own run status and sync window, which v2 doesn't read.
# Masked: kept, but saved as "*******" so the secret never reaches the config.
EVENTS_V1_DROP = ["days_to_sync", "error", "last_run", "last_successful_run", "warning"]
OBSERVATIONS_V1_DROP = ["batch_size", "error", "last_run", "last_successful_run", "look_back_window_hours",
                        "schedule_interval_minutes"]
V1_MASK = ["dest_token"]
# Unwrapped: v1 stores these lists as [{"id": ...}]; the portal reads the named field of each item.
OBSERVATIONS_V1_UNWRAP = {"feature_groups": "id"}


def raw_view(v1_keys: dict, drop: Sequence[str] = (), mask: Sequence[str] = (), unwrap: Optional[dict] = None) -> dict:
    """gundi:raw_view annotation: the portal shows a "Legacy View" of this
    action and translates between it and the form with this key map."""
    annotation = {"title": RAW_VIEW_TITLE, "map": dict(v1_keys), "drop": list(drop), "mask": list(mask)}
    if unwrap:
        annotation["unwrap"] = dict(unwrap)
    return annotation


class EventsCompatibility(BaseModel):
    """v1 settings kept for migrated shares. None means "use the simple-mode default"."""

    prepend_system_to_categories: Optional[bool] = Field(None, title="Prefix categories with the source name")
    prepend_system_to_event_types: Optional[bool] = Field(None, title="Prefix event types with the source name")
    prepend_system_to_event_titles: Optional[bool] = Field(None, title="Prefix event titles with the source name")
    update_schema: Optional[bool] = Field(None, title="Allow updates to destination event types")
    delete_unmatched_events: Optional[bool] = Field(
        None, title="Remove destination copies when the source event is deleted"
    )
    source_system_value: Optional[str] = Field(None, title="Source system code")
    source_system_name: Optional[str] = Field(None, title="Source system display name")


class ObservationsCompatibility(BaseModel):
    provider_key: Optional[str] = Field(None, title="Destination provider key")
    subject_additional: Optional[bool] = Field(None, title="Copy subject and source 'additional' data")
    src_user: Optional[str] = Field(None, title="Sharing user recorded in the lineage")


class SyncEventsConfig(PullActionConfiguration):
    """Shares this site's events with every destination EarthRanger site. Choose which event types and areas to share."""

    run_on_schedule: bool = Field(
        True, title="Share events", description="Copy new and changed events to every destination."
    )
    event_types: List[str] = Field(
        default_factory=list, title="Event types to share", description="Leave empty to share all event types."
    )
    area_feature_groups: List[str] = Field(
        default_factory=list,
        title="Events area filter",
        description="Only share events inside these feature groups. Leave empty to share events anywhere.",
    )
    start_from: Optional[datetime] = Field(
        None, title="Start from", description="Where the first sync starts when no progress is recorded yet."
    )
    compatibility: EventsCompatibility = Field(
        default_factory=EventsCompatibility, title="Advanced (v1 compatibility)"
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=["run_on_schedule", "event_types", "area_feature_groups", "compatibility", "start_from"],
    )

    @classmethod
    def ui_schema(cls, *args, **kwargs):
        base = super().ui_schema(*args, **kwargs)
        base["event_types"] = {"items": {"gundi:reference": reference("list_event_types")}}
        base["area_feature_groups"] = {"items": {"gundi:reference": reference("list_feature_groups")}}
        base["start_from"] = {"ui:widget": "hidden"}
        base["gundi:raw_view"] = raw_view(EVENTS_V1_KEYS, drop=EVENTS_V1_DROP, mask=V1_MASK)
        return base


class SyncObservationsConfig(PullActionConfiguration):
    """Shares this site's subjects and their tracks with every destination EarthRanger site. Choose which subject groups and areas to share."""

    run_on_schedule: bool = Field(
        True, title="Share subject tracks", description="Copy new track points to every destination."
    )
    subject_groups: List[str] = Field(
        default_factory=list, title="Subject groups to share", description="Leave empty to share all subject groups."
    )
    destination_subject_group: Optional[str] = Field(
        None,
        title="Destination subject group name",
        description="Shared subjects are added to this group at each destination. "
                    "Defaults to the source site's name; set it to an empty value to add no group.",
    )
    area_feature_groups: List[str] = Field(
        default_factory=list,
        title="Tracks area filter",
        description="Only share points inside these feature groups. Leave empty to share points anywhere.",
    )
    start_from: Optional[datetime] = Field(
        None, title="Start from", description="Where the first sync starts when no progress is recorded yet."
    )
    compatibility: ObservationsCompatibility = Field(
        default_factory=ObservationsCompatibility, title="Advanced (v1 compatibility)"
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=["run_on_schedule", "subject_groups", "destination_subject_group", "area_feature_groups",
               "compatibility", "start_from"],
    )

    @classmethod
    def ui_schema(cls, *args, **kwargs):
        base = super().ui_schema(*args, **kwargs)
        base["subject_groups"] = {"items": {"gundi:reference": reference("list_subject_groups")}}
        base["area_feature_groups"] = {"items": {"gundi:reference": reference("list_feature_groups")}}
        base["start_from"] = {"ui:widget": "hidden"}
        base["gundi:raw_view"] = raw_view(OBSERVATIONS_V1_KEYS, drop=OBSERVATIONS_V1_DROP, mask=V1_MASK,
                                           unwrap=OBSERVATIONS_V1_UNWRAP)
        return base


class ListEventTypesQuery(ReferenceActionConfiguration):
    """The source site's active v2 event types."""


class ListSubjectGroupsQuery(ReferenceActionConfiguration):
    """The source site's subject groups."""


class ListFeatureGroupsQuery(ReferenceActionConfiguration):
    """The source site's spatial feature groups."""
