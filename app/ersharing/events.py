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


def copied_from(event: dict, site_url: str) -> bool:
    """The event's provenance says it was copied from the given site."""
    root = (event.get("event_details") or {}).get("er2er_src_service_root")
    if not root:
        return False
    try:
        return site_host(root) == site_host(site_url)
    except ValueError:
        return False


def is_copy_of(existing: dict, src_event_id: str, site_url: str, source_system: str) -> bool:
    """The destination's event is a copy of this source event. Copies made before v1
    recorded the service root are matched by source id and source system instead."""
    details = existing.get("event_details") or {}
    if details.get("er2er_src_service_root"):
        return copied_from(existing, site_url)
    src_id, system = details.get("er2er_src_id"), details.get("er2er_src_system")
    return bool(src_id and system) and str(src_id) == str(src_event_id) and system == source_system


def is_echo(event: dict, dest_url: str) -> bool:
    """The source event is itself a copy of a destination event: don't send it back."""
    return copied_from(event, dest_url)


def wanted(event: dict, s: EventSettings, area: Optional[Area]) -> bool:
    if s.event_types and event.get("event_type") not in s.event_types:
        return False
    return area is None or area.intersects_event(event)


def is_newer(event: dict, record: Optional[EventRecord]) -> bool:
    return record is None or parse_dt(event["updated_at"]) > record.updated_at


def dest_is_newer(dest_copy: dict, event: dict) -> bool:
    """The destination's copy changed after this version of the source event, for
    example edited there. Updates go ahead only when the source is at least as new."""
    updated = dest_copy.get("updated_at")
    return bool(updated) and parse_dt(updated) > parse_dt(event["updated_at"])


def incident_ids(event: dict) -> List[str]:
    return [str(c["related_event"]["id"]) for c in event.get("is_contained_in") or []
            if c.get("type") == "contains" and c.get("related_event")]


def member_ids(incident: dict) -> List[str]:
    return [str(c["related_event"]["id"]) for c in incident.get("contains") or []
            if c.get("type") == "contains" and c.get("related_event")]


def event_label(event: dict) -> str:
    return f"#{event.get('serial_number')} ({event.get('id')})"
