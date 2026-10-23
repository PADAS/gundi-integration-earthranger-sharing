from types import SimpleNamespace

import pytest

from app.ersharing.destinations import resolve_destinations

SOURCE = "https://sitea.pamdas.org"


def dest(id, base_url, type_value="earth_ranger", name=None):
    return SimpleNamespace(id=id, name=name or id, base_url=base_url, type=SimpleNamespace(value=type_value))


def details(base_url, auth_data, enabled=True):
    configurations = [] if auth_data is None else [
        SimpleNamespace(action=SimpleNamespace(value="auth"), data=auth_data)
    ]
    return SimpleNamespace(base_url=base_url, configurations=configurations, enabled=enabled)


class FakeGundi:
    def __init__(self, destinations, details_by_id):
        self.connection = SimpleNamespace(destinations=destinations)
        self.details_by_id = details_by_id

    async def get_connection_details(self, connection_id):
        return self.connection

    async def get_integration_details(self, integration_id):
        found = self.details_by_id[str(integration_id)]
        if isinstance(found, Exception):
            raise found
        return found


@pytest.mark.asyncio
async def test_resolves_earthranger_destinations_with_their_credentials():
    gundi = FakeGundi([dest("d1", "https://siteb.pamdas.org", name="Site B")],
                      {"d1": details("https://siteb.pamdas.org", {"token": "t"})})
    destinations, skipped = await resolve_destinations("c1", SOURCE, gundi=gundi)
    assert skipped == []
    assert [(d.id, d.name, d.url) for d in destinations] == [("d1", "Site B", "https://siteb.pamdas.org")]
    assert destinations[0].auth.token.get_secret_value() == "t"


@pytest.mark.asyncio
async def test_skips_unusable_destinations_with_reasons():
    gundi = FakeGundi(
        [
            dest("smart", "https://smart.example.org", type_value="smart_connect"),
            dest("self", "https://SiteA.pamdas.org/"),
            dest("nocreds", "https://sitec.pamdas.org"),
            dest("hidden", "https://sited.pamdas.org"),
        ],
        {
            "self": details("https://SiteA.pamdas.org/", {"token": "t"}),
            "nocreds": details("https://sitec.pamdas.org", None),
            "hidden": RuntimeError("403 from Gundi"),
        },
    )
    destinations, skipped = await resolve_destinations("c1", SOURCE, gundi=gundi)
    assert destinations == []
    assert {s.id: s.reason for s in skipped} == {
        "smart": "not an EarthRanger destination",
        "self": "same site as the source",
        "nocreds": "has no EarthRanger credentials",
        "hidden": "its credentials could not be read from Gundi",
    }


@pytest.mark.asyncio
async def test_skips_destinations_turned_off_in_gundi():
    gundi = FakeGundi([dest("off", "https://siteb.pamdas.org"), dest("on", "https://sitec.pamdas.org")],
                      {"off": details("https://siteb.pamdas.org", {"token": "t"}, enabled=False),
                       "on": details("https://sitec.pamdas.org", {"token": "t"})})
    destinations, skipped = await resolve_destinations("c1", SOURCE, gundi=gundi)
    assert [d.id for d in destinations] == ["on"]
    assert [(s.id, s.reason) for s in skipped] == [("off", "it's turned off in Gundi")]


@pytest.mark.asyncio
async def test_bad_destination_data_skips_only_that_destination():
    gundi = FakeGundi(
        [
            dest("badauth", "https://siteb.pamdas.org"),
            dest("badurl", "https://"),
            dest("nourl", None),
            dest("selfurl", "https://sitex.pamdas.org"),
            dest("good", "https://sitec.pamdas.org"),
        ],
        {
            "badauth": details("https://siteb.pamdas.org", {"token": "t", "authentication_type": "kerberos"}),
            "badurl": details(None, {"token": "t"}),
            "nourl": details(None, {"token": "t"}),
            "selfurl": details("https://sitea.pamdas.org", {"token": "t"}),
            "good": details("https://sitec.pamdas.org", {"token": "t"}),
        },
    )
    destinations, skipped = await resolve_destinations("c1", SOURCE, gundi=gundi)
    assert [d.id for d in destinations] == ["good"]
    assert {s.id: s.reason for s in skipped} == {
        "badauth": "its EarthRanger credentials in Gundi couldn't be read",
        "badurl": "its site URL in Gundi isn't a valid web address",
        "nourl": "it has no site URL in Gundi",
        "selfurl": "same site as the source",
    }
