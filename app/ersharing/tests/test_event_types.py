import json
from dataclasses import replace

import pytest

from app.actions.configurations import SyncEventsConfig
from app.ersharing.event_types import PROVENANCE_SECTION_ID, EventTypeSync, clean_event_type, has_provenance_fields
from app.ersharing.modes import event_settings
from app.ersharing.tests.fakes import FakeSite
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
    assert schema["ui"]["sections"][PROVENANCE_SECTION_ID]["isActive"] is False
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


@pytest.mark.asyncio
async def test_unchanged_type_is_not_patched_again():
    source, dest = sites()
    await EventTypeSync(source, dest, SETTINGS).ensure("wildlife_sighting")
    stored = dest.event_types["sitea_wildlife_sighting"]
    stored["schema"] = json.loads(stored["schema"])  # ER returns the schema as an object
    stored["category"] = {"value": "sitea_monitoring", "display": "Sitea-Monitoring"}
    await EventTypeSync(source, dest, SETTINGS).ensure("wildlife_sighting")
    assert not dest.calls_to("patch_event_type_v2")
