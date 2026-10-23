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
