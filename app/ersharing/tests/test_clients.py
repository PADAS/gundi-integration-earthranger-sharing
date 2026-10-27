import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from erclient.er_errors import ERClientBadCredentials, ERClientBadRequest, ERClientException, ERClientNotFound

from app.actions.configurations import AuthenticateConfig, ERAuthenticationType
from app.ersharing.clients import ERSite, auth_config_of, build_er_client, halts_site, open_site
from app.services.errors import (
    IntegrationAuthError,
    IntegrationBadResponseError,
    IntegrationConfigurationError,
    IntegrationConnectionError,
    IntegrationRateLimitError,
)


def site(client, role="destination"):
    return ERSite(client, "https://siteb.pamdas.org", role=role)


@pytest.mark.asyncio
async def test_failures_are_translated_and_labelled():
    client = MagicMock()
    client.get_me = AsyncMock(side_effect=ERClientBadCredentials("bad", status_code=401))
    with pytest.raises(IntegrationAuthError) as info:
        await site(client).get_me()
    assert info.value.message == "Destination site siteb.pamdas.org: EarthRanger rejected the credentials"
    assert info.value.site_role == "destination"
    assert halts_site(info.value)


@pytest.mark.asyncio
async def test_rejected_write_carries_er_reason_and_does_not_halt():
    client = MagicMock()
    client.post_report = AsyncMock(
        side_effect=ERClientBadRequest("activity/events", status_code=400, response_body={"event_type": ["invalid"]})
    )
    with pytest.raises(IntegrationBadResponseError) as info:
        await site(client).post_event({"id": "e1"})
    assert "invalid" in info.value.message
    assert info.value.status_code == 400
    assert not halts_site(info.value)


@pytest.mark.asyncio
async def test_get_event_returns_none_on_404():
    client = MagicMock()
    client.get_event = AsyncMock(side_effect=ERClientNotFound("missing", status_code=404))
    assert await site(client).get_event("e1") is None


@pytest.mark.asyncio
async def test_network_failure_is_a_halting_connection_error():
    client = MagicMock()
    client.get_me = AsyncMock(side_effect=ERClientException("down"))
    with pytest.raises(IntegrationConnectionError) as info:
        await site(client, role="source").get_me()
    assert info.value.site_role == "source"
    assert info.value.message.startswith("Source site siteb.pamdas.org: ")
    assert halts_site(info.value)


@pytest.mark.asyncio
async def test_iter_events_asks_oldest_first_and_translates_mid_stream_failures():
    captured = {}

    async def get_events(**kwargs):
        captured.update(kwargs)
        yield [{"id": "a"}]
        raise ERClientException("down")

    client = MagicMock()
    client.get_events = get_events
    pages = []
    with pytest.raises(IntegrationConnectionError):
        async for page in site(client).iter_events_updated_since(datetime(2026, 1, 1, tzinfo=timezone.utc)):
            pages.append(page)
    assert pages == [[{"id": "a"}]] * 3  # read again from the last page on each of the two retries
    assert captured["sort_by"] == "updated_at"
    assert json.loads(captured["filter"]) == {"update_date": {"lower": "2026-01-01T00:00:00+00:00"}}


@pytest.mark.asyncio
async def test_post_sharing_status_posts_to_provider_endpoint():
    client = MagicMock()
    client._post = AsyncMock(return_value={})
    await site(client).post_sharing_status("das_provider", [{"a": 1}])
    client._post.assert_awaited_once_with("sensors/sharing/das_provider/status", payload=[{"a": 1}])


@pytest.mark.asyncio
async def test_list_endpoints_accept_paginated_or_plain_lists():
    client = MagicMock()
    client._get = AsyncMock(return_value={"results": [{"id": "fg1", "name": "Park"}]})
    assert await site(client).list_feature_groups() == [{"id": "fg1", "name": "Park"}]


