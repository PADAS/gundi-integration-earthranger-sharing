# ER2ER Sharing Runner (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `earthranger_sharing` action runner that mirrors events and subject tracks from one EarthRanger site to every EarthRanger destination of a Gundi v2 connection. It replaces the two legacy v1 ER2ER jobs.

**Architecture:**
- **Thin action handlers.** `app/actions/handlers.py` contains only thin handlers; the work lives in focused modules under `app/er2er/`.
- **Direct writes.** Each run resolves the connection's destinations and their stored ER credentials through `gundi-client`, then writes to each destination ER directly with `erclient`.
- **Per-destination progress.** Progress is kept per destination in Redis through the template's `IntegrationStateManager`. Markers advance only past work the destination accepted.

**Tech Stack:**
- Python 3.10, pydantic 1.10
- `earthranger-client` 1.15 (`AsyncERClient`)
- `gundi-client-v2` 3.7, `gundi-core` 1.11
- shapely 2, python-dateutil
- pytest 7, pytest-asyncio 0.21 (strict mode: every async test needs `@pytest.mark.asyncio`), pytest-mock

**Spec:** `docs/superpowers/specs/2026-10-15-er2er-v2-sharing-design.md`. Read §4–§11 before starting.

**Out of scope for this plan:** the cdip `migrate_er2er_v1` command (spec §10). It gets its own plan once the prod inventory (`scripts/er2er_v1_inventory.py`) has been run. Phases 2–4 are also out of scope.

## Global Constraints

- **Integration type slug:** `earthranger_sharing`. Destinations are integrations whose type value is `earth_ranger`.
- **Action ids:** `auth`, `list_event_types`, `list_subject_groups`, `list_feature_groups`, `sync_events`, `sync_observations`. Both sync actions run on `*/5 * * * *`.
- **v2 event types only.** No v1 event-type code paths (spec D7).
- **Event copies keep the source event UUID** and carry these in `event_details`: `er2er_src_id`, `er2er_src_serial_number`, `er2er_src_system`, `er2er_src_service_root`.
- **Prefixing:**
  - Values use `{value}_{original}`. Event types are lower-cased.
  - Display names and titles use `{Name}-{original}`, with no spaces around the hyphen. This matches v1 output, so migrated destinations keep their existing type and category names. Spec §7.2's `{SysName} - {display}` was illustrative; parity wins.
- **Simple-mode defaults:**
  - all prefixes on;
  - `update_schema` on;
  - `delete_unmatched_events` on;
  - `provider_key="das_provider"`;
  - `subject_additional` off;
  - destination subject group defaults to the derived source name. An empty string means "no group", which preserves v1 shares that had no `subject_group`.
- **Backfill when no marker and no `start_from`:** events 10 days; observations 12 hours.
- **Batching and file limits:** observation batches of at most 200; attachments over 20 MB are skipped with a warning.
- **Time budget:** `0.8 × settings.MAX_ACTION_EXECUTION_TIME`. When it runs out, save progress, release the lease, then `trigger_action` the same action.
- **Leases:** each sync action holds a Redis lease (`set_if_absent`, TTL = `MAX_ACTION_EXECUTION_TIME + 30`). A run that finds the lease taken returns `{"status": "skipped", ...}`.
- **Import rule:** `app/actions/__init__.py` imports `app.actions.handlers` at import time. **`handlers.py` must import `app.er2er.*` inside function bodies, never at module top**, or importing any `app.er2er` module first creates a circular import.
- **Errors:** every ER failure leaves `ERSite` as an `IntegrationError` subclass, with:
  - `message` prefixed `"Source site <host>: "` or `"Destination site <host>: "`;
  - an attribute `site_role` of `"source"` or `"destination"`.
  
  Source-side halting errors abort the run. Destination-side halting errors stop that destination only. `halts_site()` decides which errors halt.
- **Deliberate deviations from the spec:**
  - **Reconciliation runs inside `sync_events`.** The spec's daily `reconcile_events` runs as a once-per-24-hours step of `sync_events`, gated per destination with `set_if_absent`. A separate pull action would need its own stored config row, and the runner silently skips pull actions without one. Behaviour is unchanged: deletes are mirrored daily.
  - **Destination checks happen at run time.** Save-time validation of destinations (spec §6.5) isn't possible: destinations are separate integrations, validated by their own `auth` action. The runner checks them at the start of each run (same-site, missing credentials) and reports problems in the activity log.
  - **No `er2er_src_id` lookup fallback.** Copies are looked up by UUID only. v1 copies already use the source UUID; the fallback (spec §7.3) would require listing destination events and is dropped.

## Review Focus

1. **A v1 copy adopted on first sync.** The destination already holds the event, its notes and its attachments from v1, but there is no state record yet. Notes and files must be matched, not duplicated. Covered by `test_adopts_v1_copy_without_duplicating_notes_or_files` in Task 9.
2. **A late-arriving observation.** One whose `recorded_at` is before the destination's marker but whose `created_at` is after it must still be shared. Covered by `test_late_arriving_observation_is_new` in Task 11.
3. **The marker boundary.** `update_date >= marker` re-reads the last event every run, and that must not cause a rewrite. Covered by `test_rerun_does_not_rewrite_unchanged_events` in Task 9.
4. **A destination going unreachable mid-run.** Its marker must hold at the last success while the other destinations still sync. Covered by `test_unreachable_destination_keeps_progress_and_spares_others` in Task 10.
5. **A copy deleted by hand at the destination.** It must be recreated on the next source change rather than failing forever with 404. Covered by `test_recreates_copy_deleted_at_destination` in Task 9.

---

## File Structure

| File | Responsibility |
|---|---|
| `requirements.in` | Add `earthranger-client`, `shapely`, `python-dateutil` |
| `app/actions/configurations.py` | Auth, reference-query and sync config models; the portal form |
| `app/actions/handlers.py` | Thin action handlers (lazy imports of `app.er2er`) |
| `app/er2er/__init__.py` | Package marker |
| `app/er2er/timeutil.py` | `parse_dt`, `utcnow` |
| `app/er2er/naming.py` | Site host/root, same-site check, source-system derivation |
| `app/er2er/modes.py` | Effective settings from simple defaults + compatibility overrides |
| `app/er2er/clients.py` | `build_er_client`, `as_integration_error`, `halts_site`, `ERSite`, `open_site`, `auth_config_of` |
| `app/er2er/destinations.py` | Resolve a connection's ER destinations and credentials |
| `app/er2er/state.py` | `Marker`, `EventRecord`, `ShareState` over `IntegrationStateManager` |
| `app/er2er/reporting.py` | Per-destination run report, grouped errors, stall check, activity-log publishing |
| `app/er2er/geo.py` | Area filters from ER feature groups |
| `app/er2er/event_types.py` | Destination category/event-type naming and sync |
| `app/er2er/events.py` | Event cleaning, provenance, filters, echo guard |
| `app/er2er/notes_files.py` | Note/file/incident diffing, adoption of v1 copies, applying changes |
| `app/er2er/sync_events.py` | `sync_events` orchestration incl. daily reconcile |
| `app/er2er/observations.py` | Observation filters, payloads, lineage, markers (pure) |
| `app/er2er/sync_observations.py` | `sync_observations` orchestration |
| `app/er2er/tests/fakes.py` | In-memory `FakeSite`, `InMemoryStateManager`, event/observation builders |
| `app/er2er/tests/test_*.py` | One test module per `app/er2er` module |
| `app/actions/tests/test_handlers.py` | Handler wiring, auth and reference actions |

---

### Task 1: Dependencies, config models, naming and modes

**Files:**
- Modify: `requirements.in`
- Create: `app/er2er/__init__.py`, `app/er2er/tests/__init__.py`, `app/er2er/timeutil.py`, `app/er2er/naming.py`, `app/er2er/modes.py`
- Modify: `app/actions/configurations.py` (currently empty)
- Test: `app/er2er/tests/test_modes.py`

**Interfaces:**
- Produces:
  - `parse_dt(value: str | datetime) -> datetime` (always tz-aware, UTC if naive); `utcnow() -> datetime`
  - `site_host(url) -> str`, `site_root(url) -> str`, `same_site(a, b) -> bool`
  - `SourceSystem(value: str, name: str)`; `derive_source_system(url, value=None, name=None) -> SourceSystem`
  - Config models: `ERAuthenticationType`, `AuthenticateConfig`, `EventsCompatibility`, `ObservationsCompatibility`, `SyncEventsConfig`, `SyncObservationsConfig`, `ListEventTypesQuery`, `ListSubjectGroupsQuery`, `ListFeatureGroupsQuery`
  - `EventSettings`, `ObservationSettings` (frozen dataclasses); `event_settings(source_url, SyncEventsConfig) -> EventSettings`; `observation_settings(source_url, SyncObservationsConfig) -> ObservationSettings`; `compatibility_overrides(config, source_url) -> List[str]`; `SIMPLE_PROVIDER_KEY = "das_provider"`

- [ ] **Step 1: Add dependencies and create the package**

Append to `requirements.in`:

```
earthranger-client>=1.15.0,<2.0.0
shapely~=2.0
python-dateutil~=2.9
```

Then run:

```bash
mkdir -p app/er2er/tests && touch app/er2er/__init__.py app/er2er/tests/__init__.py
pip-compile --output-file=requirements.txt requirements-base.in requirements-dev.in requirements.in
pip install -r requirements.txt
```

Expected: `requirements.txt` now pins `earthranger-client==1.15.x`, `shapely==2.0.x` and `python-dateutil==2.9.x`.

- [ ] **Step 2: Write the failing tests**

`app/er2er/tests/test_modes.py`:

```python
from datetime import datetime, timezone

import pytest

from app.actions.configurations import AuthenticateConfig, SyncEventsConfig, SyncObservationsConfig
from app.er2er.modes import compatibility_overrides, event_settings, observation_settings
from app.er2er.naming import SourceSystem, derive_source_system, same_site, site_host, site_root
from app.er2er.timeutil import parse_dt

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
    assert s.subject_additional is False and s.src_user is None
    assert compatibility_overrides(SyncObservationsConfig(), SOURCE) == []


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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_modes.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'app.er2er.modes'` (or `ImportError` on `AuthenticateConfig`).

- [ ] **Step 4: Implement**

`app/er2er/timeutil.py`:

```python
from datetime import datetime, timezone
from typing import Union

from dateutil.parser import isoparse


def parse_dt(value: Union[str, datetime]) -> datetime:
    """An ER timestamp as an aware UTC datetime (naive values are taken as UTC)."""
    dt = value if isinstance(value, datetime) else isoparse(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
```

`app/er2er/naming.py`:

```python
"""Site identity and source-system naming (pure)."""
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class SourceSystem:
    value: str  # prefix for category and event-type values, e.g. "sitea"
    name: str   # prefix for display names and titles, e.g. "Sitea"


def site_host(url: str) -> str:
    """Lower-cased hostname of an ER site URL, with or without a scheme."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Site URL has no hostname")
    return host


def site_root(url: str) -> str:
    return f"https://{site_host(url)}"


def same_site(url_a: str, url_b: str) -> bool:
    return site_host(url_a) == site_host(url_b)


def derive_source_system(url: str, value: Optional[str] = None, name: Optional[str] = None) -> SourceSystem:
    """v1 kept these in state; v2 derives them from the source host unless a
    migrated share overrides them (compatibility mode)."""
    label = site_host(url).split(".")[0]
    return SourceSystem(value=value or label, name=name or label.capitalize())
```

`app/actions/configurations.py`:

```python
from datetime import datetime
from enum import Enum
from typing import List, Optional

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


class AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin):
    """Credentials for one EarthRanger site.

    Field names match the earth_ranger integration type's auth config, so this
    model parses both the source credentials and each destination's stored ones.
    """

    authentication_type: ERAuthenticationType = Field(ERAuthenticationType.TOKEN, title="Authentication type")
    token: Optional[SecretStr] = Field(None, title="Token", format="password")
    username: Optional[str] = Field(None, title="Username")
    password: Optional[SecretStr] = Field(None, title="Password", format="password")

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=["authentication_type", "token", "username", "password"],
    )


def reference(action: str) -> dict:
    """gundi:reference annotation: the portal fills each list item from this
    connection's own reference action (same contract as gundi-integration-earthranger)."""
    return {"action": action, "target": "self", "params": {}, "allow_free_text": True}


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
        return base


class SyncObservationsConfig(PullActionConfiguration):
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
        return base


class ListEventTypesQuery(ReferenceActionConfiguration):
    """The source site's active v2 event types."""


class ListSubjectGroupsQuery(ReferenceActionConfiguration):
    """The source site's subject groups."""


class ListFeatureGroupsQuery(ReferenceActionConfiguration):
    """The source site's spatial feature groups."""
```

`app/er2er/modes.py`:

```python
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
    "subject_additional": False,
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
        subject_additional=_pick(c.subject_additional, False),
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_modes.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add requirements.in requirements.txt app/er2er app/actions/configurations.py
git commit -m "feat: add ER2ER config models, naming and mode resolution"
```

---

### Task 2: ER site access and error translation

**Files:**
- Create: `app/er2er/clients.py`, `app/er2er/tests/fakes.py`
- Test: `app/er2er/tests/test_clients.py`

**Interfaces:**
- Consumes: `AuthenticateConfig`, `site_root`, `site_host` (Task 1)
- Produces:
  - `build_er_client(url: str, auth: AuthenticateConfig) -> AsyncERClient`
  - `as_integration_error(exc, *, not_found_is_configuration=True) -> IntegrationError`
  - `halts_site(error: IntegrationError) -> bool`
  - `auth_config_of(configurations) -> AuthenticateConfig` (raises `IntegrationConfigurationError` if there is no `auth` config)
  - `open_site(url: str, auth: AuthenticateConfig, role: str) -> ERSite` (`role` is `"source"` or `"destination"`)
  - `ERSite` attributes: `url` (site root), `service_root` (`{url}/api/v1.0`), `role`, `label`. It is used as `async with`.
  - `ERSite` methods (all async):
    - `get_me() -> dict`
    - `iter_events_updated_since(since: datetime, *, page_size=100, with_notes_and_files=True)`: an async iterator of `List[dict]`, ordered oldest `updated_at` first
    - `get_event(event_id, *, with_notes_and_files=False) -> Optional[dict]` (None on 404)
    - events: `post_event(event) -> dict`, `patch_event(event_id, data) -> dict`, `delete_event(event_id)`
    - notes: `post_note(event_id, text) -> dict`, `patch_note(event_id, note_id, text)`, `delete_note(event_id, note_id)`
    - files: `download_file(url) -> bytes`, `post_file(event_id, filename, content: bytes, comment="") -> dict`, `delete_file(event_id, file_id)`
    - incidents: `add_to_incident(event_id, incident_id)`, `remove_from_incident(event_id, incident_id)`
    - categories: `get_event_categories() -> List[dict]`, `post_event_category(data)`, `patch_event_category(data)`
    - event types: `list_event_types_v2() -> List[dict]`, `get_event_type_v2(value) -> Optional[dict]`, `post_event_type_v2(data)`, `patch_event_type_v2(data)`
    - subjects: `list_subject_groups() -> List[dict]`, `get_source_assignments(subject_ids: List[str]) -> List[dict]`, `get_subject_sources(subject_id) -> List[dict]`
    - `get_observations(subject_id, start: datetime) -> List[dict]`
    - feature groups: `list_feature_groups() -> List[dict]`, `get_feature_group(feature_group_id) -> dict`
    - `post_sharing_status(provider_key, payloads: List[dict])`
  - `app/er2er/tests/fakes.py`: `FakeSite` (the same interface, in memory), `InMemoryStateManager`, `make_event(...)`, `make_observation(...)`

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_clients.py`:

```python
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from erclient.er_errors import ERClientBadCredentials, ERClientBadRequest, ERClientException, ERClientNotFound

from app.actions.configurations import AuthenticateConfig
from app.er2er.clients import ERSite, auth_config_of, build_er_client, halts_site, open_site
from app.services.errors import (
    IntegrationAuthError,
    IntegrationBadResponseError,
    IntegrationConfigurationError,
    IntegrationConnectionError,
)


def site(client, role="destination"):
    return ERSite(client, "https://siteb.pamdas.org", role=role)


@pytest.mark.asyncio
async def test_failures_are_translated_and_labelled():
    client = MagicMock()
    client.get_me = AsyncMock(side_effect=ERClientBadCredentials("bad", status_code=401))
    with pytest.raises(IntegrationAuthError) as info:
        await site(client).get_me()
    assert info.value.message == "Destination site siteb.pamdas.org: EarthRanger rejected the credentials"
    assert info.value.site_role == "destination"
    assert halts_site(info.value)


@pytest.mark.asyncio
async def test_rejected_write_carries_er_reason_and_does_not_halt():
    client = MagicMock()
    client.post_report = AsyncMock(
        side_effect=ERClientBadRequest("activity/events", status_code=400, response_body={"event_type": ["invalid"]})
    )
    with pytest.raises(IntegrationBadResponseError) as info:
        await site(client).post_event({"id": "e1"})
    assert "invalid" in info.value.message
    assert info.value.status_code == 400
    assert not halts_site(info.value)


