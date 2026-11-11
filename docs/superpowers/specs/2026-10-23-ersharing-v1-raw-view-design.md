# EarthRanger Sharing: Legacy View — Design

## 1. Purpose

Admins moving a share from Gundi v1 have its v1 settings as JSON: the `state` of
the v1 events config and of the v1 subjects config. The Share Events and Share
Observations forms get a second view where that JSON can be pasted, read and
edited, next to the normal form.

## 2. Behaviour

- **Toggle.** Share Events and Share Observations each show a "Form / Gundi v1
  config" toggle. The form is the default view. Everyone who can edit the
  connection sees it.
- **What goes in.** Share Events takes the v1 events `state`; Share
  Observations takes the v1 subjects `state`.
- **Raw → form.** The pasted JSON fills the matching form fields. A mapped key
  that is missing or `null` clears its field, so deleting a line in the raw view
  resets that setting. Keys with no matching field are kept in the config under
  their own names, except the ones the connector drops (Share Events drops
  `days_to_sync`, `error`, `last_run` and `warning`). `dest_token` is kept as
  `*******`. Deleting a kept key's line removes it. Nothing is saved until the
  user saves.
- **Switching views with unsaved changes**, either way. A modal asks the user to save or discard
  the changes. The raw view then shows the saved settings with v1 key names.
- **Saving in the raw view.** Same result as switching to the form and saving.
- **Fields v1 doesn't have** (event types to share, subject groups to share,
  the run switch, start point) are not in the raw view. Saving from the raw
  view leaves them as saved.
- **Bad input.** Text that isn't valid JSON, or a value of the wrong type
  (`"update_schema": "yes"`), shows a plain error and keeps the raw view open.
  Nothing changes. Example messages: "This isn't valid JSON." and
  "update_schema should be true or false."
- **Rebuilt raw view.** Lists the mapped settings that are set: `null` and
  empty lists are left out. An empty string is kept, because for
  `subject_group` it means "no group". Kept keys follow, with their values as
  stored.
- **Storage.** The form values and the kept keys are stored. The raw view is
  always built from them. A masked key such as `dest_token` is stored as
  `*******`, so its real value is never stored.

## 3. Key map

| Action | v1 key | Form field |
|---|---|---|
| Share Events | `prepend_system_to_categories` | `compatibility.prepend_system_to_categories` |
| Share Events | `prepend_system_to_event_types` | `compatibility.prepend_system_to_event_types` |
| Share Events | `prepend_system_to_event_titles` | `compatibility.prepend_system_to_event_titles` |
| Share Events | `update_schema` | `compatibility.update_schema` |
| Share Events | `delete_unmatched_events` | `compatibility.delete_unmatched_events` |
| Share Events | `source_system_value` | `compatibility.source_system_value` |
| Share Events | `source_system_name` | `compatibility.source_system_name` |
| Share Events | `within_featuregroups` | `area_feature_groups` |
| Share Observations | `subject_group` | `destination_subject_group` |
| Share Observations | `feature_groups` | `area_feature_groups` |
| Share Observations | `provider_key` | `compatibility.provider_key` |
| Share Observations | `subject_additional` | `compatibility.subject_additional` |
| Share Observations | `src_user` | `compatibility.src_user` |

## 4. Connector (this repo)

- `SyncEventsConfig` and `SyncObservationsConfig` publish the map in their
  `ui_schema` under a top-level key:

  ```json
  "gundi:raw_view": {
    "title": "Legacy View",
    "map": {"within_featuregroups": "area_feature_groups",
            "update_schema": "compatibility.update_schema"}
  }
  ```

  A dot in a target names a nested field.
- A test checks that every target in each map is a field of that action's
  schema.
- Run-time behaviour is unchanged: the runner reads the same form values.

## 5. Portal (gundi-portal)

- Any action whose `ui_schema` has `gundi:raw_view` gets the toggle, so the
  feature is connector-agnostic.
- Two pure functions translate between the views:
  - `v1ToForm(json, map, schema, formData)`: for each mapped key, a value
    that is missing or `null` clears its field, and any other value has its
    type checked against the target field's schema and is set on a copy of
    `formData`. Every other key that isn't a form field name is set at the top
    level of the config, except the `drop` keys; `mask` keys are set to
    `*******`. A type mismatch returns an error naming the key and the expected
    type.
  - `formToV1(formData, map, schema)`: for each mapped field, write its value
    under the v1 key, leaving out `null` and empty lists; then add the kept
    top-level keys.
- The view adds the save-or-discard modal, the error messages and saving from
  the raw view.
- `gundi:raw_view` is removed from the `ui_schema` passed to rjsf.

## 6. Delivery

- One PR in this repo: the two maps and their test. The portal ignores the new
  key until it supports it, so this PR can ship first.
- One PR in gundi-portal: the toggle, the translation functions, the modal and
  their tests (translation both ways, type errors, kept, dropped and masked keys, the modal on
  unsaved changes, saving from the raw view).
