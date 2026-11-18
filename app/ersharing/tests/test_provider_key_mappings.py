"""Provider keys from route field mappings (PR decision 19)."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.actions.configurations import SyncObservationsConfig
from app.ersharing.destinations import provider_keys_from, resolve_destinations
from app.ersharing.modes import observation_settings
from app.ersharing.reporting import DestinationReport
from app.ersharing.state import ShareState
from app.ersharing.sync_observations import _share_subject
from app.ersharing.tests.fakes import FakeSite, InMemoryStateManager, make_observation
from app.ersharing.tests.test_destinations import FakeGundi, dest, details

SOURCE = "https://sitea.pamdas.org"
CONNECTION = "c1c1c1c1-0000-0000-0000-000000000001"
D1 = "d1d1d1d1-0000-0000-0000-000000000001"
D2 = "d2d2d2d2-0000-0000-0000-000000000002"


def mappings(**by_destination):
    return {CONNECTION: {"obv": by_destination}}


def test_a_fixed_provider_key_per_destination_is_read():
    fm = mappings(**{D1: {"destination_field": "provider_key", "default": "kws_provider"},
                     D2: {"destination_field": "provider_key", "provider_field": "x", "map": {"a": "b"}}})
    assert provider_keys_from(fm, CONNECTION) == {D1: "kws_provider"}
    # Other connections, stream types and target fields don't apply.
    assert provider_keys_from({"other": fm[CONNECTION]}, CONNECTION) == {}
    assert provider_keys_from({CONNECTION: {"ev": fm[CONNECTION]["obv"]}}, CONNECTION) == {}
    assert provider_keys_from(mappings(**{D1: {"destination_field": "subject_name", "default": "x"}}), CONNECTION) == {}


class RoutedGundi(FakeGundi):
    def __init__(self, *args, routes):
        super().__init__(*args)
        self.connection.routing_rules = [SimpleNamespace(id=r) for r in routes]
        self.routes = routes

    async def get_route_details(self, route_id):
        found = self.routes[route_id]
        if isinstance(found, Exception):
            raise found
        return SimpleNamespace(configuration=SimpleNamespace(data={"field_mappings": found}))


@pytest.mark.asyncio
async def test_destinations_carry_the_routes_provider_key():
    gundi = RoutedGundi(
        [dest(D1, "https://siteb.pamdas.org"), dest(D2, "https://sitec.pamdas.org")],
        {D1: details("https://siteb.pamdas.org", {"token": "t"}), D2: details("https://sitec.pamdas.org", {"token": "t"})},
        routes={"r1": mappings(**{D1: {"destination_field": "provider_key", "default": "kws_provider"}}),
                "r2": RuntimeError("route gone")},
    )
    destinations, skipped = await resolve_destinations(CONNECTION, SOURCE, gundi=gundi)
    assert {d.id: d.provider_key for d in destinations} == {D1: "kws_provider", D2: None}
    assert not skipped


@pytest.mark.asyncio
async def test_points_go_to_each_destinations_own_provider_key():
    state = ShareState("i1", manager=InMemoryStateManager())
    source = FakeSite(SOURCE, role="source")
    source.observations = {"s1": [make_observation("src-1", "2026-10-02T11:00:00Z")]}
    site_b, site_c = FakeSite("https://siteb.pamdas.org"), FakeSite("https://sitec.pamdas.org")
    assignments = {"s1": [SimpleNamespace(source_id="src-1", lower=datetime(2026, 1, 1, tzinfo=timezone.utc), upper=None)]}
    share = observation_settings(SOURCE, SyncObservationsConfig())
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    await _share_subject(source, {D1: site_b, D2: site_c}, {D1: DestinationReport("B", "Tracks"),
                         D2: DestinationReport("C", "Tracks")}, {}, state, share, None, assignments,
                         {"id": "s1", "name": "Leo"}, "site:sitea.pamdas.org#user:svc", now,
                         provider_keys={D1: "kws_provider"})
    assert [key for key, _ in site_b.sharing_posts] == ["kws_provider"]
    assert [key for key, _ in site_c.sharing_posts] == ["das_provider"]