@pytest.mark.asyncio
async def test_get_event_returns_none_on_404():
    client = MagicMock()
    client.get_event = AsyncMock(side_effect=ERClientNotFound("missing", status_code=404))
    assert await site(client).get_event("e1") is None


@pytest.mark.asyncio
async def test_network_failure_is_a_halting_connection_error():
    client = MagicMock()
    client.get_me = AsyncMock(side_effect=ERClientException("down"))
    with pytest.raises(IntegrationConnectionError) as info:
        await site(client, role="source").get_me()
    assert info.value.site_role == "source"
    assert info.value.message.startswith("Source site siteb.pamdas.org: ")
    assert halts_site(info.value)


@pytest.mark.asyncio
async def test_iter_events_asks_oldest_first_and_translates_mid_stream_failures():
    captured = {}

    async def get_events(**kwargs):
        captured.update(kwargs)
        yield [{"id": "a"}]
        raise ERClientException("down")

    client = MagicMock()
    client.get_events = get_events
    pages = []
    with pytest.raises(IntegrationConnectionError):
        async for page in site(client).iter_events_updated_since(datetime(2026, 1, 1, tzinfo=timezone.utc)):
            pages.append(page)
    assert pages == [[{"id": "a"}]]
    assert captured["sort_by"] == "updated_at"
    assert json.loads(captured["filter"]) == {"update_date": {"lower": "2026-01-01T00:00:00+00:00"}}


@pytest.mark.asyncio
async def test_post_sharing_status_posts_to_provider_endpoint():
    client = MagicMock()
    client._post = AsyncMock(return_value={})
    await site(client).post_sharing_status("das_provider", [{"a": 1}])
    client._post.assert_awaited_once_with("sensors/sharing/das_provider/status", payload=[{"a": 1}])


@pytest.mark.asyncio
async def test_list_endpoints_accept_paginated_or_plain_lists():
    client = MagicMock()
    client._get = AsyncMock(return_value={"results": [{"id": "fg1", "name": "Park"}]})
    assert await site(client).list_feature_groups() == [{"id": "fg1", "name": "Park"}]
    client.get_source_assignments = AsyncMock(return_value=[{"subject": "s1"}])
    assert await site(client).get_source_assignments(["s1"]) == [{"subject": "s1"}]


def test_build_er_client_targets_site_api_root():
    client = build_er_client("https://SiteB.pamdas.org/", AuthenticateConfig(token="t"))
    assert client.service_root == "https://siteb.pamdas.org/api/v1.0"


def test_open_site_rejects_url_without_host():
    with pytest.raises(IntegrationConfigurationError) as info:
        open_site("https://", AuthenticateConfig(token="t"), role="destination")
    assert info.value.site_role == "destination"


def test_auth_config_of_reads_the_auth_action_config():
    configurations = [
        SimpleNamespace(action=SimpleNamespace(value="sync_events"), data={}),
        SimpleNamespace(action=SimpleNamespace(value="auth"), data={"token": "abc"}),
    ]
    assert auth_config_of(configurations).token.get_secret_value() == "abc"
    with pytest.raises(IntegrationConfigurationError):
        auth_config_of([])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_clients.py -v`
Expected: `ModuleNotFoundError: No module named 'app.er2er.clients'`.

- [ ] **Step 3: Implement `app/er2er/clients.py`**

The error translation is copied from `gundi-integration-earthranger` (`app/actions/handlers.py:1461-1552` at `fa5d748`), so both connectors classify ER failures the same way.

```python
"""EarthRanger access for both sides of a share.

Every ERSite call raises an IntegrationError subclass labelled with the site and
carrying `site_role`, never erclient or httpx exceptions (spec §9).
"""
import json
import logging
import os
import tempfile
from datetime import datetime
from typing import AsyncIterator, List, Optional

import httpx
from erclient import AsyncERClient, ERClientException, VERSION_2_0
from erclient.er_errors import ERClientBadRequest

from app.actions.configurations import AuthenticateConfig
from app.services.errors import (
    IntegrationAuthError,
    IntegrationBadResponseError,
    IntegrationConfigurationError,
    IntegrationConnectionError,
    IntegrationError,
    IntegrationRateLimitError,
)
from app.services.utils import find_config_for_action
from .naming import site_host, site_root

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


def build_er_client(url: str, auth: AuthenticateConfig) -> AsyncERClient:
    root = site_root(url)
    client = AsyncERClient(
        service_root=f"{root}/api/v1.0",
        username=auth.username or None,
        password=auth.password.get_secret_value() if auth.password else None,
        token=auth.token.get_secret_value() if auth.token else None,
        token_url=f"{root}/oauth2/token",
        client_id="das_web_client",
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
    )
    # erclient raises on the token endpoint's 400 instead of returning False, which
    # would skip its fallback login when a refresh token goes stale mid-run.
    original_refresh = client.refresh_token

    async def refresh_or_fall_back_to_login(*args, **kwargs):
        try:
            return await original_refresh(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                return False
            raise

    client.refresh_token = refresh_or_fall_back_to_login
    return client


def _is_token_endpoint_url(url) -> bool:
    return "/oauth2/token" in str(url)


def _status_of(exc: Exception):
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _is_rejected_login(exc) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return _is_token_endpoint_url(exc.request.url) and exc.response.status_code in (400, 401)
    return isinstance(exc, ERClientBadRequest) and bool(exc.args) and _is_token_endpoint_url(exc.args[0])


_ER_CALL_FAILURES = (ERClientException, httpx.HTTPError, ValueError, KeyError)


def as_integration_error(exc: Exception, *, not_found_is_configuration: bool = True) -> IntegrationError:
    """Translate an ER call failure into the runner's classified errors. Messages
    are fixed text: str(exc) can carry the request (for a login, the password)."""
    if _is_rejected_login(exc):
        return IntegrationAuthError("EarthRanger rejected the credentials", status_code=401)
    status = _status_of(exc)
    if isinstance(exc, httpx.TransportError) or (type(exc) is ERClientException and status is None):
        return IntegrationConnectionError("EarthRanger could not be reached")
    if status is None:
        return IntegrationBadResponseError("EarthRanger returned an unexpected response")
    if status == 401:
        return IntegrationAuthError("EarthRanger rejected the credentials", status_code=status)
    if status == 403:
        return IntegrationAuthError("EarthRanger denied access with these credentials", status_code=status)
    if status == 404 and not_found_is_configuration:
        return IntegrationConfigurationError(
            "EarthRanger does not expose the API this action needs; check the site URL and EarthRanger version."
        )
    if status in (429, 409):
        return IntegrationRateLimitError("EarthRanger rate-limited the request", status_code=status)
    if status in (502, 503, 504):
        return IntegrationConnectionError("EarthRanger could not be reached", status_code=status)
    if 300 <= status < 400:
        return IntegrationConfigurationError("Site URL redirects elsewhere; use the site's https URL.")
    return IntegrationBadResponseError("EarthRanger returned an unexpected response", status_code=status)


def halts_site(error: IntegrationError) -> bool:
    """True when nothing more can be done on that site in this run (credentials,
    reachability, rate limit, misconfiguration, server error). False for a
    rejected item, which is skipped and reported."""
    if isinstance(error, (IntegrationAuthError, IntegrationConnectionError,
                          IntegrationRateLimitError, IntegrationConfigurationError)):
        return True
    return (error.status_code or 0) >= 500


def auth_config_of(configurations) -> AuthenticateConfig:
    config = find_config_for_action(configurations or [], "auth")
    if config is None or not config.data:
        raise IntegrationConfigurationError("The connection has no EarthRanger credentials; open its settings and save them.")
    return AuthenticateConfig.parse_obj(config.data)


def _results(data) -> list:
    if isinstance(data, dict):
        return data.get("results", [])
    return data or []


class ERSite:
    def __init__(self, client: AsyncERClient, url: str, *, role: str):
        self._client = client
        self.url = site_root(url)
        self.service_root = f"{self.url}/api/v1.0"
        self.role = role
        self.label = f"{'Source' if role == 'source' else 'Destination'} site {site_host(url)}"

    async def __aenter__(self) -> "ERSite":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        await self._client.__aexit__(*exc_info)

    def _translate(self, exc: Exception) -> IntegrationError:
        err = as_integration_error(exc, not_found_is_configuration=False)
        message = f"{self.label}: {err.message}"
        body = getattr(exc, "response_body", None)
        if err.status_code == 400 and body:
            message = f"{message} — {str(body)[:300]}"
        labelled = type(err)(message, status_code=err.status_code)
        labelled.site_role = self.role
        return labelled

    async def _call(self, awaitable, *, none_on_404: bool = False):
        try:
            return await awaitable
        except _ER_CALL_FAILURES as e:
            if none_on_404 and _status_of(e) == 404:
                return None
            raise self._translate(e) from e

    async def get_me(self) -> dict:
        return await self._call(self._client.get_me())

    async def iter_events_updated_since(
        self, since: datetime, *, page_size: int = 100, with_notes_and_files: bool = True
    ) -> AsyncIterator[List[dict]]:
        events = self._client.get_events(
            filter=json.dumps({"update_date": {"lower": since.isoformat()}}),
            sort_by="updated_at",
            include_details=True,
            include_notes=with_notes_and_files,
            include_files=with_notes_and_files,
            include_updates=False,
            include_related_events=False,
            page_size=page_size,
            batch_size=page_size,
        )
        try:
            async for page in events:
                yield page
        except _ER_CALL_FAILURES as e:
            raise self._translate(e) from e

    async def get_event(self, event_id: str, *, with_notes_and_files: bool = False) -> Optional[dict]:
        return await self._call(
            self._client.get_event(
                event_id=event_id, include_details=True,
                include_notes=with_notes_and_files, include_files=with_notes_and_files,
            ),
            none_on_404=True,
        )

    async def post_event(self, event: dict) -> dict:
        return await self._call(self._client.post_report(event))

    async def patch_event(self, event_id: str, data: dict) -> dict:
        return await self._call(self._client.patch_report(event_id, data))

    async def delete_event(self, event_id: str):
        return await self._call(self._client.delete_event(event_id))

    async def post_note(self, event_id: str, text: str) -> dict:
        created = await self._call(self._client.post_event_note(event_id, text))
        return created[0] if isinstance(created, list) else created

    async def patch_note(self, event_id: str, note_id: str, text: str):
        return await self._call(self._client._patch(f"activity/event/{event_id}/note/{note_id}", payload={"text": text}))

    async def delete_note(self, event_id: str, note_id: str):
        return await self._call(self._client.delete_event_note(event_id, note_id))

    async def download_file(self, url: str) -> bytes:
        response = await self._call(self._client.get_file(url))
        return response.content

    async def post_file(self, event_id: str, filename: str, content: bytes, comment: str = "") -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, os.path.basename(filename) or "attachment")
            with open(path, "wb") as f:
                f.write(content)
            return await self._call(self._client.post_event_file(event_id, filepath=path, comment=comment))

    async def delete_file(self, event_id: str, file_id: str):
        return await self._call(self._client.delete_event_file(event_id, file_id))

    async def add_to_incident(self, event_id: str, incident_id: str):
        return await self._call(self._client.add_event_to_incident(event_id, incident_id))

    async def remove_from_incident(self, event_id: str, incident_id: str):
        return await self._call(self._client.remove_event_from_incident(event_id, incident_id))

    async def get_event_categories(self) -> List[dict]:
        return _results(await self._call(self._client.get_event_categories(include_inactive=True)))

    async def post_event_category(self, data: dict):
        return await self._call(self._client.post_event_category(data))

    async def patch_event_category(self, data: dict):
        return await self._call(self._client.patch_event_category(data))

    async def list_event_types_v2(self) -> List[dict]:
        return _results(await self._call(self._client.get_event_types(include_schema=True, version=VERSION_2_0)))

    async def get_event_type_v2(self, value: str) -> Optional[dict]:
        return await self._call(
            self._client.get_event_type(value, version=VERSION_2_0, include_schema=True), none_on_404=True
        )

    async def post_event_type_v2(self, data: dict):
        return await self._call(self._client.post_event_type(data, version=VERSION_2_0))

    async def patch_event_type_v2(self, data: dict):
        return await self._call(self._client.patch_event_type(data, version=VERSION_2_0))

    async def list_subject_groups(self) -> List[dict]:
        return _results(await self._call(
            self._client.get_subjectgroups(include_inactive=False, include_hidden=True, flat=True)
        ))

    async def get_source_assignments(self, subject_ids: List[str]) -> List[dict]:
        return _results(await self._call(self._client.get_source_assignments(subject_ids=list(subject_ids))))

    async def get_subject_sources(self, subject_id: str) -> List[dict]:
        return _results(await self._call(self._client.get_subject_sources(subject_id)))

    async def get_observations(self, subject_id: str, start: datetime) -> List[dict]:
        out = []
        observations = self._client.get_observations(
            subject_id=subject_id, start=start, filter_flag=0, include_details=True
        )
        try:
            async for observation in observations:
                out.append(observation)
        except _ER_CALL_FAILURES as e:
            raise self._translate(e) from e
        return out

    async def list_feature_groups(self) -> List[dict]:
        return _results(await self._call(self._client._get("spatialfeaturegroup", params={})))

    async def get_feature_group(self, feature_group_id: str) -> dict:
        return await self._call(self._client.get_feature_group(feature_group_id))

    async def post_sharing_status(self, provider_key: str, payloads: List[dict]):
        return await self._call(self._client._post(f"sensors/sharing/{provider_key}/status", payload=payloads))


def open_site(url: str, auth: AuthenticateConfig, role: str) -> ERSite:
    try:
        return ERSite(build_er_client(url, auth), url, role=role)
    except ValueError as e:
        err = IntegrationConfigurationError(f"{role.capitalize()} site URL has no hostname; fix the site URL.")
        err.site_role = role
        raise err from e
```

- [ ] **Step 4: Implement `app/er2er/tests/fakes.py`**

Later tasks use this fake as their only ER test double. It mirrors `ERSite`'s interface.

```python
"""In-memory test doubles for ERSite and IntegrationStateManager."""
import copy
import itertools
import json
import uuid
from typing import Callable, Dict, List, Optional, Union

from app.er2er.naming import site_host, site_root
from app.er2er.timeutil import parse_dt
from app.services.errors import IntegrationBadResponseError, IntegrationError

Failure = Union[Exception, Callable[..., Optional[Exception]]]


def make_event(event_id: str, updated_at: str, event_type: str = "wildlife_sighting", **extra) -> dict:
    event = {
        "id": event_id,
        "serial_number": abs(hash(event_id)) % 100000,
        "event_type": event_type,
        "title": None,
        "priority": 0,
        "state": "new",
        "time": updated_at,
        "updated_at": updated_at,
        "location": {"latitude": 1.0, "longitude": 1.0},
        "geojson": {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0, 1.0]}},
        "event_details": {},
        "notes": [],
        "files": [],
        "is_contained_in": [],
    }
    event.update(extra)
    return event


def make_observation(source_id: str, recorded_at: str, created_at: Optional[str] = None,
                     lat: float = 1.0, lon: float = 1.0, details: Optional[dict] = None) -> dict:
    observation = {
        "id": str(uuid.uuid4()),
        "source": source_id,
        "recorded_at": recorded_at,
        "location": {"latitude": lat, "longitude": lon},
        "observation_details": details,
    }
    if created_at:
        observation["created_at"] = created_at
    return observation


def not_found(label: str = "Destination site fake") -> IntegrationBadResponseError:
    err = IntegrationBadResponseError(f"{label}: EarthRanger returned an unexpected response", status_code=404)
    err.site_role = "destination"
    return err


