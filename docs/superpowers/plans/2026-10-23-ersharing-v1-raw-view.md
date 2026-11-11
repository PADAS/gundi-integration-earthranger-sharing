# Gundi v1 Config View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let admins paste a Gundi v1 er2er `state` JSON into the Share Events and Share Observations forms, and switch between that raw view and the normal form.

**Architecture:**
- **Connector (this repo).** Each sync action publishes a key map (v1 key → form field path) in its `ui_schema` under `gundi:raw_view`. Runtime behaviour is unchanged.
- **Portal (gundi-portal).** Any action whose `ui_schema` carries `gundi:raw_view` gets a "Form / <title>" toggle in `ActionSectionForm`. Two pure functions translate between the views in the browser, and the raw view's edits go through the same stash and Save as the form's edits.

**Tech Stack:**
- Connector: Python 3.10, pydantic 1.10, pytest 7.
- Portal: React 18, TypeScript, @rjsf 5, @monaco-editor/react, react-i18next, Jest 29 with @testing-library/react.

**Spec:** `docs/superpowers/specs/2026-10-23-ersharing-v1-raw-view-design.md`.

## Global Constraints

- **Annotation key:** `gundi:raw_view`. Value: `{"title": "Gundi v1 config", "map": {<v1 key>: <field path>}}`. A dot in a path names a nested field (`compatibility.update_schema`).
- **Share Events map:** `prepend_system_to_categories`, `prepend_system_to_event_types`, `prepend_system_to_event_titles`, `update_schema`, `delete_unmatched_events`, `source_system_value`, `source_system_name` → `compatibility.<same name>`; `within_featuregroups` → `area_feature_groups`.
- **Share Observations map:** `subject_group` → `destination_subject_group`; `feature_groups` → `area_feature_groups`; `provider_key`, `subject_additional`, `src_user` → `compatibility.<same name>`.
- **Raw → form:**
  - Mapped keys set their field.
  - A mapped key that is missing or `null` clears its field.
  - Unmapped keys are ignored without a message.
  - Form fields not in the map keep their value.
- **Form → raw:** mapped fields with a value are written under their v1 key, in map order. `undefined`, `null` and empty lists are left out. `""` and `false` are kept.
- **Bad input** blocks switching back to the form and shows a plain message. Messages, verbatim:
  - `This isn't valid JSON.`
  - `This should be a JSON object, for example {"update_schema": true}.`
  - `{{key}} should be true or false.`
  - `{{key}} should be text.`
  - `{{key}} should be a number.`
  - `{{key}} should be a list of text values.`
- **Nothing is saved from the raw text.** Only form values are stored. The raw view is always rebuilt from them.
- **Unsaved edits on Form → raw** open the shared `UnsavedSectionPrompt` (Save / Discard changes). The raw view then shows the saved settings.
- **Portal work** happens on a new branch `feature-v1-raw-config-view` cut from `origin/main`, in a separate git worktree (`git worktree add ../gundi-portal-raw-view -b feature-v1-raw-config-view origin/main`), so any work in progress in the main checkout is untouched.
- **Delivery:** the connector change ships on its own; the portal ignores `gundi:raw_view` until Task 3 lands.

## File Structure

| Repo | File | Responsibility |
|---|---|---|
| connector | `app/actions/configurations.py` | The two key maps and the `raw_view()` annotation helper; publish them from both sync configs' `ui_schema` |
| connector | `app/actions/tests/test_raw_view.py` | Every map target is a real form field; the maps match the spec |
| portal | `src/components/common/configPanel/rawView.ts` | Pure: read the annotation, strip it from the ui_schema, translate both ways |
| portal | `src/components/common/configPanel/RawConfigEditor.tsx` | The JSON editor plus its error line |
| portal | `src/components/common/configPanel/ActionSectionForm.tsx` | Toggle, view state, raw edits into `onChange`, the Save/Discard prompt |
| portal | `src/components/routes/flowMap/ConfigurationsTab.tsx` | Hand the section's save/discard to `ActionSectionForm` |
| portal | `src/__test__/components/common/configPanel/rawView.test.ts` | Translation tests |
| portal | `src/__test__/components/common/configPanel/ActionSectionFormRawView.test.tsx` | Toggle and editor behaviour |

