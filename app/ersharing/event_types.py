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

# Legacy names kept for compatibility with existing copies (spec D8): v1 wrote
# these "er2er" names onto hundreds of destination sites, and matching existing
# copies, the echo guard and reconciliation all depend on them. Do not rename.
#
# Provenance fields written into every copy's event_details and declared in the
# destination type's schema. serial_number is ER's integer serial, declared
# "number" as the V2 meta-schema requires.
PROVENANCE_FIELDS = {
    "er2er_src_id": {"type": "string", "title": "ID of Source Event"},
    "er2er_src_system": {"type": "string", "title": "Name of Source ER System"},
    "er2er_src_serial_number": {"type": "number", "title": "Serial Number of Source Event"},
    "er2er_src_service_root": {"type": "string", "title": "Service Root of Source Event"},
}

TYPE_NOT_READY = "Some events weren't copied because their event type couldn't be set up at the destination."

# A V2 field is hidden from the report form by living in an isActive=False section.
PROVENANCE_SECTION_ID = "section-er2er-hidden"


def _normalize_enum_names(properties: Dict) -> None:
    """ER expects enumNames as a value->label mapping; legacy schemas use a list."""
    for prop in properties.values():
        if isinstance(prop, dict) and isinstance(prop.get("enumNames"), list):
            prop["enumNames"] = {v: v for v in prop["enumNames"]}


def add_provenance_fields_to_v2_schema(schema: Dict) -> Dict:
    """Declare the provenance fields in a V2 schema, hidden. Idempotent."""
    json_schema = schema.setdefault("json", {})
    properties = json_schema.setdefault("properties", {})
    ui = schema.setdefault("ui", {})
    ui_fields = ui.setdefault("fields", {})
    ui_sections = ui.setdefault("sections", {})
    ui_order = ui.setdefault("order", [])

    section = ui_sections.setdefault(PROVENANCE_SECTION_ID, {
        "columns": 1, "isActive": False, "label": "", "leftColumn": [], "rightColumn": [], "conditions": [],
    })
    section["isActive"] = False
    if PROVENANCE_SECTION_ID not in ui_order:
        ui_order.append(PROVENANCE_SECTION_ID)
    in_section = {entry.get("name") for entry in section.get("leftColumn", [])}

    for key, field in PROVENANCE_FIELDS.items():
        properties[key] = {"type": field["type"], "title": field["title"], "description": "", "deprecated": False}
        if field["type"] == "number":
            ui_fields[key] = {"type": "NUMERIC", "placeholder": "", "parent": PROVENANCE_SECTION_ID}
        else:
            ui_fields[key] = {"type": "TEXT", "inputType": "SHORT_TEXT", "placeholder": "",
                              "parent": PROVENANCE_SECTION_ID}
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
    cleaned["schema"] = json.dumps(add_provenance_fields_to_v2_schema(schema), indent=4)
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
    return all(key in properties for key in PROVENANCE_FIELDS)


def _as_schema(schema):
    return json.loads(schema) if isinstance(schema, str) else schema


def same_event_type(wanted: dict, existing: dict) -> bool:
    """The destination type already has every field we would send."""
    for key, value in wanted.items():
        current = existing.get(key)
        if key == "schema":
            value, current = _as_schema(value), _as_schema(current)
        elif key == "category":
            current = category_value(existing)
        if current != value:
            return False
    return True


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
            self.failures[source_value] = f"{source_value}: {e.message}"
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
                    "missing at the destination or lacks the EarthRanger Sharing provenance fields, "
                    "and updating destination event types is turned off"
                )
            return existing
        await self._ensure_category(category_value(src))
        if existing is None:
            await self._dest.post_event_type_v2(wanted)
        elif not same_event_type(wanted, existing):
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
