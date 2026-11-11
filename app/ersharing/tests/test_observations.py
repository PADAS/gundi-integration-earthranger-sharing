from dataclasses import replace

from app.actions.configurations import SyncObservationsConfig
from app.ersharing.modes import observation_settings
from app.ersharing.observations import (
    OVERLAP, add_lineage, advance, batches, build_payload, has_valid_location, in_assignment, is_new,
    parse_assignments, read_start,
)
from app.ersharing.state import Marker
from app.ersharing.tests.fakes import make_observation
from app.ersharing.timeutil import parse_dt

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
        # On by default, as in v1.
        "subject_additional": {"sex": "f"}, "source_additional": {},
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