class FakeSite:
    def __init__(self, url: str = "https://siteb.pamdas.org", role: str = "destination"):
        self.url = site_root(url)
        self.service_root = f"{self.url}/api/v1.0"
        self.role = role
        self.label = f"{'Source' if role == 'source' else 'Destination'} site {site_host(url)}"
        self.me = {"username": "svc", "is_active": True, "permissions": {}}
        self.events: Dict[str, dict] = {}
        self.event_types: Dict[str, dict] = {}
        self.categories: Dict[str, dict] = {}
        self.subject_groups: List[dict] = []
        self.assignments: List[dict] = []
        self.subject_sources: Dict[str, List[dict]] = {}
        self.observations: Dict[str, List[dict]] = {}
        self.feature_groups: Dict[str, dict] = {}
        self.files: Dict[str, bytes] = {}
        self.sharing_posts: List[tuple] = []
        self.calls: List[tuple] = []
        self.failures: Dict[str, Failure] = {}
        self._ids = itertools.count(1)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    def _hit(self, name: str, *args):
        self.calls.append((name, args))
        failure = self.failures.get(name)
        if failure is None:
            return
        exc = failure if isinstance(failure, Exception) else failure(*args)
        if exc is not None:
            raise exc

    def calls_to(self, name: str) -> List[tuple]:
        return [args for called, args in self.calls if called == name]

    def _new_id(self) -> str:
        return f"{self.role}-{next(self._ids)}"

    def _event(self, event_id: str) -> dict:
        if event_id not in self.events:
            raise not_found(self.label)
        return self.events[event_id]

    async def get_me(self):
        self._hit("get_me")
        return self.me

    async def iter_events_updated_since(self, since, *, page_size=100, with_notes_and_files=True):
        self._hit("iter_events_updated_since", since)
        matching = sorted(
            (e for e in self.events.values() if parse_dt(e["updated_at"]) >= since),
            key=lambda e: parse_dt(e["updated_at"]),
        )
        for i in range(0, len(matching), page_size):
            yield copy.deepcopy(matching[i:i + page_size])

    async def get_event(self, event_id, *, with_notes_and_files=False):
        self._hit("get_event", event_id)
        return copy.deepcopy(self.events.get(event_id))

    async def post_event(self, event):
        self._hit("post_event", event)
        stored = {"notes": [], "files": [], "is_contained_in": [], **copy.deepcopy(event)}
        stored.setdefault("id", self._new_id())
        stored.setdefault("updated_at", "2026-10-15T00:00:00+00:00")
        self.events[stored["id"]] = stored
        return copy.deepcopy(stored)

    async def patch_event(self, event_id, data):
        self._hit("patch_event", event_id, data)
        self._event(event_id).update(copy.deepcopy(data))
        return copy.deepcopy(self.events[event_id])

    async def delete_event(self, event_id):
        self._hit("delete_event", event_id)
        self._event(event_id)
        del self.events[event_id]

    async def post_note(self, event_id, text):
        self._hit("post_note", event_id, text)
        note = {"id": self._new_id(), "text": text}
        self._event(event_id)["notes"].append(note)
        return dict(note)

    async def patch_note(self, event_id, note_id, text):
        self._hit("patch_note", event_id, note_id, text)
        for note in self._event(event_id)["notes"]:
            if note["id"] == note_id:
                note["text"] = text
                return dict(note)
        raise not_found(self.label)

    async def delete_note(self, event_id, note_id):
        self._hit("delete_note", event_id, note_id)
        notes = self._event(event_id)["notes"]
        self._event(event_id)["notes"] = [n for n in notes if n["id"] != note_id]

    async def download_file(self, url):
        self._hit("download_file", url)
        return self.files[url]

    async def post_file(self, event_id, filename, content, comment=""):
        self._hit("post_file", event_id, filename)
        file = {"id": self._new_id(), "filename": filename}
        self._event(event_id)["files"].append(file)
        return dict(file)

    async def delete_file(self, event_id, file_id):
        self._hit("delete_file", event_id, file_id)
        files = self._event(event_id)["files"]
        self._event(event_id)["files"] = [f for f in files if f["id"] != file_id]

    async def add_to_incident(self, event_id, incident_id):
        self._hit("add_to_incident", event_id, incident_id)
        self._event(incident_id)
        self._event(event_id)["is_contained_in"].append({"type": "contains", "related_event": {"id": incident_id}})

    async def remove_from_incident(self, event_id, incident_id):
        self._hit("remove_from_incident", event_id, incident_id)
        event = self._event(event_id)
        event["is_contained_in"] = [c for c in event["is_contained_in"] if c["related_event"]["id"] != incident_id]

    async def get_event_categories(self):
        self._hit("get_event_categories")
        return copy.deepcopy(list(self.categories.values()))

    async def post_event_category(self, data):
        self._hit("post_event_category", data)
        self.categories[data["value"]] = copy.deepcopy(data)

    async def patch_event_category(self, data):
        self._hit("patch_event_category", data)
        self.categories[data["value"]] = copy.deepcopy(data)

    async def list_event_types_v2(self):
        self._hit("list_event_types_v2")
        return copy.deepcopy(list(self.event_types.values()))

    async def get_event_type_v2(self, value):
        self._hit("get_event_type_v2", value)
        return copy.deepcopy(self.event_types.get(value))

    async def post_event_type_v2(self, data):
        self._hit("post_event_type_v2", data)
        self.event_types[data["value"]] = copy.deepcopy(data)

    async def patch_event_type_v2(self, data):
        self._hit("patch_event_type_v2", data)
        self.event_types[data["value"]] = copy.deepcopy(data)

    async def list_subject_groups(self):
        self._hit("list_subject_groups")
        return copy.deepcopy(self.subject_groups)

    async def get_source_assignments(self, subject_ids):
        self._hit("get_source_assignments", tuple(subject_ids))
        return [a for a in self.assignments if str(a["subject"]) in set(subject_ids)]

    async def get_subject_sources(self, subject_id):
        self._hit("get_subject_sources", subject_id)
        return copy.deepcopy(self.subject_sources.get(subject_id, []))

    async def get_observations(self, subject_id, start):
        self._hit("get_observations", subject_id, start)
        return sorted(
            (copy.deepcopy(o) for o in self.observations.get(subject_id, []) if parse_dt(o["recorded_at"]) >= start),
            key=lambda o: parse_dt(o["recorded_at"]),
        )

    async def list_feature_groups(self):
        self._hit("list_feature_groups")
        return [{"id": fg["id"], "name": fg.get("name")} for fg in self.feature_groups.values()]

    async def get_feature_group(self, feature_group_id):
        self._hit("get_feature_group", feature_group_id)
        if feature_group_id not in self.feature_groups:
            raise not_found(self.label)
        return copy.deepcopy(self.feature_groups[feature_group_id])

    async def post_sharing_status(self, provider_key, payloads):
        self._hit("post_sharing_status", provider_key, payloads)
        self.sharing_posts.append((provider_key, copy.deepcopy(payloads)))


class InMemoryStateManager:
    """Same async interface as app.services.state.IntegrationStateManager."""

    def __init__(self):
        self.data: Dict[tuple, object] = {}

    async def get_state(self, integration_id, action_id, source_id="no-source"):
        return copy.deepcopy(self.data.get((integration_id, action_id, source_id), {}))

    async def set_state(self, integration_id, action_id, state, source_id="no-source"):
        self.data[(integration_id, action_id, source_id)] = json.loads(json.dumps(state, default=str))

    async def set_if_absent(self, integration_id, action_id, *, ttl_seconds, source_id="no-source"):
        key = (integration_id, action_id, source_id)
        if key in self.data:
            return False
        self.data[key] = "1"
        return True

    async def delete_state(self, integration_id, action_id, source_id="no-source"):
        self.data.pop((integration_id, action_id, source_id), None)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_clients.py -v`
Expected: all PASS. If `test_build_er_client_targets_site_api_root` fails on the attribute name, inspect `AsyncERClient.__init__` in the installed `erclient/client.py` and assert on the attribute it actually stores for `service_root`. Don't change the production code.

- [ ] **Step 6: Commit**

```bash
git add app/er2er/clients.py app/er2er/tests/fakes.py app/er2er/tests/test_clients.py
git commit -m "feat: add labelled ER site access with classified errors"
```

---

### Task 3: Destination resolution

**Files:**
- Create: `app/er2er/destinations.py`
- Test: `app/er2er/tests/test_destinations.py`

**Interfaces:**
- Consumes: `AuthenticateConfig` (Task 1), `same_site` (Task 1)
- Produces:
  - `EARTHRANGER_TYPE = "earth_ranger"`
  - `Destination(id: str, name: str, url: str, auth: AuthenticateConfig)`
  - `SkippedDestination(id: str, name: str, reason: str)`
  - `async resolve_destinations(connection_id: str, source_url: str, gundi=None) -> Tuple[List[Destination], List[SkippedDestination]]`. Pass `gundi` in tests; production code builds a `GundiClient()`.

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_destinations.py`:

```python
from types import SimpleNamespace

import pytest

from app.er2er.destinations import resolve_destinations

SOURCE = "https://sitea.pamdas.org"


def dest(id, base_url, type_value="earth_ranger", name=None):
    return SimpleNamespace(id=id, name=name or id, base_url=base_url, type=SimpleNamespace(value=type_value))


def details(base_url, auth_data):
    configurations = [] if auth_data is None else [
        SimpleNamespace(action=SimpleNamespace(value="auth"), data=auth_data)
    ]
    return SimpleNamespace(base_url=base_url, configurations=configurations)


class FakeGundi:
    def __init__(self, destinations, details_by_id):
        self.connection = SimpleNamespace(destinations=destinations)
        self.details_by_id = details_by_id

    async def get_connection_details(self, connection_id):
        return self.connection

    async def get_integration_details(self, integration_id):
        found = self.details_by_id[str(integration_id)]
        if isinstance(found, Exception):
            raise found
        return found


@pytest.mark.asyncio
async def test_resolves_earthranger_destinations_with_their_credentials():
    gundi = FakeGundi([dest("d1", "https://siteb.pamdas.org", name="Site B")],
                      {"d1": details("https://siteb.pamdas.org", {"token": "t"})})
    destinations, skipped = await resolve_destinations("c1", SOURCE, gundi=gundi)
    assert skipped == []
    assert [(d.id, d.name, d.url) for d in destinations] == [("d1", "Site B", "https://siteb.pamdas.org")]
    assert destinations[0].auth.token.get_secret_value() == "t"


@pytest.mark.asyncio
async def test_skips_unusable_destinations_with_reasons():
    gundi = FakeGundi(
        [
            dest("smart", "https://smart.example.org", type_value="smart_connect"),
            dest("self", "https://SiteA.pamdas.org/"),
            dest("nocreds", "https://sitec.pamdas.org"),
            dest("hidden", "https://sited.pamdas.org"),
        ],
        {
            "nocreds": details("https://sitec.pamdas.org", None),
            "hidden": RuntimeError("403 from Gundi"),
        },
    )
    destinations, skipped = await resolve_destinations("c1", SOURCE, gundi=gundi)
    assert destinations == []
    assert {s.id: s.reason for s in skipped} == {
        "smart": "not an EarthRanger destination",
        "self": "same site as the source",
        "nocreds": "has no EarthRanger credentials",
        "hidden": "its credentials could not be read from Gundi",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_destinations.py -v`
Expected: `ModuleNotFoundError: No module named 'app.er2er.destinations'`.

- [ ] **Step 3: Implement `app/er2er/destinations.py`**

```python
"""The connection's EarthRanger destinations and their stored credentials (spec §5)."""
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import List, Tuple

from gundi_client_v2 import GundiClient

from app.actions.configurations import AuthenticateConfig
from app.services.utils import find_config_for_action
from .naming import same_site

logger = logging.getLogger(__name__)

EARTHRANGER_TYPE = "earth_ranger"


@dataclass(frozen=True)
class Destination:
    id: str
    name: str
    url: str
    auth: AuthenticateConfig


@dataclass(frozen=True)
class SkippedDestination:
    id: str
    name: str
    reason: str


@asynccontextmanager
async def _gundi_client(client):
    if client is not None:
        yield client
        return
    async with GundiClient() as owned:
        yield owned


async def resolve_destinations(connection_id: str, source_url: str, gundi=None
                               ) -> Tuple[List[Destination], List[SkippedDestination]]:
    destinations, skipped = [], []
    async with _gundi_client(gundi) as client:
        connection = await client.get_connection_details(connection_id)
        for item in connection.destinations or []:
            item_id, name = str(item.id), item.name or str(item.id)
            type_value = item.type.value if item.type else ""
            if type_value != EARTHRANGER_TYPE:
                skipped.append(SkippedDestination(item_id, name, "not an EarthRanger destination"))
                continue
            if item.base_url and same_site(item.base_url, source_url):
                skipped.append(SkippedDestination(item_id, name, "same site as the source"))
                continue
            try:
                details = await client.get_integration_details(item_id)
            except Exception:
                logger.exception("Could not read destination %s from Gundi", item_id)
                skipped.append(SkippedDestination(item_id, name, "its credentials could not be read from Gundi"))
                continue
            auth = find_config_for_action(details.configurations or [], "auth")
            if auth is None or not auth.data:
                skipped.append(SkippedDestination(item_id, name, "has no EarthRanger credentials"))
                continue
            destinations.append(Destination(item_id, name, details.base_url or item.base_url,
                                            AuthenticateConfig.parse_obj(auth.data)))
    return destinations, skipped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_destinations.py -v`
Expected: all PASS.

- [ ] **Step 5: Verify service-account access (spec §14 risk)**

In the dev environment, run this with the runner's service-account credentials (the same env vars the deployed runner uses):

```bash
python -c "import asyncio; from gundi_client_v2 import GundiClient
async def m():
    async with GundiClient() as g:
        d = await g.get_integration_details('<a dev earth_ranger destination id>')
        print([c.action.value for c in d.configurations])
asyncio.run(m())"
```

Expected: the list includes `auth`. If you get 403 or `auth` is missing, stop and report back. A narrow cdip permission is needed before any end-to-end test.

- [ ] **Step 6: Commit**

```bash
git add app/er2er/destinations.py app/er2er/tests/test_destinations.py
git commit -m "feat: resolve a connection's EarthRanger destinations"
```

---

### Task 4: Share state and run reporting

**Files:**
- Create: `app/er2er/state.py`, `app/er2er/reporting.py`
- Test: `app/er2er/tests/test_state_and_reporting.py`

**Interfaces:**
- Consumes: `InMemoryStateManager` (Task 2 fakes), `IntegrationStateManager`, `log_action_activity`
- Produces (`state.py`):
  - constants `EVENTS_ACTION = "sync_events"`, `OBSERVATIONS_ACTION = "sync_observations"`
  - `Marker(since: datetime, advanced_at: datetime, created_since: Optional[datetime] = None)`
  - `NoteRecord(dest_id: str, digest: str)`
  - `EventRecord(dest_event_id: str, updated_at: datetime, notes: Dict[str, NoteRecord] = {}, files: Dict[str, str] = {}, incidents: List[str] = [])`
  - `ShareState(integration_id: str, manager=None)` with async methods:
    - markers: `get_marker(action_id, key) -> Optional[Marker]`, `set_marker(action_id, key, marker)`
    - event records: `get_event_record(destination_id, src_event_id) -> Optional[EventRecord]`, `set_event_record(destination_id, src_event_id, record)`, `delete_event_record(destination_id, src_event_id)`
    - leases: `try_lease(action_id, ttl_seconds) -> bool`, `release_lease(action_id)`
    - once-per gates: `once_per(action_id, key, ttl_seconds) -> bool`, `forget_once(action_id, key)`
- Produces (`reporting.py`):
  - `STALL_AFTER = timedelta(hours=6)`
  - `ErrorGroup(title, level, count, examples)`
  - `DestinationReport(destination: str, kind: str)` with:
    - fields `created`, `updated`, `deleted`, `skipped`, `failed`, `marker`, `timed_out`, `errors`
    - methods `error(title, item=None, level=LogLevel.ERROR)`, `summary_title()`, `as_dict()`
  - `async publish_report(integration_id, action_id, report)`
  - `is_stalled(marker: Optional[Marker], now: datetime) -> bool`

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_state_and_reporting.py`:

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from gundi_core.events import LogLevel

from app.er2er import reporting
from app.er2er.reporting import DestinationReport, is_stalled, publish_report
from app.er2er.state import EVENTS_ACTION, EventRecord, Marker, NoteRecord, ShareState
from app.er2er.tests.fakes import InMemoryStateManager

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_markers_and_event_records_round_trip():
    state = ShareState("i1", manager=InMemoryStateManager())
    assert await state.get_marker(EVENTS_ACTION, "d1:marker") is None
    await state.set_marker(EVENTS_ACTION, "d1:marker", Marker(since=T0, advanced_at=T0))
    assert (await state.get_marker(EVENTS_ACTION, "d1:marker")).since == T0

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
    await state.forget_once(EVENTS_ACTION, "d1:reconcile")
    assert await state.once_per(EVENTS_ACTION, "d1:reconcile", 86400) is True


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_state_and_reporting.py -v`
Expected: `ModuleNotFoundError: No module named 'app.er2er.state'`.

- [ ] **Step 3: Implement `app/er2er/state.py`**

```python
"""Per-destination progress kept in Redis (spec §7.1, §8)."""
import json
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.services.state import IntegrationStateManager

EVENTS_ACTION = "sync_events"
OBSERVATIONS_ACTION = "sync_observations"


class Marker(BaseModel):
    since: datetime                          # resume reading here
    advanced_at: datetime                    # when `since` last moved (stall detection)
    created_since: Optional[datetime] = None  # observations: high-water mark of created_at


class NoteRecord(BaseModel):
    dest_id: str
    digest: str


class EventRecord(BaseModel):
    dest_event_id: str
    updated_at: datetime
    notes: Dict[str, NoteRecord] = Field(default_factory=dict)  # source note id -> copy
    files: Dict[str, str] = Field(default_factory=dict)         # source file id -> dest file id ("" = not copied)
    incidents: List[str] = Field(default_factory=list)          # incident ids linked at the destination


def _dump(model: BaseModel) -> dict:
    return json.loads(model.json())


class ShareState:
    def __init__(self, integration_id: str, manager=None):
        self.integration_id = integration_id
        self._manager = manager or IntegrationStateManager()

    async def get_marker(self, action_id: str, key: str) -> Optional[Marker]:
        data = await self._manager.get_state(self.integration_id, action_id, source_id=key)
        return Marker.parse_obj(data) if data else None

    async def set_marker(self, action_id: str, key: str, marker: Marker):
        await self._manager.set_state(self.integration_id, action_id, _dump(marker), source_id=key)

    async def get_event_record(self, destination_id: str, src_event_id: str) -> Optional[EventRecord]:
        data = await self._manager.get_state(self.integration_id, EVENTS_ACTION,
                                             source_id=f"{destination_id}:event:{src_event_id}")
        return EventRecord.parse_obj(data) if data else None

    async def set_event_record(self, destination_id: str, src_event_id: str, record: EventRecord):
        await self._manager.set_state(self.integration_id, EVENTS_ACTION, _dump(record),
                                      source_id=f"{destination_id}:event:{src_event_id}")

    async def delete_event_record(self, destination_id: str, src_event_id: str):
        await self._manager.delete_state(self.integration_id, EVENTS_ACTION,
                                         source_id=f"{destination_id}:event:{src_event_id}")

    async def try_lease(self, action_id: str, ttl_seconds: int) -> bool:
        return await self._manager.set_if_absent(self.integration_id, action_id,
                                                 ttl_seconds=ttl_seconds, source_id="lease")

    async def release_lease(self, action_id: str):
        await self._manager.delete_state(self.integration_id, action_id, source_id="lease")

    async def once_per(self, action_id: str, key: str, ttl_seconds: int) -> bool:
        return await self._manager.set_if_absent(self.integration_id, action_id,
                                                 ttl_seconds=ttl_seconds, source_id=f"once:{key}")

    async def forget_once(self, action_id: str, key: str):
        await self._manager.delete_state(self.integration_id, action_id, source_id=f"once:{key}")
```