@pytest.mark.asyncio
async def test_source_assignments_follow_every_page():
    client = MagicMock()
    client._get = AsyncMock(side_effect=[
        {"results": [{"subject": "s1", "id": "a1"}],
         "next": "https://siteb.pamdas.org/api/v1.0/subjectsources?page=2&subjects=s1%2Cs2"},
        {"results": [{"subject": "s1", "id": "a2"}], "next": None},
    ])
    assignments = await site(client, role="source").get_source_assignments(["s1", "s2"])
    assert [a["id"] for a in assignments] == ["a1", "a2"]
    first, second = (c.kwargs["params"] for c in client._get.await_args_list)
    assert first["subjects"] == "s1,s2" and "page" not in first
    assert second["page"] == "2"


@pytest.mark.asyncio
async def test_observations_ask_er_to_leave_out_excluded_points():
    client = build_er_client("https://siteb.pamdas.org", AuthenticateConfig(token="t"))
    client._get = AsyncMock(return_value={"results": [{"id": "o1"}], "next": None})
    assert await site(client, role="source").get_observations("s1", datetime(2026, 1, 1, tzinfo=timezone.utc)) == [
        {"id": "o1"}]
    assert client._get.await_args.kwargs["params"]["filter"] == "0"


def test_build_er_client_targets_site_api_root():
    client = build_er_client("https://SiteB.pamdas.org/", AuthenticateConfig(token="t"))
    assert client._api_root() == "https://siteb.pamdas.org/api/v1.0"


def test_open_site_rejects_url_without_host():
    with pytest.raises(IntegrationConfigurationError) as info:
        open_site("https://", AuthenticateConfig(token="t"), role="destination")
    assert info.value.site_role == "destination"


def test_auth_config_of_reads_the_auth_action_config():
    configurations = [
        SimpleNamespace(action=SimpleNamespace(value="sync_events"), data={}),
        SimpleNamespace(action=SimpleNamespace(value="auth"), data={"token": "abc"}),
    ]
    assert auth_config_of(configurations).token.get_secret_value() == "abc"
    with pytest.raises(IntegrationConfigurationError):
        auth_config_of([])


def test_build_er_client_uses_only_the_selected_authentication_type():
    stale = AuthenticateConfig(authentication_type=ERAuthenticationType.USERNAME_PASSWORD,
                               token="old", username="u", password="p")
    client = build_er_client("https://siteb.pamdas.org", stale)
    assert client.auth is None and (client.username, client.password) == ("u", "p")
    token = build_er_client("https://siteb.pamdas.org", AuthenticateConfig(token="t", username="u", password="p"))
    assert token.auth["access_token"] == "t" and (token.username, token.password) == (None, None)


def test_build_er_client_uses_username_and_password_when_no_token_is_stored():
    client = build_er_client("https://siteb.pamdas.org", AuthenticateConfig(username="u", password="p"))
    assert client.auth is None and (client.username, client.password) == ("u", "p")


def er_status_error(status):
    return ERClientException(f"status {status}", status_code=status)


@pytest.mark.asyncio
async def test_can_read_is_true_on_200_and_passes_extra_params():
    client = MagicMock()
    client._get = AsyncMock(return_value={"results": []})
    assert await site(client, role="source").can_read("observations", {"since": "x", "use_cursor": "true"}) is True
    client._get.assert_awaited_once_with("observations", params={"page_size": 1, "since": "x", "use_cursor": "true"})


@pytest.mark.asyncio
async def test_can_read_is_false_only_on_403():
    client = MagicMock()
    client._get = AsyncMock(side_effect=er_status_error(403))
    assert await site(client, role="source").can_read("subjects") is False


