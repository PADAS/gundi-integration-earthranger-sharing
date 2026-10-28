from dataclasses import replace

import pytest

from app.actions.configurations import SyncEventsConfig
from app.ersharing.events import clean_event, event_label, incident_ids, is_echo, is_newer, wanted
from app.ersharing.modes import event_settings
from app.ersharing.state import EventRecord
from app.ersharing.tests.fakes import make_event
from app.ersharing.timeutil import parse_dt

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


def test_echo_guard_compares_whole_hosts():
    event = make_event("e1", "2026-10-01T00:00:00Z", event_details={"er2er_src_service_root": ROOT})
    assert not is_echo(event, "https://a.pamdas.org")


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