---

### Task 1: Publish the v1 key maps from the connector

**Files:**
- Modify: `app/actions/configurations.py` (the `reference()` helper area, `SyncEventsConfig.ui_schema`, `SyncObservationsConfig.ui_schema`)
- Create: `app/actions/tests/test_raw_view.py`

**Interfaces:**
- Produces: `RAW_VIEW_TITLE: str`, `EVENTS_V1_KEYS: Dict[str, str]`, `OBSERVATIONS_V1_KEYS: Dict[str, str]`, `raw_view(v1_keys) -> dict`; `SyncEventsConfig.ui_schema()["gundi:raw_view"]` and `SyncObservationsConfig.ui_schema()["gundi:raw_view"]`.

- [ ] **Step 1: Add the maps and the helper**

In `app/actions/configurations.py`, directly below `def reference(...)`, add:

```python
RAW_VIEW_TITLE = "Gundi v1 config"

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


def raw_view(v1_keys: dict) -> dict:
    """gundi:raw_view annotation: the portal shows a "Gundi v1 config" view of this
    action and translates between it and the form with this key map."""
    return {"title": RAW_VIEW_TITLE, "map": dict(v1_keys)}
```

- [ ] **Step 2: Publish them**

In `SyncEventsConfig.ui_schema`, before `return base`, add:

```python
        base["gundi:raw_view"] = raw_view(EVENTS_V1_KEYS)
```

In `SyncObservationsConfig.ui_schema`, before `return base`, add:

```python
        base["gundi:raw_view"] = raw_view(OBSERVATIONS_V1_KEYS)
```

- [ ] **Step 3: Write the tests**

`app/actions/tests/test_raw_view.py`:

```python
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
    assert raw["title"] == "Gundi v1 config"
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
```

- [ ] **Step 4: Run the tests**

Run: `pytest app/actions/tests/test_raw_view.py -v`
Expected: 4 passed.

Run: `pytest`
Expected: the whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add app/actions/configurations.py app/actions/tests/test_raw_view.py
git commit -m "feat: publish the Gundi v1 key maps for the share forms"
```

---

### Task 2: Translation functions in the portal

Work in the portal worktree from Global Constraints (`../gundi-portal-raw-view`, branch `feature-v1-raw-config-view`). Run `npm ci` there first.

**Files:**
- Create: `src/components/common/configPanel/rawView.ts`
- Create: `src/__test__/components/common/configPanel/rawView.test.ts`

**Interfaces:**
- Produces:
  - `RAW_VIEW_KEY = 'gundi:raw_view'`
  - `type RawView = { title: string; map: Record<string, string> }`
  - `rawViewOf(action: any): RawView | undefined`
  - `withoutRawView(uiSchema: any): any`
  - `fieldSchemaAt(schema: any, path: string): any`
  - `v1ToForm(text: string, raw: RawView, schema: any, formData: any, t: Translate): { data: any } | { error: string }`
  - `formToV1(formData: any, raw: RawView): Record<string, any>`
  - `formatV1(formData: any, raw: RawView): string`

- [ ] **Step 1: Write `rawView.ts`**

```ts
// The "raw view" a connector can ask for with a `gundi:raw_view` annotation on an
// action's ui_schema: { title, map: { <legacy key>: <field path> } }. The user
// pastes a legacy config as JSON and the form fills from it; the form's saved
// values show back in the legacy key names. Pure functions only — the view
// itself lives in ActionSectionForm.

export const RAW_VIEW_KEY = 'gundi:raw_view';

export type RawView = { title: string; map: Record<string, string> };

