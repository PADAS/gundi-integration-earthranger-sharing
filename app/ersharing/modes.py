"""Effective share settings: simple-mode defaults plus v1 compatibility overrides (spec §6)."""
from dataclasses import dataclass
from datetime import datetime
from typing import FrozenSet, List, Optional, Tuple, Union

from app.actions.configurations import SyncEventsConfig, SyncObservationsConfig
from .naming import SourceSystem, derive_source_system

SIMPLE_PROVIDER_KEY = "das_provider"

_SIMPLE_DEFAULTS = {
    "prepend_system_to_categories": True,
    "prepend_system_to_event_types": True,
    "prepend_system_to_event_titles": True,
    "update_schema": True,
    "delete_unmatched_events": True,
    "provider_key": SIMPLE_PROVIDER_KEY,
    "subject_additional": True,
    "src_user": None,
}


@dataclass(frozen=True)
class EventSettings:
    source: SourceSystem
    event_types: FrozenSet[str]
    area_feature_groups: Tuple[str, ...]
    prepend_categories: bool
    prepend_event_types: bool
    prepend_titles: bool
    update_schema: bool
    delete_unmatched: bool
    start_from: Optional[datetime]


@dataclass(frozen=True)
class ObservationSettings:
    source: SourceSystem
    subject_groups: FrozenSet[str]
    destination_subject_group: Optional[str]
    area_feature_groups: Tuple[str, ...]
    provider_key: str
    subject_additional: bool
    src_user: Optional[str]
    start_from: Optional[datetime]


def _pick(value, default):
    return default if value is None else value


def event_settings(source_url: str, config: SyncEventsConfig) -> EventSettings:
    c = config.compatibility
    return EventSettings(
        source=derive_source_system(source_url, c.source_system_value, c.source_system_name),
        event_types=frozenset(config.event_types),
        area_feature_groups=tuple(config.area_feature_groups),
        prepend_categories=_pick(c.prepend_system_to_categories, True),
        prepend_event_types=_pick(c.prepend_system_to_event_types, True),
        prepend_titles=_pick(c.prepend_system_to_event_titles, True),
        update_schema=_pick(c.update_schema, True),
        delete_unmatched=_pick(c.delete_unmatched_events, True),
        start_from=config.start_from,
    )


def observation_settings(source_url: str, config: SyncObservationsConfig) -> ObservationSettings:
    c = config.compatibility
    source = derive_source_system(source_url)
    group = config.destination_subject_group
    return ObservationSettings(
        source=source,
        subject_groups=frozenset(config.subject_groups),
        destination_subject_group=source.name if group is None else (group or None),
        area_feature_groups=tuple(config.area_feature_groups),
        provider_key=_pick(c.provider_key, SIMPLE_PROVIDER_KEY),
        subject_additional=_pick(c.subject_additional, True),
        src_user=c.src_user,
        start_from=config.start_from,
    )


def compatibility_overrides(config: Union[SyncEventsConfig, SyncObservationsConfig], source_url: str) -> List[str]:
    """Names of compatibility settings that differ from what simple mode would do.
    Empty means the share is in simple mode."""
    derived = derive_source_system(source_url)
    defaults = {**_SIMPLE_DEFAULTS, "source_system_value": derived.value, "source_system_name": derived.name}
    return [
        name for name, value in config.compatibility.dict().items()
        if value is not None and value != defaults[name]
    ]
