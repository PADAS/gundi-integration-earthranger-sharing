"""A late point skipped by a 5-minute read is picked up by the next hourly re-read (T8)."""
from datetime import datetime, timedelta, timezone

from app.ersharing.observations import advance, is_new, note_read, SHORT_OVERLAP

T0 = datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)


def point(recorded, created):
    return {"recorded_at": recorded.isoformat(), "created_at": created.isoformat()}


def test_a_late_point_posted_between_hourly_rereads_is_sent_by_the_next_one():
    marker = advance(None, [point(T0, T0)], T0)
    marker = note_read(marker, T0, T0 - timedelta(hours=12), full_reread=True)
    late = point(T0 - timedelta(hours=3), T0 + timedelta(minutes=2))
    fresh = point(T0 + timedelta(minutes=3), T0 + timedelta(minutes=3))
    # The 5-minute run at 17:05 sees only the fresh point and moves created_since past the late one.
    run_at = T0 + timedelta(minutes=5)
    start = marker.since - SHORT_OVERLAP
    marker = note_read(advance(marker, [fresh], run_at), run_at, start, full_reread=False)
    assert not is_new(late, marker)  # a short read still skips it
    assert is_new(late, marker, full_reread=True)  # the hourly re-read sends it
    assert not is_new(fresh, marker, full_reread=True)  # the fresh one isn't sent twice


def test_a_point_a_short_read_did_see_isnt_sent_again_by_the_reread():
    marker = advance(None, [point(T0, T0)], T0)
    seen = point(T0 - timedelta(minutes=2), T0 + timedelta(minutes=1))
    run_at = T0 + timedelta(minutes=5)
    marker = note_read(advance(marker, [seen], run_at), run_at, marker.since - SHORT_OVERLAP, full_reread=False)
    assert not is_new(seen, marker, full_reread=True)


def test_the_hourly_reread_forgets_the_short_reads():
    marker = advance(None, [point(T0, T0)], T0)
    marker = note_read(marker, T0, T0, full_reread=False)
    assert marker.short_reads
    assert note_read(marker, T0, T0, full_reread=True).short_reads == []