type Translate = (key: string, options?: any) => string;

export const rawViewOf = (action: any): RawView | undefined => {
  const raw = action?.ui_schema?.[RAW_VIEW_KEY];
  if (!raw || typeof raw !== 'object') return undefined;
  if (!raw.map || typeof raw.map !== 'object' || Array.isArray(raw.map))
    return undefined;
  return {
    title: typeof raw.title === 'string' && raw.title ? raw.title : 'Raw',
    map: raw.map,
  };
};

// rjsf has no use for the annotation; it is ours alone.
export const withoutRawView = (uiSchema: any) => {
  if (!uiSchema || !(RAW_VIEW_KEY in uiSchema)) return uiSchema;
  const { [RAW_VIEW_KEY]: _omitted, ...rest } = uiSchema;
  return rest;
};

const refName = (node: any): string | undefined => {
  const ref = node?.$ref ?? node?.allOf?.find((s: any) => s?.$ref)?.$ref;
  return typeof ref === 'string' ? ref.split('/').pop() : undefined;
};

// The schema of the field at a dotted path. pydantic wraps nested models as
// { allOf: [{ $ref }] }, so each step follows that into `definitions`.
export const fieldSchemaAt = (schema: any, path: string): any => {
  let node = schema;
  for (const part of path.split('.')) {
    const name = refName(node);
    if (name) node = schema?.definitions?.[name];
    node = node?.properties?.[part];
    if (!node) return undefined;
  }
  const name = refName(node);
  return name ? { ...schema?.definitions?.[name], ...node } : node;
};

const getAt = (data: any, path: string) =>
  path
    .split('.')
    .reduce((node, key) => (node == null ? undefined : node[key]), data);

// Immutable set; `undefined` removes the key so the field falls back to its default.
const setAt = (data: any, path: string, value: any): any => {
  const [head, ...rest] = path.split('.');
  const base = data && typeof data === 'object' ? data : {};
  if (rest.length === 0) {
    const { [head]: _old, ...others } = base;
    return value === undefined ? others : { ...others, [head]: value };
  }
  return { ...base, [head]: setAt(base[head], rest.join('.'), value) };
};

const typeError = (
  key: string,
  field: any,
  value: any,
  t: Translate,
): string | undefined => {
  switch (field?.type) {
    case 'boolean':
      return typeof value === 'boolean'
        ? undefined
        : t('{{key}} should be true or false.', { key });
    case 'string':
      return typeof value === 'string'
        ? undefined
        : t('{{key}} should be text.', { key });
    case 'integer':
    case 'number':
      return typeof value === 'number'
        ? undefined
        : t('{{key}} should be a number.', { key });
    case 'array':
      return Array.isArray(value) &&
        (field.items?.type !== 'string' ||
          value.every((v: any) => typeof v === 'string'))
        ? undefined
        : t('{{key}} should be a list of text values.', { key });
    default:
      return undefined;
  }
};

// Legacy JSON -> form data. Mapped keys that are missing or null clear their
// field, so deleting a line in the raw view resets that setting; fields the map
// doesn't name keep their value; keys the map doesn't name are ignored.
export const v1ToForm = (
  text: string,
  raw: RawView,
  schema: any,
  formData: any,
  t: Translate,
): { data: any } | { error: string } => {
  let parsed: any;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { error: t("This isn't valid JSON.") };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))
    return {
      error: t('This should be a JSON object, for example {"update_schema": true}.'),
    };
  let data = formData ?? {};
  for (const [legacyKey, path] of Object.entries(raw.map)) {
    const value = parsed[legacyKey];
    if (value === undefined || value === null) {
      data = setAt(data, path, undefined);
      continue;
    }
    const error = typeError(legacyKey, fieldSchemaAt(schema, path), value, t);
    if (error) return { error };
    data = setAt(data, path, value);
  }
  return { data };
};

