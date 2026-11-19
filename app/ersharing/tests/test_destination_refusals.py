"""A 403 from the destination on one event or incident link skips only that item (E40, E41)."""
import pytest

from app.ersharing import sync_events
from app.ersharing.tests.fakes import make_event
from app.ersharing.tests.test_sync_events import incidents_window_now, link, setup, sync  # noqa: F401
from app.services.errors import IntegrationAuthError


def refused(role="destination"):
    err = IntegrationAuthError("Destination site siteb.pamdas.org: EarthRanger denied access with these credentials",
                               status_code=403)
    err.site_role = role
    return err


@pytest.mark.asyncio
async def test_an_event_the_destination_refuses_doesnt_hold_back_the_others():
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"), make_event("e2", "2026-10-02T00:00:00Z"))
    dest.failures["post_event"] = lambda event: refused() if event["id"] == "e1" else None
    report = await sync(run, dest)
    assert "e2" in dest.events and "e1" not in dest.events
    assert sync_events.REJECTED in report.errors
    assert set(await state.get_rejected_events("d1")) == {"e1"} and await state.get_halt("d1") == {}


@pytest.mark.asyncio
async def test_a_refused_unlink_is_warned_about_once_and_not_repeated(incidents_window_now):
    source, dest, state, run = setup(make_event("e1", "2026-10-01T00:00:00Z"),
                                     make_event("inc", "2026-10-02T00:00:00Z", is_collection=True))
    await sync(run, dest)
    link(source.events["e1"], "inc")
    await sync(run, dest)
    dest.failures["remove_from_incident"] = refused()
    source.events["e1"]["is_contained_in"] = []
    report = await sync(run, dest)
    assert any("doesn't allow it" in e for e in report.errors)
    assert (await state.get_event_record("d1", "e1")).incidents == []
    await sync(run, dest)
    assert len(dest.calls_to("remove_from_incident")) == 1


@pytest.mark.asyncio
async def test_a_refused_link_doesnt_stop_the_destination(incidents_window_now):
    event = make_event("e1", "2026-10-01T00:00:00Z")
    link(event, "inc")
    source, dest, state, run = setup(event, make_event("inc", "2026-10-02T00:00:00Z", is_collection=True),
                                     make_event("e3", "2026-10-03T00:00:00Z"))
    dest.failures["add_to_incident"] = refused()
    await sync(run, dest)
    assert "e3" in dest.events