- [ ] **Step 4: Implement `app/er2er/reporting.py`**

```python
"""What admins see in the activity log for each run and destination (spec §9)."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from gundi_core.events import LogLevel

from app.services.activity_logger import log_action_activity
from .state import Marker

STALL_AFTER = timedelta(hours=6)
MAX_EXAMPLES = 5


@dataclass
class ErrorGroup:
    title: str
    level: LogLevel
    count: int = 0
    examples: List[str] = field(default_factory=list)


@dataclass
class DestinationReport:
    destination: str
    kind: str  # "Events" or "Tracks"
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    marker: Optional[datetime] = None
    timed_out: bool = False
    errors: Dict[str, ErrorGroup] = field(default_factory=dict)

    def error(self, title: str, item: Optional[str] = None, level: LogLevel = LogLevel.ERROR):
        group = self.errors.setdefault(title, ErrorGroup(title=title, level=level))
        group.count += 1
        if item is not None and len(group.examples) < MAX_EXAMPLES:
            group.examples.append(item)

    def summary_title(self) -> str:
        if self.kind == "Tracks":
            counts = f"{self.created} points shared, {self.failed} failed"
        else:
            counts = (f"{self.created} created, {self.updated} updated, {self.deleted} deleted, "
                      f"{self.skipped} skipped, {self.failed} failed")
        title = f"{self.kind} → {self.destination}: {counts}"
        if self.marker:
            title += f"; synced up to {self.marker:%Y-%m-%d %H:%M} UTC"
        if self.timed_out:
            title += "; continuing in a follow-up run"
        return title

    def as_dict(self) -> dict:
        data = asdict(self)
        data["marker"] = self.marker.isoformat() if self.marker else None
        data["errors"] = {k: {"count": g.count, "examples": g.examples} for k, g in self.errors.items()}
        return data


async def publish_report(integration_id: str, action_id: str, report: DestinationReport):
    level = LogLevel.WARNING if report.failed or report.errors else LogLevel.INFO
    await log_action_activity(integration_id=integration_id, action_id=action_id,
                              title=report.summary_title(), level=level, data=report.as_dict())
    for group in report.errors.values():
        await log_action_activity(integration_id=integration_id, action_id=action_id,
                                  title=f"{report.kind} → {report.destination}: {group.title}",
                                  level=group.level, data={"count": group.count, "examples": group.examples})


def is_stalled(marker: Optional[Marker], now: datetime) -> bool:
    return marker is not None and now - marker.advanced_at > STALL_AFTER
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_state_and_reporting.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/er2er/state.py app/er2er/reporting.py app/er2er/tests/test_state_and_reporting.py
git commit -m "feat: add per-destination share state and run reports"
```

---

### Task 5: Area filters

**Files:**
- Create: `app/er2er/geo.py`
- Test: `app/er2er/tests/test_geo.py`

**Interfaces:**
- Consumes: `FakeSite.get_feature_group` / `ERSite.get_feature_group` (Task 2)
- Produces:
  - `Area(shapes)` with `contains_point(lon, lat) -> bool` and `intersects_event(event) -> bool`
  - `shapes_from_feature_group(feature_group: dict) -> list`
  - `async load_area(site, feature_group_ids: Sequence[str]) -> Optional[Area]`: None when no ids are given; raises `IntegrationConfigurationError` on a missing or empty group

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_geo.py`:

```python
import pytest

from app.er2er.geo import load_area, shapes_from_feature_group
from app.er2er.tests.fakes import FakeSite, make_event
from app.services.errors import IntegrationConfigurationError

# ER nests GeoJSON features one level down inside each spatial feature.
SQUARE = {"id": "fg1", "name": "Park", "features": [{"name": "Park", "features": [
    {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]]}}
]}]}


def source_with_square():
    site = FakeSite("https://sitea.pamdas.org", role="source")
    site.feature_groups["fg1"] = SQUARE
    return site


def test_shapes_are_read_from_nested_features():
    assert len(shapes_from_feature_group(SQUARE)) == 1


@pytest.mark.asyncio
async def test_area_filters_points_and_events():
    area = await load_area(source_with_square(), ["fg1"])
    assert area.contains_point(5, 5) and not area.contains_point(20, 5)
    inside = make_event("e1", "2026-10-01T00:00:00Z")
    outside = make_event("e2", "2026-10-01T00:00:00Z",
                         geojson={"type": "Feature", "geometry": {"type": "Point", "coordinates": [20, 5]}})
    no_geometry = make_event("e3", "2026-10-01T00:00:00Z", geojson=None)
    assert area.intersects_event(inside)
    assert not area.intersects_event(outside)
    assert not area.intersects_event(no_geometry)


@pytest.mark.asyncio
async def test_no_feature_groups_means_no_filter():
    assert await load_area(source_with_square(), []) is None


