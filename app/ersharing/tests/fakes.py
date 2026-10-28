"""In-memory test doubles for ERSite and IntegrationStateManager."""
import copy
import itertools
import json
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from typing import Callable, Dict, List, Optional, Union

from app.ersharing.naming import site_host, site_root
from app.ersharing.timeutil import parse_dt
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
        self.event_types_v1: Dict[str, dict] = {}  # ER's v2 API can't see these
        self.categories: Dict[str, dict] = {}
        self.subject_groups: List[dict] = []
        self.assignments: List[dict] = []
        self.subject_sources: Dict[str, List[dict]] = {}
        self.observations: Dict[str, List[dict]] = {}
        self.feature_groups: Dict[str, dict] = {}
        self.files: Dict[str, bytes] = {}
        self.downloaded_paths: List[str] = []
        self.uploaded: Dict[str, bytes] = {}
        self.sharing_posts: List[tuple] = []
        self.calls: List[tuple] = []
        self.failures: Dict[str, Failure] = {}
        self.unreadable: set = set()
        # When set, creates and edits stamp updated_at with it, as a real site stamps the write time.
        self.write_time: Optional[str] = None
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

    async def can_read(self, path, params=None):
        self._hit("can_read", path, params)
        return path not in self.unreadable

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
        stored.setdefault("updated_at", self.write_time or "2026-10-15T00:00:00+00:00")
        self.events[stored["id"]] = stored
        return copy.deepcopy(stored)

    async def patch_event(self, event_id, data):
        self._hit("patch_event", event_id, data)
        self._event(event_id).update(copy.deepcopy(data))
        if self.write_time:
            self.events[event_id]["updated_at"] = self.write_time
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

    @asynccontextmanager
    async def download_file(self, url, filename, max_bytes):
        self._hit("download_file", url)
        content = self.files[url]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, os.path.basename(filename))
            with open(path, "wb") as f:
                f.write(content)
            self.downloaded_paths.append(path)
            yield path if len(content) <= max_bytes else None

    async def post_file(self, event_id, path, comment=""):
        filename = os.path.basename(path)
        self._hit("post_file", event_id, filename)
        with open(path, "rb") as f:
            self.uploaded[filename] = f.read()
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

    async def list_event_types_v1(self):
        self._hit("list_event_types_v1")
        return copy.deepcopy(list(self.event_types_v1.values()))

    async def get_event_type_v2(self, value):
        self._hit("get_event_type_v2", value)
        return copy.deepcopy(self.event_types.get(value))

    async def post_event_type_v2(self, data):
        self._hit("post_event_type_v2", data)
        self.event_types[data["value"]] = copy.deepcopy(data)

    async def patch_event_type_v2(self, data):
        self._hit("patch_event_type_v2", data)
        self.event_types[data["value"]] = {**self.event_types.get(data["value"], {}), **copy.deepcopy(data)}

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

    async def set_if_absent(self, integration_id, action_id, *, ttl_seconds, source_id="no-source", value="1"):
        key = (integration_id, action_id, source_id)
        if key in self.data:
            return False
        self.data[key] = json.loads(value)
        return True

    async def delete_state(self, integration_id, action_id, source_id="no-source"):
        self.data.pop((integration_id, action_id, source_id), None)

    async def delete_if_equal(self, integration_id, action_id, value, source_id="no-source"):
        key = (integration_id, action_id, source_id)
        if key in self.data and json.dumps(self.data[key]) == value:
            del self.data[key]
            return True
        return False
