# ER2ER Sharing on Gundi v2 — Design

- **Date:** 2026-10-15
- **Ticket:** [GUNDI-5810](https://allenai.atlassian.net/browse/GUNDI-5810) (related: GUNDI-5799, ERCS-4130, ERCS-4795, ERCS-5754)
- **Repo:** `PADAS/gundi-integration-earthranger-sharing` (action runner); migration command in `PADAS/cdip`
- **Status:** Draft for review

## 1. Context

EarthRanger-to-EarthRanger (ER2ER) sharing copies data from one ER site to another. Today it is two Gundi v1 Kubernetes CronJobs that write straight to the destination ER:

| | Events | Subject observations |
|---|---|---|
| Repo | `cdip-integrations/er2er` | `gundi-integration-er2er` |
| Schedule | `*/20`, 30 indexed pods | `*/10`, 30 indexed pods |
| Config | v1 inbound `endpoint`/`token` (source) + free-form `state` JSON (destination, flags) | v1 inbound `endpoint`/`token` + `state` JSON; destinations from v1 outbound configs |

Problems:

- **Hard to configure.** Everything lives in untyped `state` JSON, editable only in the legacy portal.
- **Hard to observe.** No activity logs. Known silent-failure bugs:
  - events: `last_successful_run` advances past failed chunks;
  - subjects: posting failures are swallowed and the cursor advances anyway;
  - subjects: no high-water mark at all (the same ~13 h window is re-sent every run).
- **Two codebases**, neither with the v2 framework's retries, classified errors or portal status.
- **No patrol sharing.**

## 2. Goals

- **Users:** an ER admin can set up a share from the portal, mostly by picking from lists, and can tell at a glance whether a share is working or what to fix.
- **Platform:** one repo (this action runner) replaces both legacy jobs, with the same resilience as other v2 action runners.
- **Parity:** every v1 behaviour and bug fix keeps working, except where this spec deliberately drops it (§11).

### Non-goals

- **Two-way sync.** Sharing is a one-way live mirror: source changes (including event edits, notes, attachments, deletes) flow to destinations; nothing flows back.
- **Routing data through Gundi's routing and dispatchers.** See §4.
- **Per-destination settings.** Settings are per connection; partners who need different rules for different destinations create a second connection.
- **v1 event types (ER event schema v1).** Not supported.

## 3. Phases

| Phase | Scope | Spec |
|---|---|---|
| **1. v1 replacement** | Events (create, update, notes, attachments, incidents, deletes) and subject observations; simple and compatibility modes; activity logs; list-based form; migration of all v1 shares; retire legacy jobs | **This document (detailed)** |
| 2. Patrols | GUNDI-5799: patrol details, start/end time and location, track, duration and distance, events linked to their patrol | Own spec; §12 fixes the extension points |
| 3. Small additions | Conditional event sharing (ERCS-4130), geofence edge points (ERCS-4795), history limit (ERCS-5754) | Own spec; §12 |
| 4. Preview | Show what will be shared before saving; needs portal work and the designer's form | Own spec |

## 4. Key decisions

| # | Decision | Why |
|---|---|---|
| D1 | One-way live mirror | Matches v1 events behaviour; partners care about state changes. |
| D2 | **Gundi v2 setup, direct writes.** Source and destinations are configured the standard v2 way; at runtime the runner reads each destination's ER credentials from Gundi and writes to the destination ER itself. | The Gundi routing → ER delivery path has no patrol support, drops event fields such as priority on create, cannot create destination event types, and depends on routing for update IDs. Direct writes keep all work in one repo. The ER dispatcher already reads destination credentials the same way (`gundi-dispatcher-er/core/event_handlers.py:119`). |
| D3 | Settings are per connection | Matches v1; one-page form; one source read per run. |
| D4 | Simple mode by default; compatibility mode for migrated shares that need it | Ticket requires v1 parity; product wants a much simpler form. |
| D5 | Migrate all v1 shares with a cdip management command | Hundreds of shares. |
| D6 | Build in `gundi-integration-earthranger-sharing`, synced to the current template | Gets the current template library (timeouts, PubSub redelivery, ephemeral reference actions). |
| D7 | No v1 event-type support | v1 event types are not used going forward. |

## 5. Architecture

```
            Gundi portal (v2)
   connection = source ER  ──route──▶ destinations = Gundi EarthRanger integrations
            │ (config, schedules, activity logs)
            ▼
  gundi-integration-earthranger-sharing (action runner)
     reads source ER ──────────────▶ writes each destination ER directly
     (erclient, source token)         (erclient, destination token fetched via gundi-client)
     state: Redis IntegrationStateManager, keyed per destination
```

The integration type is `earthranger_sharing`. Destinations are existing Gundi EarthRanger destination integrations. The runner resolves the connection's destinations and their auth configs through `gundi-client` at the start of each run.

### 5.1 Actions

| Action | Kind | Schedule | Purpose |
|---|---|---|---|
| `auth` | auth | — | Validate the source token. Report which capabilities it has (events, subjects, patrols in phase 2) so a missing permission shows at save time. |
| `list_event_types` | reference | — | Form pick-list (v2 event types of the source). |
| `list_subject_groups` | reference | — | Form pick-list. |
| `list_feature_groups` | reference | — | Form pick-lists for both area filters. |
| `sync_events` | pull | every 5 min | §7 |
| `sync_observations` | pull | every 5 min | §8 |
| `reconcile_events` | pull | daily | Remove destination copies whose source event was deleted (§7.6). |
| `sync_patrols` | pull | — | Phase 2. |

Reference actions are stateless, so the portal can run them before the connection is saved.

### 5.2 Code layout

Each unit is small, pure where possible, and tested on its own.

- `app/actions/configurations.py` — auth, reference and pull configs (§6).
- `app/actions/handlers.py` — thin action handlers: load config, resolve destinations, loop, log activity.
- `app/er2er/clients.py` — builds source and destination `AsyncERClient`s; translates ER failures into `IntegrationAuthError`, `IntegrationConnectionError`, `IntegrationRateLimitError` and `IntegrationConfigurationError` (pattern from `gundi-integration-earthranger`'s `_translating_er_client`).
- `app/er2er/destinations.py` — resolves the connection's destinations and credentials; rejects same-site destinations.
- `app/er2er/naming.py` — source system code and name derivation; prefixing of categories, types and titles.
- `app/er2er/event_types.py` — destination category and type sync (v2 schema; hidden provenance section).
- `app/er2er/events.py` — event filtering, cleaning, provenance, loop guard, create/update decision.
- `app/er2er/notes_files.py` — note and file diffing against recorded IDs.
- `app/er2er/observations.py` — subject selection, assignment-range / location / area filters, batching, sharing payloads.
- `app/er2er/geo.py` — area-filter geometry (shapely).
- `app/er2er/state.py` — typed per-destination markers and per-event records over `IntegrationStateManager`.
- `app/er2er/modes.py` — effective settings from simple mode plus compatibility overrides.

## 6. Configuration

### 6.1 Auth (source)

- `host` (ER site URL)
- `token` (`SecretStr`)

### 6.2 Simple mode (all admins see)

| Setting | Input | Default |
|---|---|---|
| Share events | toggle | on |
| Event types to share | multi-select from `list_event_types` | all |
| Events area filter | feature-group picker from `list_feature_groups` | none |
| Share subject tracks | toggle | on |
| Subject groups to share | multi-select from `list_subject_groups` | all |
| Destination subject group name | text, pre-filled | source site name |
| Tracks area filter | feature-group picker | none |

Fixed or derived in simple mode:

- Source system code and name come from the source host's first label (e.g. `sitea` / `SiteA`).
- Categories, event types and titles are always prefixed.
- Destination event types are created or updated as needed.
- Destination copies are removed when the source event is deleted.
- Schedules, batch sizes, windows and look-back are internal.

### 6.3 Advanced (v1 compatibility), collapsed by default

- `prepend_system_to_categories`, `prepend_system_to_event_types`, `prepend_system_to_event_titles`
- `update_schema` (allow updates to destination event types)
- `delete_unmatched_events`
- `source_system_value`, `source_system_name` (overrides)
- `provider_key` (default `das_provider`)
- `subject_additional`
- `src_user`

A share is in **compatibility mode** if any of these differs from the simple-mode value. The mode is derived, not a separate switch, so a share drops back to simple mode as soon as its overrides match the defaults.

### 6.4 Internal (not in the form)

- Per-action `start_from` (optional datetime). Used only when no marker exists for a destination; the migration sets it (§10).
- Backfill window for first runs: events 10 days (v1 `days_to_sync` default); observations 12 h (v1 effective look-back).

### 6.5 Save-time validation

- Source token is valid; required read permissions are present.
- Each destination: `user/me` succeeds; the token can create event types when events are on.
- A destination may not be the same site as the source.

The designer owns layout and wording; this spec owns fields and behaviour. Form ordering uses `GlobalUISchemaOptions`; pick-lists use `FieldWithUIOptions` widgets backed by the reference actions.

## 7. Events (`sync_events`)

The steps below run for each destination. One destination's failure never blocks another.

### 7.1 Read the source

- `get_events` with `update_date` ≥ the destination's marker, `include_notes` and `include_files`, `page_size=100`.
- Apply the event-type filter (selected types; all if none) and the events area filter.
- Process page by page. The marker advances to the page's last `updated_at` only after every event on the page is written or recorded as failed (§9). A page that fails as a whole (destination unreachable) leaves the marker where it was.

### 7.2 Destination categories and event types

- Create or PATCH destination categories and v2 event types under the prefixed value (`{sys}_{value}`, lower-cased) and display name (`{SysName} - {display}`), unless the compatibility prefix flags are off.
- Inject the provenance properties into the hidden section `section-er2er-hidden`.
- When `update_schema` is off, the type must already exist with the provenance properties; otherwise report a per-type error.
- Cache results per run.
- Version mismatches and types that cannot be created are **per-type errors**: events of that type are skipped and listed; other types continue.

### 7.3 Write events

- Copy an allowlist of fields (as v1 `clean_event`).
- Keep the **source event UUID** as the destination event ID.
- Add provenance to `event_details`: `er2er_src_id`, `er2er_src_serial_number`, `er2er_src_system`, `er2er_src_service_root`.
- **Lookup:** find an existing copy by ID; fall back to matching `er2er_src_id` and `er2er_src_system`.
- **Update** only when the source `updated_at` ≥ the destination's.
- **Loop guard:** skip an event whose `er2er_src_service_root` matches the destination.
- Priority, state, title, time, location and geometry are carried on both create and update.

### 7.4 Notes and attachments

Per event and destination, state records the copied note IDs (with a content hash) and file IDs. On each sync:

- add new notes;
- PATCH notes whose content changed;
- upload files not yet copied.

This replaces v1's delete-and-re-copy of every note and file on every update. Files stream through a temp file; files over 20 MB are skipped with a warning (as `gundi-integration-earthranger`).

### 7.5 Incidents

Mirror incident-collection membership (add and remove). Each call's result is checked, and failures are reported (v1 ignored them).

### 7.6 Deletes (`reconcile_events`, daily)

- List destination copies with `er2er_src_system` = this source whose source event `updated_at` falls in the last 30 days.
- For each, if the source returns 404, delete the copy.
- Skipped when `delete_unmatched_events` is off.
- Per-destination, bounded per run; resumes from a cursor.

## 8. Subject observations (`sync_observations`)

The steps below run for each destination.

1. **Subjects:** members of the selected subject groups (all if none). Selecting what to share is separate from the destination group name (§6.2).
2. **Read** per subject: source assignments (subjectsources), then `get_observations(subject_id, since=marker, filter_flag=0)`.
3. **Filter** (v1 behaviour):
   - only points within a source assignment range;
   - only points with a valid location;
   - only points inside the tracks area filter, if set.
4. **Write** to `POST {dest}/api/v1.0/sensors/sharing/{provider_key}/status` in batches of at most 200.
   - Payload as v1: `subject_id`, `manufacturer_id` = source subject UUID, name, subject type and subtype, `subject_groups` = destination group name, `subject_additional` / `source_additional` when enabled.
   - Append `site:<host>#user:<u>#subject:<id>#rts:<ts>` to `additional.sharing_lineage`.
5. **Markers:** per destination and subject, advanced only past batches the destination accepted (2xx). New subjects backfill 12 h.
6. **Time budget:** within `MAX_ACTION_EXECUTION_TIME`, the action saves its cursor and calls `trigger_action` to continue. A Redis `set_if_absent` lease prevents overlapping runs (pattern from `gundi-integration-earthranger`).

## 9. Errors and observability

| Cause | Portal shows | Retry |
|---|---|---|
| Source or destination 401/403 | "Source/destination credentials invalid or missing permission X — update the token in …" | Not retried; reported every run until fixed |
| Destination unreachable / 5xx / timeout | "Destination X unreachable" | stamina retries within the run; marker holds |
| Rate limited (429) | Warning | Retried with backoff |
| Event type cannot be created or updated | Error naming the type | Other types continue |
| Single event or batch rejected (400) | Error naming the item with ER's reason | Item skipped and listed |

Activity logging:

- One `log_action_activity` summary per run per destination, with counts (created, updated, skipped, failed) and the new marker.
- One error entry per **distinct** cause per run, not per item.
- **Stall warning:** a warning when a destination's marker has not moved for 6 hours while the source has newer data.
- `@activity_logger` marks run start, completion and failure; errors are classified with the template's `IntegrationError` hierarchy so the portal shows status and retryability.

## 10. Migration (cdip: `migrate_er2er_v1`)

The command is a dry run by default. Batch selectors: `--ids`, `--owner`, `--limit`.

1. **Collect** enabled v1 configs for events (state has `dest_endpoint`) and for subjects. Pair events and subjects configs by source host and owner into one connection; unpaired configs become connections with only one side on.
2. **Translate:**
   - Source: v1 `endpoint` / `token`.
   - Settings: `state` → simple settings plus compatibility overrides (§6.3).
     - `within_featuregroups` → events area filter.
     - `feature_groups` → tracks area filter.
     - `subject_group` → destination subject group name.
   - Destinations: the events config's `dest_endpoint` / `dest_token` and the subjects config's outbound ER configs map to v2 EarthRanger integrations. An existing v2 ER integration for the same host and organization is reused; otherwise one is created.
   - Starting points:
     - `sync_events.start_from` = `last_successful_run`;
     - `sync_observations.start_from` = now − look-back.
3. **Create disabled:** pull actions are created with `run_on_schedule=False`.
4. **Activate** (`--activate <ids>`): in one transaction, enable the v2 schedules and set the v1 configs `enabled=False`. **Rollback** (`--rollback <ids>`) reverses it. Overlap is safe because copies keep source UUIDs and carry provenance.
5. **Report**, on every run:
   - merges;
   - compatibility-mode shares and the settings that caused it;
   - destinations that only accept v1 event types (must be fixed before activation);
   - shares already broken in v1 (stored `error`, or no successful run in 7 days), which are migrated but left disabled and flagged;
   - skipped configs with reasons.

**Rollout:**

1. Dev.
2. A few friendly prod sites, checked side by side with `compare_er_sites.py` plus event counts.
3. Remaining shares in batches.
4. Retire both CronJobs and archive `cdip-integrations/er2er` and `gundi-integration-er2er`.
5. Delete `cdip-integrations-conf/er2er`, which holds a plaintext `KEYCLOAK_CLIENT_SECRET` that must also be rotated.

## 11. Changes from v1

| v1 | v2 |
|---|---|
| 5-day / 3-day chunked windows; `days_to_sync` | Per-destination markers; first-run backfill only |
| Subjects re-send a fixed ~13 h window every run | Per-subject markers |
| `schedule_interval_minutes`, `look_back_window_hours`, `batch_size` | Internal |
| Delete and re-copy all notes and files on update | Incremental by recorded IDs |
| Incident remove failures ignored | Checked and reported |
| Subjects: all subjects always read; `subject_group` only names the destination group | Selected groups filter the source; destination name is separate |
| One area filter per job | Separate events and tracks area filters |
| v1 event types (`ER_EVENT_SCHEMA_V1_RESTRICTED`) | Not supported |
| Errors written into `state.error` | Activity logs plus classified errors |

## 12. Extension points for later phases

- **Patrols (phase 2):**
  - a `sync_patrols` action with its own per-destination markers;
  - "Share patrols" toggle and patrol-type picker (`list_patrol_types` reference action);
  - patrol segments link to destination subjects via the source subject UUID, which observations already preserve;
  - events link to patrols through the source UUIDs, which events already preserve.
- **Conditional event sharing (phase 3):** an extra predicate in the §7.1 filter step.
- **Geofence edge points (phase 3):** a transform in §8 step 3 that inserts an interpolated point at area-boundary crossings.
- **History limit (phase 3):** a cap on backfill and on reconcile scope, plus pruning of destination history older than N days.
- **Preview (phase 4):** a reference action that runs §7.1 and §8 steps 1–3 read-only and returns counts and samples.

## 13. Testing

- **Unit** (pure functions):
  - filters and area geometry;
  - event cleaning and prefixing;
  - provenance and loop guard;
  - note and file diffing;
  - observation filters and batching;
  - mode detection;
  - destination resolution.
- **Action** (template harness, recorded ER responses):
  - marker advancement;
  - partial page failure;
  - per-destination isolation;
  - per-type event-type errors;
  - continuation after the time budget;
  - lease contention.
- **Migration** (cdip pytest):
  - pairing;
  - mode detection;
  - destination reuse versus creation;
  - activate and rollback atomicity;
  - report contents.
- **Parity:** `compare_er_sites.py` and event-count comparison in dev and during the friendly-site rollout.

## 14. Risks and open items

- **Service-account access to destination auth configs.** The ER dispatcher already reads them through `gundi-client`; confirm the action runner's service account has the same access. If not, add a narrow cdip API permission.
- **v1 settings distribution.** Run `scripts/er2er_v1_inventory.py` (read-only; `python3 manage.py shell < …` in a portal pod) in prod to confirm simple-mode defaults and size the compatibility-mode population before writing the migration command.
- **Destination token permissions.** v1 event and subject destinations may hold tokens without event-type-create permission. Save-time validation and the migration report surface these.
- **Load.** Hundreds of shares on 5-minute schedules. Size the runner's concurrency and Cloud Run settings during the friendly-site rollout; destinations shared by many sources may need rate limiting.