@pytest.mark.asyncio
async def test_missing_or_empty_feature_group_is_a_configuration_error():
    site = source_with_square()
    with pytest.raises(IntegrationConfigurationError, match="Feature group gone"):
        await load_area(site, ["gone"])
    site.feature_groups["empty"] = {"id": "empty", "features": []}
    with pytest.raises(IntegrationConfigurationError, match="contain no shapes"):
        await load_area(site, ["empty"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_geo.py -v`
Expected: `ModuleNotFoundError: No module named 'app.er2er.geo'`.

- [ ] **Step 3: Implement `app/er2er/geo.py`**

```python
"""Area filters built from the source site's spatial feature groups (spec §7.1, §8)."""
from typing import List, Optional, Sequence

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from app.services.errors import IntegrationConfigurationError, IntegrationError
from .clients import halts_site


class Area:
    def __init__(self, shapes: List[BaseGeometry]):
        self._shapes = shapes

    def contains_point(self, lon: float, lat: float) -> bool:
        point = Point(lon, lat)
        return any(s.contains(point) for s in self._shapes)

    def intersects_event(self, event: dict) -> bool:
        geometry = (event.get("geojson") or {}).get("geometry")
        if not geometry:
            return False
        event_shape = shape(geometry)
        return any(s.intersects(event_shape) for s in self._shapes)


def shapes_from_feature_group(feature_group: dict) -> List[BaseGeometry]:
    shapes = []
    for spatial_feature in feature_group.get("features") or []:
        for geojson_feature in spatial_feature.get("features") or []:
            if geojson_feature.get("geometry"):
                shapes.append(shape(geojson_feature["geometry"]))
    return shapes


async def load_area(site, feature_group_ids: Sequence[str]) -> Optional[Area]:
    if not feature_group_ids:
        return None
    shapes = []
    for feature_group_id in feature_group_ids:
        try:
            feature_group = await site.get_feature_group(feature_group_id)
        except IntegrationError as e:
            if halts_site(e):
                raise
            raise IntegrationConfigurationError(
                f"Feature group {feature_group_id} could not be loaded from the source site; "
                "check it still exists and update the area filter."
            ) from e
        shapes.extend(shapes_from_feature_group(feature_group))
    if not shapes:
        raise IntegrationConfigurationError("The area filter's feature groups contain no shapes.")
    return Area(shapes)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_geo.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/er2er/geo.py app/er2er/tests/test_geo.py
git commit -m "feat: add feature-group area filters"
```

---

### Task 6: Destination categories and event types

**Files:**
- Create: `app/er2er/event_types.py`
- Test: `app/er2er/tests/test_event_types.py`

**Interfaces:**
- Consumes: `EventSettings` (Task 1), `FakeSite` (Task 2), `halts_site` (Task 2)
- Produces:
  - constants `ER2ER_SRC_FIELDS`, `ER2ER_HIDDEN_SECTION_ID`
  - pure helpers:
    - `add_er2er_fields_to_v2_schema(schema: dict) -> dict`
    - `dest_event_type_value(value: str, s: EventSettings) -> str`
    - `dest_category_value(value: str, s: EventSettings) -> str`
    - `category_value(event_type: dict) -> str`
    - `clean_event_type(src_type: dict, s: EventSettings) -> dict`
    - `has_provenance_fields(event_type: dict) -> bool`
  - `EventTypeSync(source, dest, s)`:
    - `async ensure(source_value: str) -> Optional[dict]`: the destination type dict (with `value`, `display`), or None
    - `failures: Dict[str, str]`: source value → admin-facing reason
    - raises only errors for which `halts_site` is true

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_event_types.py`:

```python
import json
from dataclasses import replace

import pytest

from app.actions.configurations import SyncEventsConfig
from app.er2er.event_types import ER2ER_HIDDEN_SECTION_ID, EventTypeSync, clean_event_type, has_provenance_fields
from app.er2er.modes import event_settings
from app.er2er.tests.fakes import FakeSite
from app.services.errors import IntegrationBadResponseError, IntegrationConnectionError

SETTINGS = event_settings("https://sitea.pamdas.org", SyncEventsConfig())


def src_type(value="wildlife_sighting"):
    return {
        "value": value,
        "display": "Wildlife Sighting",
        "category": {"value": "monitoring", "display": "Monitoring"},
        "is_active": True,
        "geometry_type": "Point",
        "schema": {"json": {"properties": {"species": {"type": "string", "enumNames": ["lion"]}}},
                   "ui": {"fields": {}, "sections": {}, "order": []}},
    }


def sites():
    source = FakeSite("https://sitea.pamdas.org", role="source")
    source.event_types["wildlife_sighting"] = src_type()
    source.categories["monitoring"] = {"value": "monitoring", "display": "Monitoring"}
    return source, FakeSite()


def test_clean_event_type_prefixes_and_hides_provenance():
    cleaned = clean_event_type(src_type(), SETTINGS)
    assert cleaned["value"] == "sitea_wildlife_sighting"
    assert cleaned["display"] == "Sitea-Wildlife Sighting"
    assert cleaned["category"] == "sitea_monitoring"
    schema = json.loads(cleaned["schema"])
    assert {"er2er_src_id", "er2er_src_system", "er2er_src_serial_number", "er2er_src_service_root"} <= set(
        schema["json"]["properties"])
    assert schema["ui"]["sections"][ER2ER_HIDDEN_SECTION_ID]["isActive"] is False
    assert schema["json"]["properties"]["species"]["enumNames"] == {"lion": "lion"}
    assert has_provenance_fields(cleaned)


def test_clean_event_type_without_prefixes():
    s = replace(SETTINGS, prepend_event_types=False, prepend_categories=False)
    cleaned = clean_event_type(src_type("Wildlife_Sighting"), s)
    assert (cleaned["value"], cleaned["display"], cleaned["category"]) == (
        "wildlife_sighting", "Wildlife Sighting", "monitoring")


@pytest.mark.asyncio
async def test_ensure_creates_category_and_type_once():
    source, dest = sites()
    sync = EventTypeSync(source, dest, SETTINGS)
    first = await sync.ensure("wildlife_sighting")
    second = await sync.ensure("wildlife_sighting")
    assert first["value"] == "sitea_wildlife_sighting" and second is first
    assert dest.categories["sitea_monitoring"]["display"] == "Sitea-Monitoring"
    assert len(dest.calls_to("post_event_type_v2")) == 1


@pytest.mark.asyncio
async def test_existing_type_is_patched_when_updates_are_allowed():
    source, dest = sites()
    dest.event_types["sitea_wildlife_sighting"] = {"value": "sitea_wildlife_sighting", "display": "old"}
    await EventTypeSync(source, dest, SETTINGS).ensure("wildlife_sighting")
    assert dest.calls_to("patch_event_type_v2") and not dest.calls_to("post_event_type_v2")


@pytest.mark.asyncio
async def test_without_schema_updates_a_type_lacking_provenance_fails_with_reason():
    source, dest = sites()
    s = replace(SETTINGS, update_schema=False)
    sync = EventTypeSync(source, dest, s)
    assert await sync.ensure("wildlife_sighting") is None
    assert "updating destination event types is turned off" in sync.failures["wildlife_sighting"]
    assert not dest.calls_to("post_event_type_v2")


@pytest.mark.asyncio
async def test_unknown_source_type_and_rejected_type_are_failures_not_exceptions():
    source, dest = sites()
    dest.failures["post_event_type_v2"] = IntegrationBadResponseError("Destination site x: rejected", status_code=400)
    sync = EventTypeSync(source, dest, SETTINGS)
    assert await sync.ensure("wildlife_sighting") is None
    assert await sync.ensure("no_such_type") is None
    assert "rejected" in sync.failures["wildlife_sighting"]
    assert "not found at the source" in sync.failures["no_such_type"]


@pytest.mark.asyncio
async def test_halting_errors_propagate():
    source, dest = sites()
    dest.failures["get_event_type_v2"] = IntegrationConnectionError("Destination site x: down")
    with pytest.raises(IntegrationConnectionError):
        await EventTypeSync(source, dest, SETTINGS).ensure("wildlife_sighting")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_event_types.py -v`
Expected: `ModuleNotFoundError: No module named 'app.er2er.event_types'`.

- [ ] **Step 3: Implement `app/er2er/event_types.py`**

The schema helpers are ported from `cdip-integrations/er2er/er2er_syncher.py:51-217` (commit `23ba4ea`), keeping only the v2 path.

```python
"""Destination event categories and v2 event types (spec §7.2)."""
import copy
import json
import logging
from typing import Dict, Optional, Set

from app.services.errors import IntegrationConfigurationError, IntegrationError
from .clients import halts_site
from .modes import EventSettings

logger = logging.getLogger(__name__)

SYNC_FIELDS_EVENT_TYPES = [
    "value", "display", "ordernum", "icon_id", "is_active", "schema", "properties",
    "category", "is_collection", "geometry_type",
]

# Provenance fields written into every copy's event_details and declared in the
# destination type's schema. serial_number is ER's integer serial, declared
# "number" as the V2 meta-schema requires.
ER2ER_SRC_FIELDS = {
    "er2er_src_id": {"type": "string", "title": "ID of Source Event"},
    "er2er_src_system": {"type": "string", "title": "Name of Source ER System"},
    "er2er_src_serial_number": {"type": "number", "title": "Serial Number of Source Event"},
    "er2er_src_service_root": {"type": "string", "title": "Service Root of Source Event"},
}

# A V2 field is hidden from the report form by living in an isActive=False section.
ER2ER_HIDDEN_SECTION_ID = "section-er2er-hidden"


def _normalize_enum_names(properties: Dict) -> None:
    """ER expects enumNames as a value->label mapping; legacy schemas use a list."""
    for prop in properties.values():
        if isinstance(prop, dict) and isinstance(prop.get("enumNames"), list):
            prop["enumNames"] = {v: v for v in prop["enumNames"]}


def add_er2er_fields_to_v2_schema(schema: Dict) -> Dict:
    """Declare the provenance fields in a V2 schema, hidden. Idempotent."""
    json_schema = schema.setdefault("json", {})
    properties = json_schema.setdefault("properties", {})
    ui = schema.setdefault("ui", {})
    ui_fields = ui.setdefault("fields", {})
    ui_sections = ui.setdefault("sections", {})
    ui_order = ui.setdefault("order", [])

    section = ui_sections.setdefault(ER2ER_HIDDEN_SECTION_ID, {
        "columns": 1, "isActive": False, "label": "", "leftColumn": [], "rightColumn": [], "conditions": [],
    })
    section["isActive"] = False
    if ER2ER_HIDDEN_SECTION_ID not in ui_order:
        ui_order.append(ER2ER_HIDDEN_SECTION_ID)
    in_section = {entry.get("name") for entry in section.get("leftColumn", [])}

    for key, field in ER2ER_SRC_FIELDS.items():
        properties[key] = {"type": field["type"], "title": field["title"], "description": "", "deprecated": False}
        if field["type"] == "number":
            ui_fields[key] = {"type": "NUMERIC", "placeholder": "", "parent": ER2ER_HIDDEN_SECTION_ID}
        else:
            ui_fields[key] = {"type": "TEXT", "inputType": "SHORT_TEXT", "placeholder": "",
                              "parent": ER2ER_HIDDEN_SECTION_ID}
        if key not in in_section:
            section["leftColumn"].append({"type": "field", "name": key})
            in_section.add(key)
    _normalize_enum_names(properties)
    return schema


def dest_event_type_value(value: str, s: EventSettings) -> str:
    return (f"{s.source.value}_{value}" if s.prepend_event_types else value).lower()


def dest_category_value(value: str, s: EventSettings) -> str:
    return f"{s.source.value}_{value}" if s.prepend_categories else value


def category_value(event_type: dict) -> str:
    category = event_type.get("category")
    return category.get("value") if isinstance(category, dict) else category


def clean_event_type(src_type: dict, s: EventSettings) -> dict:
    cleaned = {k: copy.deepcopy(v) for k, v in src_type.items() if k in SYNC_FIELDS_EVENT_TYPES}
    schema = src_type.get("schema") or {}
    schema = json.loads(schema) if isinstance(schema, str) else copy.deepcopy(schema)
    cleaned["schema"] = json.dumps(add_er2er_fields_to_v2_schema(schema), indent=4)
    display = src_type.get("display") or src_type["value"]
    cleaned["display"] = f"{s.source.name}-{display}" if s.prepend_event_types else display
    cleaned["value"] = dest_event_type_value(src_type["value"], s)
    cleaned["category"] = dest_category_value(category_value(src_type), s)
    return cleaned


def has_provenance_fields(event_type: dict) -> bool:
    schema = event_type.get("schema") or {}
    if isinstance(schema, str):
        schema = json.loads(schema)
    properties = (schema.get("json") or {}).get("properties") or {}
    return all(key in properties for key in ER2ER_SRC_FIELDS)


class EventTypeSync:
    """Makes sure each source event type exists at one destination, once per run."""

    def __init__(self, source, dest, s: EventSettings):
        self._source, self._dest, self._s = source, dest, s
        self._ready: Dict[str, dict] = {}
        self.failures: Dict[str, str] = {}
        self._source_types: Optional[Dict[str, dict]] = None
        self._source_categories: Optional[Dict[str, dict]] = None
        self._dest_categories: Optional[Dict[str, dict]] = None
        self._categories_done: Set[str] = set()

    async def ensure(self, source_value: str) -> Optional[dict]:
        if source_value in self._ready:
            return self._ready[source_value]
        if source_value in self.failures:
            return None
        try:
            dest_type = await self._ensure(source_value)
        except IntegrationError as e:
            if halts_site(e) and not isinstance(e, IntegrationConfigurationError):
                raise
            self.failures[source_value] = f"Event type {source_value} couldn't be set up at the destination: {e.message}"
            return None
        self._ready[source_value] = dest_type
        return dest_type

    async def _ensure(self, source_value: str) -> dict:
        if self._source_types is None:
            self._source_types = {t["value"]: t for t in await self._source.list_event_types_v2()}
        src = self._source_types.get(source_value)
        if src is None:
            raise IntegrationConfigurationError("not found at the source (is it a v1 event type?)")
        wanted = clean_event_type(src, self._s)
        existing = await self._dest.get_event_type_v2(wanted["value"])
        if not self._s.update_schema:
            if existing is None or not has_provenance_fields(existing):
                raise IntegrationConfigurationError(
                    "missing at the destination or lacks the er2er provenance fields, "
                    "and updating destination event types is turned off"
                )
            return existing
        await self._ensure_category(category_value(src))
        if existing is None:
            await self._dest.post_event_type_v2(wanted)
        else:
            await self._dest.patch_event_type_v2(wanted)
        return wanted

    async def _ensure_category(self, src_value: str):
        dest_value = dest_category_value(src_value, self._s)
        if dest_value in self._categories_done:
            return
        if self._source_categories is None:
            self._source_categories = {c["value"]: c for c in await self._source.get_event_categories()}
        if self._dest_categories is None:
            self._dest_categories = {c["value"]: c for c in await self._dest.get_event_categories()}
        display = (self._source_categories.get(src_value) or {}).get("display") or src_value
        wanted = {"value": dest_value,
                  "display": f"{self._s.source.name}-{display}" if self._s.prepend_categories else display}
        existing = self._dest_categories.get(dest_value)
        if existing is None:
            await self._dest.post_event_category(wanted)
        elif existing.get("display") != wanted["display"]:
            await self._dest.patch_event_category({**existing, **wanted})
        self._categories_done.add(dest_value)
```

`IntegrationConfigurationError` counts as halting in `halts_site`, but within `ensure` it means "this type can't be set up". So `ensure` turns it into a per-type failure rather than re-raising, while connection, auth and rate-limit errors still propagate.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_event_types.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/er2er/event_types.py app/er2er/tests/test_event_types.py
git commit -m "feat: sync destination categories and v2 event types"
```

---

### Task 7: Event cleaning, filters and echo guard

**Files:**
- Create: `app/er2er/events.py`
- Test: `app/er2er/tests/test_events.py`

**Interfaces:**
- Consumes: `EventSettings` (Task 1), `dest_event_type_value` (Task 6), `Area` (Task 5), `EventRecord` (Task 4), `parse_dt`, `site_host` (Task 1)
- Produces:
  - `SYNC_FIELDS_EVENTS`
  - `clean_event(event, s, dest_type: dict, source_service_root: str) -> dict`
  - `is_echo(event, dest_url: str) -> bool`
  - `wanted(event, s, area: Optional[Area]) -> bool`
  - `is_newer(event, record: Optional[EventRecord]) -> bool`
  - `incident_ids(event) -> List[str]`
  - `event_label(event) -> str`

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_events.py`:

```python
from dataclasses import replace

import pytest

from app.actions.configurations import SyncEventsConfig
from app.er2er.events import clean_event, event_label, incident_ids, is_echo, is_newer, wanted
from app.er2er.modes import event_settings
from app.er2er.state import EventRecord
from app.er2er.tests.fakes import make_event
from app.er2er.timeutil import parse_dt

SETTINGS = event_settings("https://sitea.pamdas.org", SyncEventsConfig())
DEST_TYPE = {"value": "sitea_wildlife_sighting", "display": "Sitea-Wildlife Sighting"}
ROOT = "https://sitea.pamdas.org/api/v1.0"


def test_clean_event_keeps_uuid_prefixes_type_and_adds_provenance():
    event = make_event("e1", "2026-10-01T00:00:00Z", title="Lion", priority=200, revision=7)
    cleaned = clean_event(event, SETTINGS, DEST_TYPE, ROOT)
    assert cleaned["id"] == "e1"
    assert cleaned["event_type"] == "sitea_wildlife_sighting"
    assert cleaned["title"] == "Sitea-Lion"
    assert cleaned["priority"] == 200
    assert "revision" not in cleaned and "notes" not in cleaned
    assert cleaned["event_details"] == {
        "er2er_src_id": "e1",
        "er2er_src_serial_number": event["serial_number"],
        "er2er_src_system": "sitea",
        "er2er_src_service_root": ROOT,
    }


def test_untitled_event_gets_prefixed_type_display_unless_already_prefixed():
    event = make_event("e1", "2026-10-01T00:00:00Z", title=None)
    assert "title" not in clean_event(event, SETTINGS, DEST_TYPE, ROOT)
    plain_type = {"value": "x", "display": "Wildlife Sighting"}
    assert clean_event(event, SETTINGS, plain_type, ROOT)["title"] == "Sitea-Wildlife Sighting"


def test_titles_unprefixed_when_turned_off():
    s = replace(SETTINGS, prepend_titles=False)
    event = make_event("e1", "2026-10-01T00:00:00Z", title="Lion")
    assert clean_event(event, s, DEST_TYPE, ROOT)["title"] == "Lion"


def test_echo_guard_skips_events_that_came_from_the_destination():
    echoed = make_event("e1", "2026-10-01T00:00:00Z",
                        event_details={"er2er_src_service_root": "https://SiteB.pamdas.org/api/v1.0"})
    assert is_echo(echoed, "https://siteb.pamdas.org")
    assert not is_echo(make_event("e2", "2026-10-01T00:00:00Z"), "https://siteb.pamdas.org")


def test_wanted_applies_event_type_filter():
    s = replace(SETTINGS, event_types=frozenset({"fire"}))
    assert wanted(make_event("e1", "2026-10-01T00:00:00Z", event_type="fire"), s, None)
    assert not wanted(make_event("e2", "2026-10-01T00:00:00Z"), s, None)
    assert wanted(make_event("e3", "2026-10-01T00:00:00Z"), SETTINGS, None)


def test_is_newer_compares_with_recorded_update():
    event = make_event("e1", "2026-10-01T00:00:00Z")
    assert is_newer(event, None)
    assert not is_newer(event, EventRecord(dest_event_id="e1", updated_at=parse_dt("2026-10-01T00:00:00Z")))
    assert is_newer(event, EventRecord(dest_event_id="e1", updated_at=parse_dt("2026-09-30T00:00:00Z")))


def test_incident_ids_and_label():
    event = make_event("e1", "2026-10-01T00:00:00Z", serial_number=42, is_contained_in=[
        {"type": "contains", "related_event": {"id": "inc1"}},
        {"type": "other", "related_event": {"id": "x"}},
    ])
    assert incident_ids(event) == ["inc1"]
    assert event_label(event) == "#42 (e1)"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_events.py -v`
Expected: `ModuleNotFoundError: No module named 'app.er2er.events'`.

- [ ] **Step 3: Implement `app/er2er/events.py`**

`clean_event`'s title rules are ported from `cdip-integrations/er2er/er2er_syncher.py:282-318`.

```python
"""Event copies: cleaning, provenance, filters and the echo guard (spec §7.3)."""
import copy
from typing import List, Optional

from .event_types import dest_event_type_value
from .geo import Area
from .modes import EventSettings
from .naming import site_host
from .state import EventRecord
from .timeutil import parse_dt

SYNC_FIELDS_EVENTS = [
    "id", "location", "time", "event_type", "priority", "title", "state",
    "event_details", "is_collection", "geometry", "geojson",
]


def clean_event(event: dict, s: EventSettings, dest_type: dict, source_service_root: str) -> dict:
    cleaned = {k: copy.deepcopy(v) for k, v in event.items() if k in SYNC_FIELDS_EVENTS}
    cleaned["event_type"] = dest_event_type_value(event["event_type"], s)

    if s.prepend_titles:
        display = dest_type.get("display") or dest_type.get("value") or ""
        title = None
        if event.get("title"):
            title = f"{s.source.name}-{event['title']}"
            if title == display:
                title = None
        elif not display.startswith(f"{s.source.name}-"):
            title = f"{s.source.name}-{display}"
        cleaned["title"] = title
    if not cleaned.get("title"):
        cleaned.pop("title", None)

    details = cleaned.get("event_details") or {}
    details.update({
        "er2er_src_id": event["id"],
        "er2er_src_serial_number": event.get("serial_number"),
        "er2er_src_system": s.source.value,
        "er2er_src_service_root": source_service_root,
    })
    cleaned["event_details"] = details
    return cleaned


def is_echo(event: dict, dest_url: str) -> bool:
    """The source event is itself a copy of a destination event: don't send it back."""
    root = ((event.get("event_details") or {}).get("er2er_src_service_root") or "").lower()
    return bool(root) and site_host(dest_url) in root


def wanted(event: dict, s: EventSettings, area: Optional[Area]) -> bool:
    if s.event_types and event.get("event_type") not in s.event_types:
        return False
    return area is None or area.intersects_event(event)


def is_newer(event: dict, record: Optional[EventRecord]) -> bool:
    return record is None or parse_dt(event["updated_at"]) > record.updated_at


def incident_ids(event: dict) -> List[str]:
    return [str(c["related_event"]["id"]) for c in event.get("is_contained_in") or []
            if c.get("type") == "contains" and c.get("related_event")]


def event_label(event: dict) -> str:
    return f"#{event.get('serial_number')} ({event.get('id')})"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_events.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/er2er/events.py app/er2er/tests/test_events.py
git commit -m "feat: clean events with provenance, filters and echo guard"
```

---

### Task 8: Notes, files and incidents

**Files:**
- Create: `app/er2er/notes_files.py`
- Test: `app/er2er/tests/test_notes_files.py`

**Interfaces:**
- Consumes: `EventRecord`, `NoteRecord` (Task 4), `incident_ids` (Task 7), `halts_site` (Task 2), `DestinationReport` (Task 4), `FakeSite` (Task 2)
- Produces:
  - `MAX_FILE_BYTES`, `EPOCH`
  - `note_digest(text) -> str`
  - `NotesPlan(add, update, delete)`; `plan_notes(src_notes, recorded) -> NotesPlan`
  - `FilesPlan(add, delete)`; `plan_files(src_files, recorded) -> FilesPlan`
  - `adopt_record(src_event, dest_event) -> EventRecord`
  - async appliers:
    - `apply_notes(dest, dest_event_id, plan, record)`
    - `apply_files(source, dest, dest_event_id, plan, record, report)`
    - `apply_incidents(dest, dest_event_id, wanted_ids, record, report)`

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_notes_files.py`:

```python
import pytest
from gundi_core.events import LogLevel

from app.er2er.notes_files import (
    MAX_FILE_BYTES, adopt_record, apply_files, apply_incidents, apply_notes, note_digest, plan_files, plan_notes,
)
from app.er2er.reporting import DestinationReport
from app.er2er.state import EventRecord, NoteRecord
from app.er2er.tests.fakes import FakeSite, make_event
from app.er2er.timeutil import parse_dt
from app.services.errors import IntegrationBadResponseError

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
    assert report.errors["Attachment over 20 MB not copied"].level == LogLevel.WARNING
    await apply_files(source, dest, "e1", plan_files(src_files, record.files), record, report)
    assert len(source.calls_to("download_file")) == 2  # nothing re-downloaded


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_notes_files.py -v`
Expected: `ModuleNotFoundError: No module named 'app.er2er.notes_files'`.

- [ ] **Step 3: Implement `app/er2er/notes_files.py`**

```python
"""Incremental notes, attachments and incident links for one event copy (spec §7.4–7.5)."""
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from gundi_core.events import LogLevel

from app.services.errors import IntegrationError
from .clients import halts_site
from .events import incident_ids
from .reporting import DestinationReport
from .state import EventRecord, NoteRecord

MAX_FILE_BYTES = 20 * 1024 * 1024
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def note_digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class NotesPlan:
    add: List[dict] = field(default_factory=list)
    update: List[Tuple[dict, str]] = field(default_factory=list)  # (source note, dest note id)
    delete: List[Tuple[str, str]] = field(default_factory=list)   # (source note id, dest note id)


@dataclass
class FilesPlan:
    add: List[dict] = field(default_factory=list)
    delete: List[Tuple[str, str]] = field(default_factory=list)   # (source file id, dest file id or "")


def plan_notes(src_notes, recorded: Dict[str, NoteRecord]) -> NotesPlan:
    plan = NotesPlan()
    current = {str(n["id"]): n for n in src_notes or [] if n.get("text")}
    for src_id, note in current.items():
        rec = recorded.get(src_id)
        if rec is None:
            plan.add.append(note)
        elif rec.digest != note_digest(note["text"]):
            plan.update.append((note, rec.dest_id))
    plan.delete = [(src_id, rec.dest_id) for src_id, rec in recorded.items() if src_id not in current]
    return plan


def plan_files(src_files, recorded: Dict[str, str]) -> FilesPlan:
    current = {str(f["id"]): f for f in src_files or []}
    return FilesPlan(
        add=[f for src_id, f in current.items() if src_id not in recorded],
        delete=[(src_id, dest_id) for src_id, dest_id in recorded.items() if src_id not in current],
    )


def adopt_record(src_event: dict, dest_event: dict) -> EventRecord:
    """Build a record for a copy that exists but was never recorded (made by v1),
    matching notes by text and files by filename so nothing is copied twice."""
    record = EventRecord(dest_event_id=str(dest_event["id"]), updated_at=EPOCH)
    unused_notes = list(dest_event.get("notes") or [])
    for note in src_event.get("notes") or []:
        match = next((d for d in unused_notes if d.get("text") == note.get("text")), None)
        if match:
            unused_notes.remove(match)
            record.notes[str(note["id"])] = NoteRecord(dest_id=str(match["id"]), digest=note_digest(note.get("text")))
    unused_files = list(dest_event.get("files") or [])
    for file in src_event.get("files") or []:
        match = next((d for d in unused_files if d.get("filename") == file.get("filename")), None)
        if match:
            unused_files.remove(match)
            record.files[str(file["id"])] = str(match["id"])
    record.incidents = incident_ids(dest_event)
    return record


async def _ignore_404(awaitable):
    try:
        return await awaitable
    except IntegrationError as e:
        if e.status_code != 404:
            raise


async def apply_notes(dest, dest_event_id: str, plan: NotesPlan, record: EventRecord):
    for note in plan.add:
        created = await dest.post_note(dest_event_id, note["text"])
        record.notes[str(note["id"])] = NoteRecord(dest_id=str(created["id"]), digest=note_digest(note["text"]))
    for note, dest_note_id in plan.update:
        try:
            await dest.patch_note(dest_event_id, dest_note_id, note["text"])
            new_id = dest_note_id
        except IntegrationError as e:
            if halts_site(e) or e.status_code not in (404, 405):
                raise
            # Note edits unsupported or note gone: replace it.
            await _ignore_404(dest.delete_note(dest_event_id, dest_note_id))
            new_id = str((await dest.post_note(dest_event_id, note["text"]))["id"])
        record.notes[str(note["id"])] = NoteRecord(dest_id=new_id, digest=note_digest(note["text"]))
    for src_id, dest_note_id in plan.delete:
        await _ignore_404(dest.delete_note(dest_event_id, dest_note_id))
        record.notes.pop(src_id, None)


async def apply_files(source, dest, dest_event_id: str, plan: FilesPlan, record: EventRecord,
                      report: DestinationReport):
    for file in plan.add:
        content = await source.download_file(file["url"])
        if len(content) > MAX_FILE_BYTES:
            report.error("Attachment over 20 MB not copied", item=file.get("filename"), level=LogLevel.WARNING)
            record.files[str(file["id"])] = ""  # recorded so it isn't downloaded again
            continue
        created = await dest.post_file(dest_event_id, file.get("filename") or "attachment", content,
                                       file.get("comment") or "")
        record.files[str(file["id"])] = str(created.get("id", ""))
    for src_id, dest_file_id in plan.delete:
        if dest_file_id:
            await _ignore_404(dest.delete_file(dest_event_id, dest_file_id))
        record.files.pop(src_id, None)


async def apply_incidents(dest, dest_event_id: str, wanted_ids: List[str], record: EventRecord,
                          report: DestinationReport):
    for incident_id in [i for i in wanted_ids if i not in record.incidents]:
        try:
            await dest.add_to_incident(dest_event_id, incident_id)
            record.incidents.append(incident_id)
        except IntegrationError as e:
            if halts_site(e):
                raise
            report.error("Couldn't link a copied event to its incident (the incident may not be shared)",
                         item=incident_id, level=LogLevel.WARNING)
    for incident_id in [i for i in record.incidents if i not in wanted_ids]:
        try:
            await dest.remove_from_incident(dest_event_id, incident_id)
        except IntegrationError as e:
            if halts_site(e):
                raise
            if e.status_code != 404:
                report.error("Couldn't unlink a copied event from an incident", item=incident_id,
                             level=LogLevel.WARNING)
                continue
        record.incidents.remove(incident_id)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_notes_files.py -v`
Expected: all PASS.

- [ ] **Step 5: Verify note PATCH against a dev ER (one-off)**

Using a dev ER token, create an event with a note. Then:

```bash
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"edited"}' "https://<dev-site>/api/v1.0/activity/event/<event_id>/note/<note_id>" -o /dev/null -w '%{http_code}\n'
```

Expected: `200`. If it returns `404` or `405`, the fallback path above is what production will use, and that's fine. Record the result in the PR description.

- [ ] **Step 6: Commit**

```bash
git add app/er2er/notes_files.py app/er2er/tests/test_notes_files.py
git commit -m "feat: copy notes, files and incident links incrementally"
```

---

### Task 9: Syncing events to one destination

**Files:**
- Create: `app/er2er/sync_events.py` (part 1: per-destination sync)
- Test: `app/er2er/tests/test_sync_events.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8
- Produces:
  - constants `BACKFILL`, `RECONCILE_WINDOW`
  - `EventsRun(integration_id, source, settings, area, state, deadline: float)` with `out_of_time() -> bool`
  - `marker_key(destination_id) -> str`
  - `async sync_destination(run, destination, dest, report) -> None`: saves the marker on every exit path; re-raises halting errors

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_sync_events.py`:

```python
import time

import pytest

from app.actions.configurations import AuthenticateConfig, SyncEventsConfig
from app.er2er.destinations import Destination
from app.er2er.modes import event_settings
from app.er2er.reporting import DestinationReport
from app.er2er.state import EVENTS_ACTION, EventRecord, ShareState
from app.er2er.sync_events import EventsRun, marker_key, sync_destination
from app.er2er.tests.fakes import FakeSite, InMemoryStateManager, make_event
from app.er2er.timeutil import parse_dt
from app.services.errors import IntegrationBadResponseError, IntegrationConnectionError

SOURCE_URL = "https://sitea.pamdas.org"
DESTINATION = Destination("d1", "Site B", "https://siteb.pamdas.org", AuthenticateConfig(token="t"))


def wildlife_type():
    return {"value": "wildlife_sighting", "display": "Wildlife Sighting", "category": "monitoring",
            "schema": {"json": {"properties": {}}, "ui": {}}}


def setup(*events, config=None, deadline=None):
    source = FakeSite(SOURCE_URL, role="source")
    source.event_types["wildlife_sighting"] = wildlife_type()
    for event in events:
        source.events[event["id"]] = event
    dest = FakeSite()
    state = ShareState("i1", manager=InMemoryStateManager())
    settings = event_settings(SOURCE_URL, config or SyncEventsConfig.parse_obj({"start_from": "2026-09-01T00:00:00Z"}))
    run = EventsRun("i1", source, settings, None, state, deadline or time.monotonic() + 60)
    return source, dest, state, run


async def sync(run, dest):
    report = DestinationReport("Site B", "Events")
    await sync_destination(run, DESTINATION, dest, report)
    return report


@pytest.mark.asyncio
async def test_creates_copies_with_same_uuid_and_advances_marker():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    report = await sync(run, dest)
    assert set(dest.events) == {"e1", "e2"}
    assert dest.events["e1"]["event_details"]["er2er_src_id"] == "e1"
    assert report.created == 2
    marker = await state.get_marker(EVENTS_ACTION, marker_key("d1"))
    assert marker.since == parse_dt("2026-10-02T00:00:00Z")


@pytest.mark.asyncio
async def test_rerun_does_not_rewrite_unchanged_events():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"))
    await sync(run, dest)
    calls_before = len(dest.calls)
    report = await sync(run, dest)  # re-reads e1 at the marker boundary (update_date >= marker)
    assert report.skipped == 1 and report.created == report.updated == 0
    assert not [c for c in dest.calls[calls_before:] if c[0] in ("post_event", "patch_event")]


@pytest.mark.asyncio
async def test_source_edit_patches_copy_and_adds_only_new_notes():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", notes=[{"id": "n1", "text": "a"}]))
    await sync(run, dest)
    source.events["e1"].update(state="resolved", updated_at="2026-10-03T00:00:00Z",
                               notes=[{"id": "n1", "text": "a"}, {"id": "n2", "text": "b"}])
    report = await sync(run, dest)
    assert report.updated == 1
    assert dest.events["e1"]["state"] == "resolved"
    assert [n["text"] for n in dest.events["e1"]["notes"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_adopts_v1_copy_without_duplicating_notes_or_files():
    event = make_event("e1", "2026-10-01T00:00:00Z", notes=[{"id": "n1", "text": "hello"}],
                       files=[{"id": "f1", "url": "u1", "filename": "a.jpg"}])
    source, dest, state, run = setup(event)
    source.files["u1"] = b"img"
    dest.events["e1"] = {"id": "e1", "event_type": "sitea_wildlife_sighting", "updated_at": "2026-10-01T00:05:00Z",
                         "notes": [{"id": "dn1", "text": "hello"}], "files": [{"id": "df1", "filename": "a.jpg"}],
                         "is_contained_in": [], "event_details": {}}
    report = await sync(run, dest)
    assert report.updated == 1 and report.created == 0
    assert len(dest.events["e1"]["notes"]) == 1 and len(dest.events["e1"]["files"]) == 1
    assert not source.calls_to("download_file")


@pytest.mark.asyncio
async def test_recreates_copy_deleted_at_destination():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", notes=[{"id": "n1", "text": "a"}]))
    await sync(run, dest)
    del dest.events["e1"]
    source.events["e1"]["updated_at"] = "2026-10-02T00:00:00Z"
    report = await sync(run, dest)
    assert report.created == 1 and report.failed == 0
    assert [n["text"] for n in dest.events["e1"]["notes"]] == ["a"]


@pytest.mark.asyncio
async def test_rejected_event_is_reported_and_skipped():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    dest.failures["post_event"] = lambda event: (
        IntegrationBadResponseError("Destination site siteb.pamdas.org: bad location", status_code=400)
        if event["id"] == "e1" else None)
    report = await sync(run, dest)
    assert report.failed == 1 and report.created == 1
    assert list(report.errors)[0].startswith("EarthRanger rejected an event")
    assert (await state.get_marker(EVENTS_ACTION, marker_key("d1"))).since == parse_dt("2026-10-02T00:00:00Z")


@pytest.mark.asyncio
async def test_event_type_failure_only_affects_that_type():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", event_type="unknown_v1_type"),
                                     make_event("e2", "2026-10-02T00:00:00Z"))
    report = await sync(run, dest)
    assert report.failed == 1 and report.created == 1
    assert any("unknown_v1_type" in title for title in report.errors)


@pytest.mark.asyncio
async def test_filtered_and_echoed_events_are_skipped():
    config = SyncEventsConfig.parse_obj({"start_from": "2026-09-01T00:00:00Z", "event_types": ["wildlife_sighting"]})
    echoed = make_event("e3", "2026-10-03T00:00:00Z",
                        event_details={"er2er_src_service_root": "https://siteb.pamdas.org/api/v1.0"})
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z", event_type="fire"),
                                     make_event("e2", "2026-10-02T00:00:00Z"), echoed, config=config)
    report = await sync(run, dest)
    assert set(dest.events) == {"e2"} and report.skipped == 2


@pytest.mark.asyncio
async def test_halting_destination_error_keeps_progress_up_to_last_success():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    dest.failures["post_event"] = lambda event: (
        IntegrationConnectionError("Destination site siteb.pamdas.org: down") if event["id"] == "e2" else None)
    with pytest.raises(IntegrationConnectionError):
        await sync(run, dest)
    assert (await state.get_marker(EVENTS_ACTION, marker_key("d1"))).since == parse_dt("2026-10-01T00:00:00Z")
    assert await state.get_event_record("d1", "e1") is not None


@pytest.mark.asyncio
async def test_out_of_time_stops_and_flags_continuation():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), deadline=time.monotonic() - 1)
    report = await sync(run, dest)
    assert report.timed_out and not dest.events
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_sync_events.py -v`
Expected: `ModuleNotFoundError: No module named 'app.er2er.sync_events'`.

- [ ] **Step 3: Implement part 1 of `app/er2er/sync_events.py`**

```python
"""sync_events: mirror source events onto each destination (spec §7)."""
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from app.services.errors import IntegrationError
from .clients import halts_site
from .destinations import Destination
from .event_types import EventTypeSync
from .events import clean_event, event_label, incident_ids, is_echo, is_newer, wanted
from .geo import Area
from .modes import EventSettings
from .notes_files import adopt_record, apply_files, apply_incidents, apply_notes, plan_files, plan_notes
from .reporting import DestinationReport
from .state import EVENTS_ACTION, EventRecord, Marker, ShareState
from .timeutil import parse_dt, utcnow