// Form data -> legacy JSON, in map order. Unset values and empty lists are left
// out; "" and false are real choices and stay.
export const formToV1 = (formData: any, raw: RawView): Record<string, any> => {
  const out: Record<string, any> = {};
  for (const [legacyKey, path] of Object.entries(raw.map)) {
    const value = getAt(formData, path);
    if (value === undefined || value === null) continue;
    if (Array.isArray(value) && value.length === 0) continue;
    out[legacyKey] = value;
  }
  return out;
};

export const formatV1 = (formData: any, raw: RawView): string =>
  JSON.stringify(formToV1(formData, raw), null, 2);
```

- [ ] **Step 2: Write the tests**

`src/__test__/components/common/configPanel/rawView.test.ts`:

```ts
import {
  RAW_VIEW_KEY,
  fieldSchemaAt,
  formToV1,
  formatV1,
  rawViewOf,
  v1ToForm,
  withoutRawView,
} from 'components/common/configPanel/rawView';

// What the earthranger_sharing connector publishes for Share Events (trimmed).
const schema = {
  type: 'object',
  properties: {
    run_on_schedule: { type: 'boolean' },
    event_types: { type: 'array', items: { type: 'string' } },
    area_feature_groups: { type: 'array', items: { type: 'string' } },
    compatibility: { allOf: [{ $ref: '#/definitions/EventsCompatibility' }] },
  },
  definitions: {
    EventsCompatibility: {
      type: 'object',
      properties: {
        update_schema: { type: 'boolean' },
        source_system_value: { type: 'string' },
      },
    },
  },
};
const raw = {
  title: 'Gundi v1 config',
  map: {
    update_schema: 'compatibility.update_schema',
    source_system_value: 'compatibility.source_system_value',
    within_featuregroups: 'area_feature_groups',
  },
};
const t = (key: string, options?: any) =>
  key.replace('{{key}}', options?.key ?? '');

describe('rawViewOf / withoutRawView', () => {
  it('reads the annotation and strips it for rjsf', () => {
    const action = { ui_schema: { 'ui:order': ['a'], [RAW_VIEW_KEY]: raw } };
    expect(rawViewOf(action)).toEqual(raw);
    expect(withoutRawView(action.ui_schema)).toEqual({ 'ui:order': ['a'] });
  });

  it('is absent without a usable map', () => {
    expect(rawViewOf({ ui_schema: {} })).toBeUndefined();
    expect(rawViewOf({ ui_schema: { [RAW_VIEW_KEY]: { title: 'x' } } })).toBeUndefined();
  });
});

describe('fieldSchemaAt', () => {
  it('follows allOf/$ref into nested models', () => {
    expect(fieldSchemaAt(schema, 'compatibility.update_schema')).toEqual({ type: 'boolean' });
    expect(fieldSchemaAt(schema, 'area_feature_groups').type).toBe('array');
    expect(fieldSchemaAt(schema, 'compatibility.nope')).toBeUndefined();
  });
});

describe('v1ToForm', () => {
  const form = { run_on_schedule: true, event_types: ['a'], compatibility: { update_schema: true } };

  it('fills mapped fields and keeps fields the map does not name', () => {
    const result = v1ToForm(
      '{"update_schema": false, "within_featuregroups": ["fg"], "dest_token": "x"}',
      raw, schema, form, t,
    );
    expect(result).toEqual({
      data: {
        run_on_schedule: true,
        event_types: ['a'],
        area_feature_groups: ['fg'],
        compatibility: { update_schema: false },
      },
    });
  });

  it('clears a mapped field whose key is missing or null', () => {
    const result = v1ToForm('{"source_system_value": null}', raw, schema, form, t);
    expect(result).toEqual({ data: { run_on_schedule: true, event_types: ['a'], compatibility: {} } });
  });

  it('rejects text that is not JSON', () => {
    expect(v1ToForm('{nope', raw, schema, form, t)).toEqual({ error: "This isn't valid JSON." });
  });

  it('rejects JSON that is not an object', () => {
    expect(v1ToForm('[1]', raw, schema, form, t)).toEqual({
      error: 'This should be a JSON object, for example {"update_schema": true}.',
    });
  });

  it('names the key whose value has the wrong type', () => {
    expect(v1ToForm('{"update_schema": "yes"}', raw, schema, form, t)).toEqual({
      error: 'update_schema should be true or false.',
    });
    expect(v1ToForm('{"within_featuregroups": [1]}', raw, schema, form, t)).toEqual({
      error: 'within_featuregroups should be a list of text values.',
    });
    expect(v1ToForm('{"source_system_value": 3}', raw, schema, form, t)).toEqual({
      error: 'source_system_value should be text.',
    });
  });
});