@pytest.mark.asyncio
async def test_can_read_raises_on_401_and_404():
    client = MagicMock()
    client._get = AsyncMock(side_effect=er_status_error(401))
    with pytest.raises(IntegrationAuthError):
        await site(client, role="source").can_read("subjects")
    client._get = AsyncMock(side_effect=er_status_error(404))
    with pytest.raises(IntegrationBadResponseError) as info:
        await site(client, role="source").can_read("subjects")
    assert info.value.status_code == 404


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks, chunk_size=1024 * 1024):
        self.chunks, self.chunk_size, self.sent = chunks, chunk_size, 0

    async def __aiter__(self):
        for _ in range(self.chunks):
            self.sent += 1
            yield b"x" * self.chunk_size


def streaming_site(handler):
    client = build_er_client("https://siteb.pamdas.org", AuthenticateConfig(token="t"))
    client._http_session = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.auth_headers = AsyncMock(return_value={"Authorization": "Bearer t"})
    return site(client, role="source")


@pytest.mark.asyncio
async def test_download_streams_to_a_temp_file_that_is_removed_on_exit():
    src = streaming_site(lambda request: httpx.Response(200, content=b"hello"))
    async with src.download_file("https://siteb.pamdas.org/f", "a.txt", 100) as path:
        assert os.path.basename(path) == "a.txt" and open(path, "rb").read() == b"hello"
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_download_stops_reading_once_the_file_is_over_the_limit():
    stream = CountingStream(chunks=1000)
    src = streaming_site(lambda request: httpx.Response(200, stream=stream))
    async with src.download_file("https://siteb.pamdas.org/f", "big.bin", 3 * 1024 * 1024) as path:
        assert path is None
    assert stream.sent <= 5


@pytest.mark.asyncio
async def test_download_skips_early_when_content_length_is_over_the_limit():
    stream = CountingStream(chunks=1000)
    src = streaming_site(lambda request: httpx.Response(
        200, headers={"content-length": str(10 * 1024 * 1024 * 1024)}, stream=stream))
    async with src.download_file("https://siteb.pamdas.org/f", "big.bin", 1024) as path:
        assert path is None
    assert stream.sent == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("url, sent", [("https://siteb.pamdas.org/f", True), ("http://siteb.pamdas.org/f", False)])
async def test_the_token_is_only_sent_to_the_sites_own_https_urls(url, sent):
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, content=b"hi")
    src = streaming_site(handler)
    async with src.download_file(url, "a.txt", 100):
        pass
    assert seen == (["Bearer t"] if sent else [None])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 410, 400, 302])
async def test_a_failed_download_is_a_non_halting_error_for_that_file(status):
    src = streaming_site(lambda request: httpx.Response(status, content=b"nope"))
    with pytest.raises(IntegrationBadResponseError) as info:
        async with src.download_file("https://siteb.pamdas.org/f", "a.txt", 100):
            pass
    assert not halts_site(info.value) and info.value.site_role == "source"
    assert info.value.status_code == status and "status" not in info.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("status, error", [
    (401, IntegrationAuthError), (429, IntegrationRateLimitError), (409, IntegrationRateLimitError),
    (500, IntegrationBadResponseError), (502, IntegrationConnectionError), (503, IntegrationConnectionError),
    (504, IntegrationConnectionError),
])
async def test_a_download_failing_for_the_whole_site_halts_it(status, error):
    src = streaming_site(lambda request: httpx.Response(status))
    with pytest.raises(error) as info:
        async with src.download_file("https://siteb.pamdas.org/f", "a.txt", 100):
            pass
    assert info.value.status_code == status and halts_site(info.value)
    assert info.value.site_role == "source" and info.value.message.startswith("Source site siteb.pamdas.org: ")


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", [".", "..", "", "../..", "dir/..", "a\\.."])
async def test_download_names_an_odd_filename_attachment(filename):
    src = streaming_site(lambda request: httpx.Response(200, content=b"hello"))
    async with src.download_file("https://siteb.pamdas.org/f", filename, 100) as path:
        assert os.path.basename(path) == "attachment" and open(path, "rb").read() == b"hello"