BACKFILL = timedelta(days=10)
RECONCILE_WINDOW = timedelta(days=30)


@dataclass
class EventsRun:
    integration_id: str
    source: object  # ERSite
    settings: EventSettings
    area: Optional[Area]
    state: ShareState
    deadline: float  # time.monotonic() value

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline


def marker_key(destination_id: str) -> str:
    return f"{destination_id}:marker"


async def sync_destination(run: EventsRun, destination: Destination, dest, report: DestinationReport) -> None:
    now = utcnow()
    marker = await run.state.get_marker(EVENTS_ACTION, marker_key(destination.id))
    since = marker.since if marker else (run.settings.start_from or now - BACKFILL)
    types = EventTypeSync(run.source, dest, run.settings)
    try:
        async for page in run.source.iter_events_updated_since(since):
            for event in page:
                if run.out_of_time():
                    report.timed_out = True
                    return
                await _sync_event(run, destination, dest, types, event, report)
                since = max(since, parse_dt(event["updated_at"]))
    finally:
        # Events arrive oldest first, so `since` is the last fully handled update.
        if marker is None or since != marker.since:
            await run.state.set_marker(EVENTS_ACTION, marker_key(destination.id),
                                       Marker(since=since, advanced_at=now))
        report.marker = since


async def _sync_event(run, destination, dest, types, event, report):
    if not wanted(event, run.settings, run.area) or is_echo(event, dest.url):
        report.skipped += 1
        return
    record = await run.state.get_event_record(destination.id, event["id"])
    if not is_newer(event, record):
        report.skipped += 1
        return
    dest_type = await types.ensure(event["event_type"])
    if dest_type is None:
        report.failed += 1
        report.error(types.failures[event["event_type"]], item=event_label(event))
        return
    copy = clean_event(event, run.settings, dest_type, run.source.service_root)
    try:
        record = await _write_event(dest, event, copy, record, report)
        await apply_notes(dest, record.dest_event_id, plan_notes(event.get("notes"), record.notes), record)
        await apply_files(run.source, dest, record.dest_event_id, plan_files(event.get("files"), record.files),
                          record, report)
        await apply_incidents(dest, record.dest_event_id, incident_ids(event), record, report)
    except IntegrationError as e:
        if halts_site(e):
            raise
        report.failed += 1
        report.error(f"EarthRanger rejected an event: {e.message}", item=event_label(event))
        return
    record.updated_at = parse_dt(event["updated_at"])
    await run.state.set_event_record(destination.id, event["id"], record)


async def _write_event(dest, event: dict, copy: dict, record: Optional[EventRecord],
                       report: DestinationReport) -> EventRecord:
    body = {k: v for k, v in copy.items() if k != "id"}
    if record is not None:
        try:
            await dest.patch_event(record.dest_event_id, body)
            report.updated += 1
            return record
        except IntegrationError as e:
            if e.status_code != 404:
                raise
            # Deleted at the destination: recreate it below.
    existing = await dest.get_event(event["id"], with_notes_and_files=True)
    if existing is not None:
        await dest.patch_event(existing["id"], body)
        report.updated += 1
        return adopt_record(event, existing)
    created = await dest.post_event(copy)
    report.created += 1
    return EventRecord(dest_event_id=str(created["id"]), updated_at=parse_dt(event["updated_at"]))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_sync_events.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/er2er/sync_events.py app/er2er/tests/test_sync_events.py
git commit -m "feat: mirror events onto one destination with resumable markers"
```

---

### Task 10: Event reconciliation and the `sync_events` run

**Files:**
- Modify: `app/er2er/sync_events.py` (append part 2)
- Modify: `app/actions/handlers.py` (currently empty)
- Test: `app/er2er/tests/test_sync_events_run.py`

**Interfaces:**
- Consumes: `sync_destination`, `EventsRun`, `marker_key` (Task 9); `resolve_destinations` (Task 3); `open_site`, `auth_config_of` (Task 2); `publish_report`, `is_stalled` (Task 4); `trigger_action`, `log_action_activity`
- Produces:
  - `async reconcile_destination(run, destination, dest, report) -> bool` (True when finished)
  - `async run_sync_events(integration, config: SyncEventsConfig) -> dict`
  - handler `action_sync_events(integration, action_config: SyncEventsConfig)`, scheduled `*/5 * * * *`

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_sync_events_run.py`:

```python
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.actions.configurations import AuthenticateConfig, SyncEventsConfig
from app.er2er import sync_events
from app.er2er.destinations import Destination, SkippedDestination
from app.er2er.modes import event_settings
from app.er2er.reporting import DestinationReport
from app.er2er.state import EVENTS_ACTION, ShareState
from app.er2er.sync_events import EventsRun, marker_key, reconcile_destination, run_sync_events
from app.er2er.tests.fakes import FakeSite, InMemoryStateManager, make_event
from app.er2er.timeutil import parse_dt, utcnow
from app.services.errors import IntegrationAuthError, IntegrationConnectionError

SOURCE_URL = "https://sitea.pamdas.org"
INTEGRATION = SimpleNamespace(
    id="i1", base_url=SOURCE_URL,
    configurations=[SimpleNamespace(action=SimpleNamespace(value="auth"), data={"token": "t"})],
)
CONFIG = SyncEventsConfig.parse_obj({"start_from": "2026-09-01T00:00:00Z"})


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
        dest.events[copy_id] = make_event(copy_id, utcnow().isoformat(),
                                          event_details={"er2er_src_id": src_id, "er2er_src_system": system})
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_sync_events_run.py -v`
Expected: `ImportError: cannot import name 'reconcile_destination'`.

- [ ] **Step 3: Append part 2 to `app/er2er/sync_events.py`**

Add these imports to the top of the file:

```python
from gundi_core.events import LogLevel

from app import settings
from app.services.action_scheduler import trigger_action
from app.services.activity_logger import log_action_activity
from .clients import auth_config_of, open_site
from .destinations import resolve_destinations
from .geo import load_area
from .modes import event_settings
from .reporting import is_stalled, publish_report
```

Append:

```python
RECONCILE_EVERY_SECONDS = 24 * 60 * 60
BUDGET_FRACTION = 0.8
LEASE_MARGIN_SECONDS = 30


def reconcile_key(destination_id: str) -> str:
    return f"{destination_id}:reconcile"


async def reconcile_destination(run: EventsRun, destination: Destination, dest, report: DestinationReport) -> bool:
    """Delete recent destination copies whose source event is gone (v1 delete_unmatched_events)."""
    since = utcnow() - RECONCILE_WINDOW
    async for page in dest.iter_events_updated_since(since, with_notes_and_files=False):
        for copy in page:
            if run.out_of_time():
                report.timed_out = True
                return False
            details = copy.get("event_details") or {}
            src_id = details.get("er2er_src_id")
            if not src_id or details.get("er2er_src_system") != run.settings.source.value:
                continue
            if await run.source.get_event(src_id) is not None:
                continue
            try:
                await dest.delete_event(copy["id"])
            except IntegrationError as e:
                if halts_site(e):
                    raise
                if e.status_code != 404:
                    report.error(f"Couldn't remove a copy whose source event was deleted: {e.message}",
                                 item=copy["id"])
                    continue
            await run.state.delete_event_record(destination.id, src_id)
            report.deleted += 1
    return True


async def _maybe_reconcile(run: EventsRun, destination: Destination, dest, report: DestinationReport):
    if not await run.state.once_per(EVENTS_ACTION, reconcile_key(destination.id), RECONCILE_EVERY_SECONDS):
        return
    finished = False
    try:
        finished = await reconcile_destination(run, destination, dest, report)
    finally:
        if not finished:
            await run.state.forget_once(EVENTS_ACTION, reconcile_key(destination.id))


async def run_sync_events(integration, config) -> dict:
    integration_id = str(integration.id)
    state = ShareState(integration_id)
    if not await state.try_lease(EVENTS_ACTION, settings.MAX_ACTION_EXECUTION_TIME + LEASE_MARGIN_SECONDS):
        return {"status": "skipped", "reason": "Another sync_events run is still in progress."}
    try:
        result, needs_more_time = await _run(integration, config, state)
    finally:
        await state.release_lease(EVENTS_ACTION)
    if needs_more_time:
        await trigger_action(integration_id, EVENTS_ACTION)
    return result


async def _run(integration, config, state: ShareState):
    integration_id = str(integration.id)
    deadline = time.monotonic() + settings.MAX_ACTION_EXECUTION_TIME * BUDGET_FRACTION
    share = event_settings(integration.base_url, config)
    destinations, skipped = await resolve_destinations(integration_id, integration.base_url)
    for item in skipped:
        await log_action_activity(integration_id=integration_id, action_id=EVENTS_ACTION,
                                  title=f"Destination {item.name} skipped: {item.reason}", level=LogLevel.WARNING)
    if not destinations:
        await log_action_activity(integration_id=integration_id, action_id=EVENTS_ACTION,
                                  title="No EarthRanger destinations to share events with.", level=LogLevel.WARNING)
        return {"destinations": []}, False

    reports = []
    async with open_site(integration.base_url, auth_config_of(integration.configurations), role="source") as source:
        run = EventsRun(integration_id, source, share, await load_area(source, share.area_feature_groups),
                        state, deadline)
        for destination in destinations:
            report = DestinationReport(destination=destination.name, kind="Events")
            try:
                async with open_site(destination.url, destination.auth, role="destination") as dest:
                    await sync_destination(run, destination, dest, report)
                    if share.delete_unmatched and not report.timed_out:
                        await _maybe_reconcile(run, destination, dest, report)
            except IntegrationError as e:
                if getattr(e, "site_role", None) == "source":
                    raise
                report.error(e.message)
            await publish_report(integration_id, EVENTS_ACTION, report)
            marker = await state.get_marker(EVENTS_ACTION, marker_key(destination.id))
            if report.errors and is_stalled(marker, utcnow()):
                await log_action_activity(
                    integration_id=integration_id, action_id=EVENTS_ACTION, level=LogLevel.WARNING,
                    title=f"Events → {destination.name}: no progress since "
                          f"{marker.advanced_at:%Y-%m-%d %H:%M} UTC. Check the errors above.",
                )
            reports.append(report)
    return {"destinations": [r.as_dict() for r in reports]}, any(r.timed_out for r in reports)
```

- [ ] **Step 4: Write `app/actions/handlers.py`**

```python
"""Action handlers. Keep these thin: the work lives in app/er2er.

app/actions/__init__.py imports this module at import time, so app.er2er is
imported inside each handler; a top-level import would be circular.
"""
from gundi_core.schemas.v2 import Integration

from app.services.action_scheduler import crontab_schedule
from app.services.activity_logger import activity_logger
from .configurations import SyncEventsConfig


@crontab_schedule("*/5 * * * *")
@activity_logger()
async def action_sync_events(integration: Integration, action_config: SyncEventsConfig):
    from app.er2er.sync_events import run_sync_events
    return await run_sync_events(integration, action_config)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_sync_events_run.py app/er2er/tests/test_sync_events.py -v`
Expected: all PASS.

Then run: `python -c "from app.actions import action_handlers; print(sorted(action_handlers))"`
Expected: `['sync_events']`

- [ ] **Step 6: Commit**

```bash
git add app/er2er/sync_events.py app/er2er/tests/test_sync_events_run.py app/actions/handlers.py
git commit -m "feat: add scheduled sync_events with daily delete reconciliation"
```

---

### Task 11: Observation filters, payloads and markers

**Files:**
- Create: `app/er2er/observations.py`
- Test: `app/er2er/tests/test_observations.py`

**Interfaces:**
- Consumes: `ObservationSettings` (Task 1), `Marker` (Task 4), `parse_dt` (Task 1), `make_observation` (Task 2 fakes)
- Produces:
  - constants `MAX_BATCH = 200`, `OVERLAP = timedelta(hours=12)`
  - `has_valid_location(obs) -> bool`
  - `Assignment(source_id, lower, upper)`; `parse_assignments(raw) -> Dict[str, List[Assignment]]`; `in_assignment(obs, assignments) -> bool`
  - `is_new(obs, marker) -> bool`
  - `read_start(markers: List[Optional[Marker]], fallback: datetime) -> datetime`
  - `advance(marker, batch, now) -> Marker`
  - `build_payload(subject, obs, s, source_additional: Dict[str, dict]) -> dict`
  - `add_lineage(payload, fingerprint) -> dict`
  - `batches(items, size=MAX_BATCH) -> Iterator[list]`

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_observations.py`:

```python
from dataclasses import replace

from app.actions.configurations import SyncObservationsConfig
from app.er2er.modes import observation_settings
from app.er2er.observations import (
    OVERLAP, add_lineage, advance, batches, build_payload, has_valid_location, in_assignment, is_new,
    parse_assignments, read_start,
)
from app.er2er.state import Marker
from app.er2er.tests.fakes import make_observation
from app.er2er.timeutil import parse_dt

T0 = parse_dt("2026-10-01T00:00:00Z")
SETTINGS = observation_settings("https://sitea.pamdas.org", SyncObservationsConfig())
SUBJECT = {"id": "s1", "name": "Lion 1", "subject_type": "wildlife", "subject_subtype": "lion",
           "additional": {"sex": "f"}}


def test_invalid_locations_are_rejected():
    assert has_valid_location(make_observation("src", "2026-10-01T00:00:00Z", lat=1, lon=1))
    for lat, lon in ((0, 0), (91, 1), (1, 181)):
        assert not has_valid_location(make_observation("src", "2026-10-01T00:00:00Z", lat=lat, lon=lon))
    assert not has_valid_location({"location": None})


def test_assignment_ranges_including_open_ended():
    assignments = parse_assignments([
        {"subject": "s1", "source": "src1",
         "assigned_range": {"lower": "2026-09-01T00:00:00Z", "upper": "2026-09-30T00:00:00Z"}},
        {"subject": {"id": "s1"}, "source": {"id": "src2"}, "assigned_range": {"lower": "2026-09-15T00:00:00Z",
                                                                             "upper": None}},
    ])["s1"]
    assert in_assignment(make_observation("src1", "2026-09-10T00:00:00Z"), assignments)
    assert not in_assignment(make_observation("src1", "2026-10-10T00:00:00Z"), assignments)
    assert in_assignment(make_observation("src2", "2026-12-01T00:00:00Z"), assignments)
    assert not in_assignment(make_observation("src3", "2026-09-10T00:00:00Z"), assignments)


def test_late_arriving_observation_is_new():
    marker = Marker(since=T0, advanced_at=T0, created_since=T0)
    late = make_observation("src", "2026-09-30T20:00:00Z", created_at="2026-10-01T01:00:00Z")
    old = make_observation("src", "2026-09-30T20:00:00Z", created_at="2026-09-30T21:00:00Z")
    assert is_new(late, marker)
    assert not is_new(old, marker)
    assert is_new(make_observation("src", "2026-10-01T00:00:01Z"), marker)
    assert is_new(old, None)


def test_read_start_covers_the_oldest_marker_minus_overlap():
    m1 = Marker(since=T0, advanced_at=T0)
    m2 = Marker(since=parse_dt("2026-10-05T00:00:00Z"), advanced_at=T0)
    assert read_start([m1, m2], fallback=parse_dt("2026-10-10T00:00:00Z")) == T0 - OVERLAP
    assert read_start([m2, None], fallback=T0) == T0
    assert read_start([], fallback=T0) == T0


def test_advance_moves_both_high_water_marks():
    batch = [make_observation("src", "2026-10-02T00:00:00Z", created_at="2026-10-02T00:01:00Z"),
             make_observation("src", "2026-10-01T12:00:00Z")]
    marker = advance(Marker(since=T0, advanced_at=T0), batch, now=parse_dt("2026-10-03T00:00:00Z"))
    assert marker.since == parse_dt("2026-10-02T00:00:00Z")
    assert marker.created_since == parse_dt("2026-10-02T00:01:00Z")
    assert marker.advanced_at == parse_dt("2026-10-03T00:00:00Z")


def test_payload_matches_v1_shape_and_lineage_is_appended():
    obs = make_observation("src1", "2026-10-01T00:00:00Z", details={"speed": 3})
    payload = add_lineage(build_payload(SUBJECT, obs, SETTINGS, {}), "site:sitea.pamdas.org#user:svc#subject:s1#rts:x")
    assert payload == {
        "subject_id": "s1", "manufacturer_id": "src1", "subject_name": "Lion 1", "subject_subtype": "lion",
        "subject_type": "wildlife", "recorded_at": "2026-10-01T00:00:00Z", "location": {"lat": 1.0, "lon": 1.0},
        "additional": {"speed": 3, "sharing_lineage": ["site:sitea.pamdas.org#user:svc#subject:s1#rts:x"]},
        "subject_groups": ["Sitea"],
    }
    assert obs["observation_details"] == {"speed": 3}  # source data not mutated


def test_payload_options():
    obs = make_observation("src1", "2026-10-01T00:00:00Z", details=None)
    s = replace(SETTINGS, destination_subject_group=None, subject_additional=True)
    payload = build_payload(SUBJECT, obs, s, {"src1": {"collar": "x"}})
    assert "subject_groups" not in payload and payload["additional"] == {}
    assert payload["subject_additional"] == {"sex": "f"} and payload["source_additional"] == {"collar": "x"}