describe('formToV1', () => {
  it('writes set values under their legacy keys and drops unset ones', () => {
    const data = {
      area_feature_groups: [],
      compatibility: { update_schema: false, source_system_value: '' },
    };
    expect(formToV1(data, raw)).toEqual({ update_schema: false, source_system_value: '' });
  });

  it('round-trips through v1ToForm', () => {
    const data = { area_feature_groups: ['fg'], compatibility: { update_schema: false } };
    expect(v1ToForm(formatV1(data, raw), raw, schema, {}, t)).toEqual({ data });
  });
});
```

- [ ] **Step 3: Run the tests**

Run: `npx jest src/__test__/components/common/configPanel/rawView.test.ts`
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/components/common/configPanel/rawView.ts src/__test__/components/common/configPanel/rawView.test.ts
git commit -m "feat: translate between a legacy config and an action form"
```

---

### Task 3: The raw view in `ActionSectionForm`, wired into the flow map

**Files:**
- Create: `src/components/common/configPanel/RawConfigEditor.tsx`
- Modify: `src/components/common/configPanel/ActionSectionForm.tsx`
- Modify: `src/components/routes/flowMap/ConfigurationsTab.tsx` (`renderActionForm`)
- Create: `src/__test__/components/common/configPanel/ActionSectionFormRawView.test.tsx`

**Interfaces:**
- Consumes: everything Task 2 produces; `PendingEdits`, `SectionPrompt` and the default export from `./UnsavedSectionPrompt`; `SegmentedToggle` from `components/common/SegmentedToggle`; `MONACO_OPTIONS` from `components/routes/schema-builder/editorConfig`; `configFor` from `./actionSetup`.
- Produces: a new optional `ActionSectionForm` prop `pendingEdits?: PendingEdits` (`{ hasEdits(): boolean; save(): Promise<boolean>; discard(): void }`).

- [ ] **Step 1: Write `RawConfigEditor.tsx`**

```tsx
import React from 'react';
import Editor from '@monaco-editor/react';
import { MONACO_OPTIONS } from 'components/routes/schema-builder/editorConfig';

// The JSON side of an action's raw view: the editor and the one line that says
// why the text can't be used yet.
const RawConfigEditor = ({
  value,
  onChange,
  error,
  readOnly,
}: {
  value: string;
  onChange: (text: string) => void;
  error: string | null;
  readOnly: boolean;
}) => (
  <div>
    <div className="h-72 overflow-hidden rounded border border-ui-field-outline">
      <Editor
        height="100%"
        language="json"
        value={value}
        theme="vs"
        options={MONACO_OPTIONS(readOnly)}
        onChange={(text) => onChange(text ?? '')}
      />
    </div>
    {error && (
      <p role="alert" className="mt-2 text-sm text-ui-status-error-destructive">
        {error}
      </p>
    )}
  </div>
);

export default RawConfigEditor;
```

- [ ] **Step 2: Add the imports and the prop to `ActionSectionForm.tsx`**

Add to the imports:

```tsx
import { SegmentedToggle } from 'components/common/SegmentedToggle';
import RawConfigEditor from './RawConfigEditor';
import UnsavedSectionPrompt, {
  PendingEdits,
  SectionPrompt,
} from './UnsavedSectionPrompt';
import {
  RAW_VIEW_KEY,
  formatV1,
  rawViewOf,
  v1ToForm,
  withoutRawView,
} from './rawView';
```

Add to `ActionSectionFormProps`, after `showDescription`:

```tsx
  // This section's own save and discard, for actions with a raw view: leaving
  // the form with unsaved edits asks Save or Discard first, so the raw view can
  // show what is saved. Screens without per-section saving leave it out, and
  // the raw view then shows the values on screen.
  pendingEdits?: PendingEdits;
```

Add `pendingEdits,` to the destructured props, after `showDescription = true,`.

- [ ] **Step 3: Add the view state and handlers**

Directly after the existing `useEffect(() => { setTested(false); }, [action.id, target.id]);`, add:

```tsx
  // ---- raw view (connector-provided `gundi:raw_view`) ----------------------
  const rawView = rawViewOf(action);
  const [view, setView] = useState<'form' | 'raw'>('form');
  // null until the user types: until then the editor shows rawSource().
  const [rawText, setRawText] = useState<string | null>(null);
  const [rawError, setRawError] = useState<string | null>(null);
  // The form data the raw edits apply on top of, fixed at the first edit.
  const [rawBase, setRawBase] = useState<any>(null);
  // What the form remounts with after leaving the raw view: the formData prop
  // trails the stash (useConfigEditor re-renders only on a section's first edit).
  const [formFromRaw, setFormFromRaw] = useState<any>(null);
  const [prompt, setPrompt] = useState<SectionPrompt | null>(null);

  useEffect(() => {
    setView('form');
    setRawText(null);
    setRawError(null);
    setRawBase(null);
    setFormFromRaw(null);
  }, [action.id, target.id]);
  // A save or a cancel rebases the section on what is stored.
  useEffect(() => {
    setRawText(null);
    setRawError(null);
    setRawBase(null);
    setFormFromRaw(null);
  }, [formVersion]);

  const currentData = () => (getFormData ? getFormData() : formData) ?? {};
  // With per-section saving the raw view shows what is saved (the prompt has
  // just settled any edits); without it, the values on screen.
  const rawSource = () =>
    (pendingEdits && saved ? saved.data : currentData()) ?? {};

  const showRaw = () => {
    setRawText(null);
    setRawError(null);
    setRawBase(null);
    setView('raw');
  };

  const onRawChange = (text: string) => {
    if (!rawView) return;
    const base = rawBase ?? rawSource();
    if (rawBase === null) setRawBase(base);
    setRawText(text);
    const result = v1ToForm(text, rawView, action.schema, base, t);
    if ('error' in result) {
      setRawError(result.error);
      // Unusable text changes nothing: a Save now writes the starting values.
      onChange(base, RAW_VIEW_KEY);
      return;
    }
    setRawError(null);
    onChange(result.data, RAW_VIEW_KEY);
  };

  const onSelectView = (next: string) => {
    if (!rawView) return;
    if (next === 'raw') {
      if (pendingEdits?.hasEdits()) {
        setPrompt({
          onSave: () => pendingEdits.save(),
          onDiscard: () => pendingEdits.discard(),
          proceed: showRaw,
        });
        return;
      }
      showRaw();
      return;
    }
    if (rawText !== null) {
      if (rawError) return;
      const result = v1ToForm(
        rawText,
        rawView,
        action.schema,
        rawBase ?? rawSource(),
        t,
      );
      if ('error' in result) {
        setRawError(result.error);
        return;
      }
      setFormFromRaw(result.data);
    }
    setView('form');
  };
```

- [ ] **Step 4: Render the toggle, the editor and the prompt**

Directly before `<SchemaForm`, insert:

```tsx
      {rawView && (
        <div className="mb-3 flex justify-end">
          <SegmentedToggle
            value={view}
            options={[
              { value: 'form', label: t('Form') },
              { value: 'raw', label: rawView.title },
            ]}
            onChange={onSelectView}
            ariaLabel={t('Configuration view')}
          />
        </div>
      )}
      {rawView && view === 'raw' ? (
        <RawConfigEditor
          value={rawText ?? formatV1(rawSource(), rawView)}
          onChange={onRawChange}
          error={rawError}
          readOnly={!fieldsEditable}
        />
      ) : (
```

Then change the `<SchemaForm ... />` element:
- `ui_schema={{ ...(action.ui_schema ? { ...action.ui_schema, ...` becomes `ui_schema={{ ...(action.ui_schema ? { ...withoutRawView(action.ui_schema), ...`
- `formdata={formData}` becomes `formdata={formFromRaw ?? formData}`

Close the conditional right after the `/>` that ends `<SchemaForm`, and add the prompt:

```tsx
      )}
      <UnsavedSectionPrompt prompt={prompt} onClose={() => setPrompt(null)} />
```

- [ ] **Step 5: Wire the flow map**

In `src/components/routes/flowMap/ConfigurationsTab.tsx`, in `renderActionForm`'s `<ActionSectionForm`, add after the `onChange` prop:

```tsx
      pendingEdits={{
        hasEdits: () => editor.isActionDirty(bundle.id, action.id),
        save: () => editor.save(bundle, action.id),
        discard: () => editor.cancel(bundle, action.id),
      }}
```

- [ ] **Step 6: Write the tests**

`src/__test__/components/common/configPanel/ActionSectionFormRawView.test.tsx`:

```tsx
import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import ActionSectionForm from 'components/common/configPanel/ActionSectionForm';
import { componentWrapper } from '__test__/utils/testUtils';

const shareEvents = {
  id: 'action-share-events',
  name: 'Share Events',
  value: 'sync_events',
  type: 'pull',
  schema: {
    type: 'object',
    properties: {
      event_types: { type: 'array', items: { type: 'string' }, title: 'Event types' },
      area_feature_groups: { type: 'array', items: { type: 'string' }, title: 'Area' },
      compatibility: { allOf: [{ $ref: '#/definitions/EventsCompatibility' }] },
    },
    definitions: {
      EventsCompatibility: {
        type: 'object',
        properties: { update_schema: { type: 'boolean', title: 'Update schema' } },
      },
    },
  },
  ui_schema: {
    'gundi:raw_view': {
      title: 'Gundi v1 config',
      map: {
        update_schema: 'compatibility.update_schema',
        within_featuregroups: 'area_feature_groups',
      },
    },
  },
};

const savedData = {
  event_types: ['wildlife'],
  area_feature_groups: ['fg1'],
  compatibility: { update_schema: false },
};
const target = {
  id: 'source-1',
  actions: [shareEvents],
  configurations: [{ id: 'cfg-1', action: { id: shareEvents.id }, data: savedData }],
};

const renderForm = (props: any = {}) => {
  const onChange = jest.fn();
  render(
    componentWrapper(
      <ActionSectionForm
        target={target}
        action={shareEvents}
        formData={savedData}
        onChange={onChange}
        renderScheduleToggle={false}
        {...props}
      />,
    ),
  );
  return onChange;
};

const editor = () => screen.getByTestId('monaco-editor') as HTMLTextAreaElement;
const openRaw = () => fireEvent.click(screen.getByText('Gundi v1 config'));

describe('ActionSectionForm — raw view', () => {
  it('shows no toggle when the connector asks for no raw view', () => {
    renderForm({ action: { ...shareEvents, ui_schema: {} } });
    expect(screen.queryByText('Gundi v1 config')).not.toBeInTheDocument();
  });

  it('shows the saved settings in legacy key names', () => {
    renderForm();
    openRaw();
    expect(JSON.parse(editor().value)).toEqual({
      update_schema: false,
      within_featuregroups: ['fg1'],
    });
  });

  it('stashes the translated settings and keeps unmapped fields', () => {
    const onChange = renderForm();
    openRaw();
    fireEvent.change(editor(), { target: { value: '{"update_schema": true}' } });
    expect(onChange).toHaveBeenLastCalledWith(
      { event_types: ['wildlife'], compatibility: { update_schema: true } },
      'gundi:raw_view',
    );
  });

  it('blocks invalid JSON and changes nothing', () => {
    const onChange = renderForm();
    openRaw();
    fireEvent.change(editor(), { target: { value: '{nope' } });
    expect(screen.getByRole('alert')).toHaveTextContent("This isn't valid JSON.");
    expect(onChange).toHaveBeenLastCalledWith(savedData, 'gundi:raw_view');
    fireEvent.click(screen.getByText('Form'));
    expect(editor()).toBeInTheDocument();
  });

  it('names a value of the wrong type', () => {
    renderForm();
    openRaw();
    fireEvent.change(editor(), { target: { value: '{"update_schema": "yes"}' } });
    expect(screen.getByRole('alert')).toHaveTextContent(
      'update_schema should be true or false.',
    );
  });

  it('asks to save or discard unsaved edits before opening the raw view', () => {
    const pendingEdits = {
      hasEdits: jest.fn(() => true),
      save: jest.fn(async () => true),
      discard: jest.fn(),
    };
    renderForm({ pendingEdits });
    openRaw();
    expect(screen.getByText('Save changes?')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Discard changes'));
    expect(pendingEdits.discard).toHaveBeenCalled();
    expect(editor()).toBeInTheDocument();
  });

  it('saves through the section when the user picks Save', async () => {
    const pendingEdits = {
      hasEdits: jest.fn(() => true),
      save: jest.fn(async () => true),
      discard: jest.fn(),
    };
    renderForm({ pendingEdits });
    openRaw();
    fireEvent.click(screen.getByText('Save'));
    expect(pendingEdits.save).toHaveBeenCalled();
    expect(await screen.findByTestId('monaco-editor')).toBeInTheDocument();
  });
});
```