@pytest.mark.asyncio
async def test_download_sends_credentials_only_to_the_sites_own_host():
    seen = {}

    def handler(request):
        seen[request.url.host] = request.headers.get("Authorization")
        return httpx.Response(200, content=b"x")

    src = streaming_site(handler)
    for url in ("https://siteb.pamdas.org/f", "https://storage.example.com/f", "activity/event/e1/file/f1"):
        async with src.download_file(url, "a.txt", 100):
            pass
    assert seen == {"siteb.pamdas.org": "Bearer t", "storage.example.com": None}


@pytest.mark.asyncio
async def test_download_connection_failure_halts_the_site():
    def refuse(request):
        raise httpx.ConnectError("down")

    with pytest.raises(IntegrationConnectionError):
        async with streaming_site(refuse).download_file("https://siteb.pamdas.org/f", "a.txt", 100):
            pass


@pytest.mark.asyncio
async def test_a_read_that_times_out_is_retried_within_the_run():
    client = MagicMock()
    client.get_me = AsyncMock(side_effect=[httpx.ReadTimeout("slow"), {"username": "svc"}])
    assert await site(client).get_me() == {"username": "svc"}
    assert client.get_me.await_count == 2


@pytest.mark.asyncio
async def test_a_server_error_is_retried_then_reported():
    client = MagicMock()
    client.patch_report = AsyncMock(side_effect=ERClientException("boom", status_code=500))
    with pytest.raises(IntegrationBadResponseError):
        await site(client).patch_event("e1", {})
    assert client.patch_report.await_count == 3


@pytest.mark.asyncio
async def test_a_create_that_times_out_is_not_sent_twice():
    client = MagicMock()
    client.post_event_note = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(IntegrationConnectionError):
        await site(client).post_note("e1", "hello")
    assert client.post_event_note.await_count == 1


@pytest.mark.asyncio
async def test_a_rate_limited_create_is_retried():
    client = MagicMock()
    client.post_event_note = AsyncMock(side_effect=[ERClientException("slow down", status_code=429), {"id": "n1"}])
    assert await site(client).post_note("e1", "hello") == {"id": "n1"}


@pytest.mark.asyncio
async def test_a_rejected_item_or_bad_credentials_are_not_retried():
    client = MagicMock()
    client.patch_report = AsyncMock(side_effect=ERClientBadRequest("x", status_code=400, response_body="bad"))
    client.get_me = AsyncMock(side_effect=ERClientBadCredentials("bad", status_code=401))
    with pytest.raises(IntegrationBadResponseError):
        await site(client).patch_event("e1", {})
    with pytest.raises(IntegrationAuthError):
        await site(client).get_me()
    assert client.patch_report.await_count == 1 and client.get_me.await_count == 1


@pytest.mark.asyncio
async def test_reading_events_resumes_after_the_last_page_on_a_retry():
    filters = []

    async def get_events(**kwargs):
        filters.append(json.loads(kwargs["filter"])["update_date"]["lower"])
        if len(filters) == 1:
            yield [{"id": "a", "updated_at": "2026-01-02T00:00:00+00:00"}]
            raise httpx.ReadTimeout("slow")
        yield [{"id": "b", "updated_at": "2026-01-03T00:00:00+00:00"}]

    client = MagicMock()
    client.get_events = get_events
    pages = [page async for page in site(client).iter_events_updated_since(datetime(2026, 1, 1, tzinfo=timezone.utc))]
    assert [p[0]["id"] for p in pages] == ["a", "b"]
    assert filters == ["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"]


@pytest.mark.asyncio
async def test_reading_observations_is_retried_from_the_start():
    calls = []

    async def get_observations(**kwargs):
        calls.append(kwargs)
        yield {"id": "o1"}
        if len(calls) == 1:
            raise httpx.ConnectError("lost")
        yield {"id": "o2"}

    client = MagicMock()
    client.get_observations = get_observations
    observations = await site(client, role="source").get_observations("s1", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert [o["id"] for o in observations] == ["o1", "o2"]
    assert len(calls) == 2