def test_batches():
    assert [len(b) for b in batches(list(range(450)))] == [200, 200, 50]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_observations.py -v`
Expected: `ModuleNotFoundError: No module named 'app.er2er.observations'`.

- [ ] **Step 3: Implement `app/er2er/observations.py`**

```python
"""Subject-track filters, payloads and markers (spec §8). Pure functions."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterator, List, Optional

from .modes import ObservationSettings
from .state import Marker
from .timeutil import parse_dt

MAX_BATCH = 200
# Re-read this far behind each marker so points that reach the source late
# (recorded earlier, uploaded later) are still found; is_new() drops repeats.
OVERLAP = timedelta(hours=12)


def has_valid_location(obs: dict) -> bool:
    location = obs.get("location") or {}
    lat, lon = location.get("latitude"), location.get("longitude")
    if lat is None or lon is None:
        return False
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return False
    return not (lat == 0 and lon == 0)


@dataclass(frozen=True)
class Assignment:
    source_id: str
    lower: datetime
    upper: Optional[datetime]


def _id(value) -> str:
    return str(value.get("id")) if isinstance(value, dict) else str(value)


def parse_assignments(raw: List[dict]) -> Dict[str, List[Assignment]]:
    out: Dict[str, List[Assignment]] = {}
    for item in raw or []:
        rng = item.get("assigned_range") or {}
        if not rng.get("lower"):
            continue
        out.setdefault(_id(item["subject"]), []).append(Assignment(
            source_id=_id(item["source"]),
            lower=parse_dt(rng["lower"]),
            upper=parse_dt(rng["upper"]) if rng.get("upper") else None,
        ))
    return out


def in_assignment(obs: dict, assignments: List[Assignment]) -> bool:
    recorded, source = parse_dt(obs["recorded_at"]), str(obs.get("source"))
    return any(a.source_id == source and a.lower <= recorded and (a.upper is None or recorded <= a.upper)
               for a in assignments)


def is_new(obs: dict, marker: Optional[Marker]) -> bool:
    if marker is None or parse_dt(obs["recorded_at"]) > marker.since:
        return True
    created = obs.get("created_at")
    if created is None or marker.created_since is None:
        return True  # can't tell; ER's sharing endpoint drops duplicates
    return parse_dt(created) > marker.created_since


def read_start(markers: List[Optional[Marker]], fallback: datetime) -> datetime:
    starts = [m.since - OVERLAP for m in markers if m is not None]
    if not markers or any(m is None for m in markers):
        starts.append(fallback)
    return min(starts)


def advance(marker: Optional[Marker], batch: List[dict], now: datetime) -> Marker:
    recorded = max(parse_dt(o["recorded_at"]) for o in batch)
    created = [parse_dt(o["created_at"]) for o in batch if o.get("created_at")]
    if marker is not None:
        recorded = max(recorded, marker.since)
        if marker.created_since:
            created.append(marker.created_since)
    created_since = max(created) if created else None
    moved = marker is None or recorded != marker.since or created_since != marker.created_since
    return Marker(since=recorded, created_since=created_since,
                  advanced_at=now if moved else marker.advanced_at)


def build_payload(subject: dict, obs: dict, s: ObservationSettings, source_additional: Dict[str, dict]) -> dict:
    payload = {
        "subject_id": str(subject["id"]),
        "manufacturer_id": obs.get("source"),  # the source device's UUID, as v1
        "subject_name": subject.get("name"),
        "subject_subtype": subject.get("subject_subtype"),
        "subject_type": subject.get("subject_type"),
        "recorded_at": obs["recorded_at"],
        "location": {"lat": obs["location"]["latitude"], "lon": obs["location"]["longitude"]},
        "additional": dict(obs.get("observation_details") or {}),
    }
    if s.destination_subject_group:
        payload["subject_groups"] = [s.destination_subject_group]
    if s.subject_additional:
        payload["subject_additional"] = subject.get("additional") or {}
        payload["source_additional"] = source_additional.get(str(obs.get("source")), {})
    return payload


def add_lineage(payload: dict, fingerprint: str) -> dict:
    lineage = list(payload["additional"].get("sharing_lineage") or [])
    payload["additional"]["sharing_lineage"] = lineage + [fingerprint]
    return payload


def batches(items: list, size: int = MAX_BATCH) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_observations.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/er2er/observations.py app/er2er/tests/test_observations.py
git commit -m "feat: add observation filters, payloads and markers"
```

---

### Task 12: The `sync_observations` run

**Files:**
- Create: `app/er2er/sync_observations.py`
- Modify: `app/actions/handlers.py` (add the handler)
- Test: `app/er2er/tests/test_sync_observations.py`

**Interfaces:**
- Consumes: Task 11 functions; `ShareState`, `Marker`, `OBSERVATIONS_ACTION` (Task 4); `open_site`, `auth_config_of`, `halts_site` (Task 2); `resolve_destinations` (Task 3); `load_area` (Task 5); `publish_report`, `is_stalled` (Task 4); `site_host` (Task 1)
- Produces:
  - `async run_sync_observations(integration, config: SyncObservationsConfig) -> dict`
  - `subject_marker_key(destination_id, subject_id) -> str`
  - `async select_subjects(source, group_ids) -> List[dict]`
  - handler `action_sync_observations`, scheduled `*/5 * * * *`

- [ ] **Step 1: Write the failing tests**

`app/er2er/tests/test_sync_observations.py`:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.actions.configurations import AuthenticateConfig, SyncObservationsConfig
from app.er2er import sync_observations
from app.er2er.destinations import Destination
from app.er2er.state import OBSERVATIONS_ACTION, ShareState
from app.er2er.sync_observations import run_sync_observations, select_subjects, subject_marker_key
from app.er2er.tests.fakes import FakeSite, InMemoryStateManager, make_observation
from app.er2er.timeutil import parse_dt
from app.services.errors import IntegrationBadResponseError, IntegrationConnectionError

SOURCE_URL = "https://sitea.pamdas.org"
INTEGRATION = SimpleNamespace(
    id="i1", base_url=SOURCE_URL,
    configurations=[SimpleNamespace(action=SimpleNamespace(value="auth"), data={"token": "t"})],
)
CONFIG = SyncObservationsConfig.parse_obj({"start_from": "2026-09-30T00:00:00Z"})
LION = {"id": "s1", "name": "Lion 1", "subject_type": "wildlife", "subject_subtype": "lion"}
ZEBRA = {"id": "s2", "name": "Zebra 1", "subject_type": "wildlife", "subject_subtype": "zebra"}


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
    return SimpleNamespace(source=source, sites=sites, manager=manager, published=published)


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
    state = ShareState("i1", manager=world.manager)
    marker = await state.get_marker(OBSERVATIONS_ACTION, subject_marker_key("d1", "s1"))
    assert marker.since == parse_dt("2026-10-01T01:00:00Z")


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
    world.sites["https://site1.pamdas.org"].failures["post_sharing_status"] = lambda key, payloads: (
        IntegrationBadResponseError("Destination site site1.pamdas.org: bad", status_code=400)
        if payloads[0]["subject_id"] == "s1" else None)
    await run_sync_observations(INTEGRATION, CONFIG)
    report = world.published[0]
    assert report.failed == 2 and report.created == 1
    state = ShareState("i1", manager=world.manager)
    assert (await state.get_marker(OBSERVATIONS_ACTION, subject_marker_key("d1", "s1"))).since == parse_dt(
        "2026-10-01T01:00:00Z")


@pytest.mark.asyncio
async def test_tracks_area_filter(world):
    world.source.feature_groups["fg1"] = {"id": "fg1", "features": [{"features": [{"type": "Feature", "geometry": {
        "type": "Polygon", "coordinates": [[[5, 5], [5, 6], [6, 6], [6, 5], [5, 5]]]}}]}]}
    world.source.observations["s2"].append(make_observation("src-s2", "2026-10-01T03:00:00Z", lat=5.5, lon=5.5))
    config = SyncObservationsConfig.parse_obj({"start_from": "2026-09-30T00:00:00Z", "area_feature_groups": ["fg1"]})
    await run_sync_observations(INTEGRATION, config)
    assert points(world.sites["https://site1.pamdas.org"]) == [("s2", "2026-10-01T03:00:00Z")]


@pytest.mark.asyncio
async def test_out_of_time_triggers_continuation(world, monkeypatch):
    monkeypatch.setattr(sync_observations, "BUDGET_FRACTION", -1)
    await run_sync_observations(INTEGRATION, CONFIG)
    sync_observations.trigger_action.assert_awaited_once_with("i1", OBSERVATIONS_ACTION)
    assert all(r.timed_out for r in world.published)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/er2er/tests/test_sync_observations.py -v`
Expected: `ModuleNotFoundError: No module named 'app.er2er.sync_observations'`.

- [ ] **Step 3: Implement `app/er2er/sync_observations.py`**

```python
"""sync_observations: share subject tracks with each destination (spec §8)."""
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Dict, FrozenSet, List

from gundi_core.events import LogLevel

from app import settings
from app.services.action_scheduler import trigger_action
from app.services.activity_logger import log_action_activity
from app.services.errors import IntegrationError
from .clients import auth_config_of, halts_site, open_site
from .destinations import resolve_destinations
from .geo import load_area
from .modes import observation_settings
from .naming import site_host
from .observations import (
    add_lineage, advance, batches, build_payload, has_valid_location, in_assignment, is_new, parse_assignments,
    read_start,
)
from .reporting import DestinationReport, is_stalled, publish_report
from .state import OBSERVATIONS_ACTION, ShareState
from .timeutil import parse_dt, utcnow

BACKFILL = timedelta(hours=12)
SUBJECT_ID_CHUNK = 25
BUDGET_FRACTION = 0.8
LEASE_MARGIN_SECONDS = 30


def subject_marker_key(destination_id: str, subject_id: str) -> str:
    return f"{destination_id}:subject:{subject_id}"


async def select_subjects(source, group_ids: FrozenSet[str]) -> List[dict]:
    subjects: Dict[str, dict] = {}
    for group in await source.list_subject_groups():
        if group_ids and str(group.get("id")) not in group_ids:
            continue
        for subject in group.get("subjects") or []:
            subjects.setdefault(str(subject["id"]), subject)
    return list(subjects.values())


async def _load_assignments(source, subject_ids: List[str]):
    raw = []
    for i in range(0, len(subject_ids), SUBJECT_ID_CHUNK):
        raw.extend(await source.get_source_assignments(subject_ids[i:i + SUBJECT_ID_CHUNK]))
    return parse_assignments(raw)


async def run_sync_observations(integration, config) -> dict:
    integration_id = str(integration.id)
    state = ShareState(integration_id)
    if not await state.try_lease(OBSERVATIONS_ACTION, settings.MAX_ACTION_EXECUTION_TIME + LEASE_MARGIN_SECONDS):
        return {"status": "skipped", "reason": "Another sync_observations run is still in progress."}
    try:
        result, needs_more_time = await _run(integration, config, state)
    finally:
        await state.release_lease(OBSERVATIONS_ACTION)
    if needs_more_time:
        await trigger_action(integration_id, OBSERVATIONS_ACTION)
    return result


async def _run(integration, config, state: ShareState):
    integration_id = str(integration.id)
    deadline = time.monotonic() + settings.MAX_ACTION_EXECUTION_TIME * BUDGET_FRACTION
    share = observation_settings(integration.base_url, config)
    destinations, skipped = await resolve_destinations(integration_id, integration.base_url)
    for item in skipped:
        await log_action_activity(integration_id=integration_id, action_id=OBSERVATIONS_ACTION,
                                  title=f"Destination {item.name} skipped: {item.reason}", level=LogLevel.WARNING)
    if not destinations:
        await log_action_activity(integration_id=integration_id, action_id=OBSERVATIONS_ACTION,
                                  title="No EarthRanger destinations to share subject tracks with.",
                                  level=LogLevel.WARNING)
        return {"destinations": []}, False

    reports = {d.id: DestinationReport(destination=d.name, kind="Tracks") for d in destinations}
    async with AsyncExitStack() as stack:
        source = await stack.enter_async_context(
            open_site(integration.base_url, auth_config_of(integration.configurations), role="source"))
        sites = {}
        for d in destinations:
            try:
                sites[d.id] = await stack.enter_async_context(open_site(d.url, d.auth, role="destination"))
            except IntegrationError as e:
                reports[d.id].error(e.message)
        area = await load_area(source, share.area_feature_groups)
        subjects = await select_subjects(source, share.subject_groups)
        assignments = await _load_assignments(source, [str(s["id"]) for s in subjects])
        src_user = share.src_user or (await source.get_me()).get("username", "")
        now = utcnow()
        prefix = f"site:{site_host(source.url)}#user:{src_user}"
        for subject in subjects:
            if not sites:
                break
            if time.monotonic() > deadline:
                for d_id in sites:
                    reports[d_id].timed_out = True
                break
            await _share_subject(source, sites, reports, state, share, area, assignments, subject, prefix, now)

    for d in destinations:
        await publish_report(integration_id, OBSERVATIONS_ACTION, reports[d.id])
    return ({"destinations": [r.as_dict() for r in reports.values()]},
            any(r.timed_out for r in reports.values()))


async def _share_subject(source, sites, reports, state, share, area, assignments, subject, prefix, now):
    subject_id = str(subject["id"])
    label = subject.get("name") or subject_id
    markers = {d_id: await state.get_marker(OBSERVATIONS_ACTION, subject_marker_key(d_id, subject_id))
               for d_id in sites}
    start = read_start(list(markers.values()), share.start_from or now - BACKFILL)
    try:
        observations = await source.get_observations(subject_id, start)
        source_additional = {}
        if share.subject_additional:
            source_additional = {str(s.get("id")): s.get("additional") or {}
                                 for s in await source.get_subject_sources(subject_id)}
    except IntegrationError as e:
        if halts_site(e):
            raise
        for d_id in sites:
            reports[d_id].error(f"Couldn't read a subject's track from the source: {e.message}", item=label)
        return

    candidates = sorted(
        (o for o in observations
         if has_valid_location(o)
         and in_assignment(o, assignments.get(subject_id, []))
         and (area is None or area.contains_point(o["location"]["longitude"], o["location"]["latitude"]))),
        key=lambda o: parse_dt(o["recorded_at"]),
    )
    fingerprint = f"{prefix}#subject:{subject_id}#rts:{now:%Y%m%d%H%M%S}"
    for d_id, dest in list(sites.items()):
        marker = markers[d_id]
        for batch in batches([o for o in candidates if is_new(o, marker)]):
            payloads = [add_lineage(build_payload(subject, o, share, source_additional), fingerprint) for o in batch]
            try:
                await dest.post_sharing_status(share.provider_key, payloads)
                reports[d_id].created += len(batch)
            except IntegrationError as e:
                if halts_site(e):
                    reports[d_id].error(e.message)
                    del sites[d_id]
                    break
                reports[d_id].failed += len(batch)
                reports[d_id].error(f"EarthRanger rejected track points: {e.message}", item=label)
            marker = advance(marker, batch, now)
            await state.set_marker(OBSERVATIONS_ACTION, subject_marker_key(d_id, subject_id), marker)
        if marker is not None:
            current = reports[d_id].marker
            reports[d_id].marker = marker.since if current is None else max(current, marker.since)
```

- [ ] **Step 4: Add the handler to `app/actions/handlers.py`**

Add `SyncObservationsConfig` to the configurations import, then append:

```python
@crontab_schedule("*/5 * * * *")
@activity_logger()
async def action_sync_observations(integration: Integration, action_config: SyncObservationsConfig):
    from app.er2er.sync_observations import run_sync_observations
    return await run_sync_observations(integration, action_config)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest app/er2er/tests/test_sync_observations.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/er2er/sync_observations.py app/er2er/tests/test_sync_observations.py app/actions/handlers.py
git commit -m "feat: add scheduled sync_observations with per-subject markers"
```

---

### Task 13: Auth and reference actions

**Files:**
- Modify: `app/actions/handlers.py`
- Test: `app/actions/tests/test_handlers.py`

**Interfaces:**
- Consumes: `open_site`, `auth_config_of` (Task 2); config and query models (Task 1); `ReferenceDataResponse`, `ReferenceOption` (`app/actions/core.py`)
- Produces handlers:
  - `action_auth(integration, action_config: AuthenticateConfig) -> dict` returning `{"valid_credentials": bool, "capabilities"?: {...}, "warning"?: str, "error"?: str}`
  - `action_list_event_types`, `action_list_subject_groups`, `action_list_feature_groups`, each returning `ReferenceDataResponse(...).dict()`

- [ ] **Step 1: Write the failing tests**

`app/actions/tests/test_handlers.py`:

```python
from types import SimpleNamespace

import pytest

from app.actions import action_handlers, handlers
from app.actions.configurations import (
    AuthenticateConfig, ListEventTypesQuery, ListFeatureGroupsQuery, ListSubjectGroupsQuery,
)
from app.er2er import clients
from app.er2er.tests.fakes import FakeSite
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


@pytest.mark.asyncio
async def test_auth_reports_capabilities(source):
    source.me = {"is_active": True, "permissions": {"event": ["view"], "subject": ["view"], "observation": ["view"]}}
    result = await handlers.action_auth(INTEGRATION, AuthenticateConfig(token="t"))
    assert result == {"valid_credentials": True, "capabilities": {"events": True, "subject_tracks": True}}


@pytest.mark.asyncio
async def test_auth_warns_about_missing_read_permissions(source):
    source.me = {"is_active": True, "permissions": {"event": ["view"]}}
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/actions/tests/test_handlers.py -v`
Expected: FAIL. `test_all_actions_are_registered` lists only the two sync actions, and `handlers.action_auth` doesn't exist.

- [ ] **Step 3: Add the handlers**

In `app/actions/handlers.py`, extend the imports:

```python
from .configurations import (
    AuthenticateConfig, ERAuthenticationType, ListEventTypesQuery, ListFeatureGroupsQuery, ListSubjectGroupsQuery,
    SyncEventsConfig, SyncObservationsConfig,
)
from .core import ReferenceDataResponse, ReferenceOption
```

Append:

```python
async def action_auth(integration: Integration, action_config: AuthenticateConfig):
    from app.er2er import clients
    from app.services.errors import IntegrationError

    if action_config.authentication_type == ERAuthenticationType.TOKEN and not action_config.token:
        return {"valid_credentials": False, "error": "Please provide a token."}
    if action_config.authentication_type == ERAuthenticationType.USERNAME_PASSWORD and not (
            action_config.username and action_config.password):
        return {"valid_credentials": False, "error": "Please provide both a username and a password."}
    try:
        async with clients.open_site(integration.base_url, action_config, role="source") as site:
            me = await site.get_me()
    except IntegrationError as e:
        return {"valid_credentials": False, "error": e.message}
    result = {"valid_credentials": bool(me.get("is_active", True))}
    permissions = me.get("permissions")
    if isinstance(permissions, dict):
        can_view = lambda model: "view" in (permissions.get(model) or [])  # noqa: E731
        capabilities = {"events": can_view("event"),
                        "subject_tracks": can_view("subject") and can_view("observation")}
        result["capabilities"] = capabilities
        missing = [name for name, ok in capabilities.items() if not ok]
        if missing:
            result["warning"] = (f"This token can't read: {', '.join(missing)}. "
                                 "Sharing those will fail until the token's permissions are updated.")
    return result


async def _source_site(integration):
    from app.er2er import clients
    return clients.open_site(integration.base_url, clients.auth_config_of(integration.configurations), role="source")


async def action_list_event_types(integration: Integration, action_config: ListEventTypesQuery):
    async with await _source_site(integration) as site:
        event_types = await site.list_event_types_v2()

    def group(event_type):
        category = event_type.get("category")
        return category.get("display") if isinstance(category, dict) else category

    options = [ReferenceOption(value=t["value"], label=t.get("display") or t["value"], group=group(t))
               for t in event_types]
    return ReferenceDataResponse(options=sorted(options, key=lambda o: (o.label or "").lower())).dict()


async def action_list_subject_groups(integration: Integration, action_config: ListSubjectGroupsQuery):
    async with await _source_site(integration) as site:
        groups = await site.list_subject_groups()
    options = [ReferenceOption(value=str(g["id"]), label=g.get("name")) for g in groups]
    return ReferenceDataResponse(options=sorted(options, key=lambda o: (o.label or "").lower())).dict()


async def action_list_feature_groups(integration: Integration, action_config: ListFeatureGroupsQuery):
    async with await _source_site(integration) as site:
        feature_groups = await site.list_feature_groups()
    options = [ReferenceOption(value=str(f["id"]), label=f.get("name")) for f in feature_groups]
    return ReferenceDataResponse(options=sorted(options, key=lambda o: (o.label or "").lower())).dict()
```

Monkeypatching `clients.open_site` in tests works because the handlers reach `open_site` through the `clients` module at call time.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest app/actions/tests/test_handlers.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: add auth and reference actions for the share form"
```

---

### Task 14: Registration, docs and full verification

**Files:**
- Modify: `README.md`, `.env.example`, `local/.env.local.example`

- [ ] **Step 1: Set the integration type identity**

In `.env.example` and `local/.env.local.example`, set:

```
INTEGRATION_TYPE_SLUG=earthranger_sharing
INTEGRATION_TYPE_NAME=EarthRanger Sharing
```

- [ ] **Step 2: Replace the template README**

Replace `README.md` with:

````markdown
# Gundi Integration: EarthRanger Sharing (ER2ER)

Mirrors events and subject tracks from one EarthRanger site (the connection's
source) to every EarthRanger destination of the connection. It is a one-way
live mirror: edits, notes, attachments, incident links and deletes follow the source.

Design: `docs/superpowers/specs/2026-10-15-er2er-v2-sharing-design.md`.

## Actions

| Action | Kind | Schedule |
|---|---|---|
| `auth` | Validates the source token and reports what it can read | — |
| `list_event_types`, `list_subject_groups`, `list_feature_groups` | Pick-lists for the form | — |
| `sync_events` | Copies new/changed events; once a day removes copies whose source was deleted | every 5 min |
| `sync_observations` | Shares subject track points through each destination's sharing endpoint | every 5 min |

Destinations are Gundi `earth_ranger` integrations. Their stored credentials are
read through Gundi at run time, and the runner writes to each destination directly.

## Development

```bash
pip-compile --output-file=requirements.txt requirements-base.in requirements-dev.in requirements.in
pip install -r requirements.txt
pytest
```

Code lives in `app/er2er/`; `app/actions/handlers.py` only wires actions to it.
Handlers import `app.er2er` inside function bodies (see the module docstring).
````

- [ ] **Step 3: Run the whole suite**

Run: `pytest`
Expected: all tests PASS, including the template's own `app/services/tests`.

- [ ] **Step 4: Check the published form schemas**

Run:

```bash
python -c "
from app.actions.configurations import SyncEventsConfig, SyncObservationsConfig, AuthenticateConfig
import json
for m in (AuthenticateConfig, SyncEventsConfig, SyncObservationsConfig):
    print(m.__name__, json.dumps(m.schema())[:200], json.dumps(m.ui_schema())[:200])
"
```

Expected: each prints without error, and the UI schemas contain `gundi:reference` entries and `ui:order`.

- [ ] **Step 5: Commit**

```bash
git add README.md .env.example local/.env.local.example
git commit -m "docs: describe the EarthRanger Sharing runner"
```

- [ ] **Step 6: Dev smoke test (manual, before any prod use)**

1. Deploy to dev and register the type: `python app/register.py --slug earthranger_sharing --name "EarthRanger Sharing"`.
2. In the dev portal, create a connection with a dev ER as source and another dev ER destination.
3. Save the form with defaults.
4. Wait one schedule tick.

Confirm:
- the activity log shows one "Events → …" and one "Tracks → …" summary per destination;
- a test event created at the source appears at the destination with the same UUID and a `Sitea-` prefixed type;
- editing its state and adding a note at the source shows up at the destination within two ticks, with exactly one copy of each note.

Record the results in the PR description.