- [ ] **Step 7: Run the tests and the type check**

Run: `npx jest src/__test__/components/common/configPanel`
Expected: all tests pass, including the existing `ActionSectionForm*` suites.

Run: `npx tsc --noEmit -p tsconfig.json`
Expected: no errors.

Run: `npm test`
Expected: the whole suite passes.

- [ ] **Step 8: Commit**

```bash
git add src/components/common/configPanel/RawConfigEditor.tsx src/components/common/configPanel/ActionSectionForm.tsx src/components/routes/flowMap/ConfigurationsTab.tsx src/__test__/components/common/configPanel/ActionSectionFormRawView.test.tsx
git commit -m "feat: add a raw legacy-config view to action forms"
```

---

### Task 4: Check it end to end in dev

- [ ] **Step 1: Register the connector's new form**

From this repo, with the dev Gundi environment variables loaded:

```bash
python -m app.register --slug earthranger_sharing --name "EarthRanger Sharing"
```

Expected: `Registering integration type 'earthranger_sharing'...DONE`.

- [ ] **Step 2: Run the portal against dev**

In the portal worktree: `npm start`. Open an EarthRanger Sharing connection's Share Events section.

Confirm:
- the "Form / Gundi v1 config" toggle shows, with the form selected;
- pasting `{"update_schema": false, "within_featuregroups": [], "dest_token": "x"}` and switching to the form turns "Allow updates to destination event types" off and leaves the other fields as they were;
- `{"update_schema": "yes"}` shows "update_schema should be true or false." and keeps the raw view open;
- editing a form field and switching to the raw view opens "Save changes?", and after Save the raw view shows the saved values;
- Share Observations behaves the same with `{"subject_group": "", "provider_key": "das_provider"}`.
