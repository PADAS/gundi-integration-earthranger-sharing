"""EarthRanger access for both sides of a share.

Every ERSite call raises an IntegrationError subclass labelled with the site and
carrying `site_role`, never erclient or httpx exceptions (spec §9).
"""
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from erclient import AsyncERClient, ERClientException, VERSION_2_0
from erclient.er_errors import ERClientBadRequest

from app.actions.configurations import AuthenticateConfig, ERAuthenticationType
from app.services.errors import (
    IntegrationAuthError,
    IntegrationBadResponseError,
    IntegrationConfigurationError,
    IntegrationConnectionError,
    IntegrationError,
    IntegrationRateLimitError,
)
from app.services.utils import find_config_for_action
from .naming import site_host, site_root

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
ASSIGNMENTS_PAGE_SIZE = 1000


def build_er_client(url: str, auth: AuthenticateConfig) -> AsyncERClient:
    root = site_root(url)
    # erclient prefers the token whenever one is set, so only one kind of credentials goes in.
    use_token = auth.authentication_type == ERAuthenticationType.TOKEN and bool(auth.token)
    client = AsyncERClient(
        service_root=f"{root}/api/v1.0",
        username=None if use_token else auth.username or None,
        password=None if use_token or not auth.password else auth.password.get_secret_value(),
        token=auth.token.get_secret_value() if use_token else None,
        token_url=f"{root}/oauth2/token",
        client_id="das_web_client",
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
    )
    # erclient raises on the token endpoint's 400 instead of returning False, which
    # would skip its fallback login when a refresh token goes stale mid-run.
    original_refresh = client.refresh_token

    async def refresh_or_fall_back_to_login(*args, **kwargs):
        try:
            return await original_refresh(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                return False
            raise

    client.refresh_token = refresh_or_fall_back_to_login
    return client


def _is_token_endpoint_url(url) -> bool:
    return "/oauth2/token" in str(url)


def _status_of(exc: Exception):
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _is_rejected_login(exc) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return _is_token_endpoint_url(exc.request.url) and exc.response.status_code in (400, 401)
    return isinstance(exc, ERClientBadRequest) and bool(exc.args) and _is_token_endpoint_url(exc.args[0])


_ER_CALL_FAILURES = (ERClientException, httpx.HTTPError, ValueError, KeyError)


def as_integration_error(exc: Exception, *, not_found_is_configuration: bool = True) -> IntegrationError:
    """Translate an ER call failure into the runner's classified errors. Messages
    are fixed text: str(exc) can carry the request (for a login, the password)."""
    if _is_rejected_login(exc):
        return IntegrationAuthError("EarthRanger rejected the credentials", status_code=401)
    status = _status_of(exc)
    if isinstance(exc, httpx.TransportError) or (type(exc) is ERClientException and status is None):
        return IntegrationConnectionError("EarthRanger could not be reached")
    if status is None:
        return IntegrationBadResponseError("EarthRanger returned an unexpected response")
    if status == 401:
        return IntegrationAuthError("EarthRanger rejected the credentials", status_code=status)
    if status == 403:
        return IntegrationAuthError("EarthRanger denied access with these credentials", status_code=status)
    if status == 404 and not_found_is_configuration:
        return IntegrationConfigurationError(
            "EarthRanger does not expose the API this action needs; check the site URL and EarthRanger version."
        )
    if status in (429, 409):
        return IntegrationRateLimitError("EarthRanger rate-limited the request", status_code=status)
    if status in (502, 503, 504):
        return IntegrationConnectionError("EarthRanger could not be reached", status_code=status)
    if 300 <= status < 400:
        return IntegrationConfigurationError("Site URL redirects elsewhere; use the site's https URL.")
    return IntegrationBadResponseError("EarthRanger returned an unexpected response", status_code=status)


def halts_site(error: IntegrationError) -> bool:
    """True when nothing more can be done on that site in this run (credentials,
    reachability, rate limit, misconfiguration, server error). False for a
    rejected item, which is skipped and reported."""
    if isinstance(error, (IntegrationAuthError, IntegrationConnectionError,
                          IntegrationRateLimitError, IntegrationConfigurationError)):
        return True
    return (error.status_code or 0) >= 500


def auth_config_of(configurations) -> AuthenticateConfig:
    config = find_config_for_action(configurations or [], "auth")
    if config is None or not config.data:
        raise IntegrationConfigurationError("The connection has no EarthRanger credentials; open its settings and save them.")
    return AuthenticateConfig.parse_obj(config.data)


def source_url_of(integration, auth: AuthenticateConfig) -> str:
    """The source site's URL: the auth form's base URL, else the integration's own."""
    return auth.base_url or integration.base_url


def _results(data) -> list:
    if isinstance(data, dict):
        return data.get("results", [])
    return data or []


def safe_filename(filename: str) -> str:
    name = os.path.basename((filename or "").replace("\\", "/").replace("\0", "")).strip()
    return "attachment" if name in ("", ".", "..") else name


class ERSite:
    def __init__(self, client: AsyncERClient, url: str, *, role: str):
        self._client = client
        self.url = site_root(url)
        self.service_root = f"{self.url}/api/v1.0"
        self.role = role
        self.label = f"{'Source' if role == 'source' else 'Destination'} site {site_host(url)}"

    async def __aenter__(self) -> "ERSite":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        await self._client.__aexit__(*exc_info)

    def _translate(self, exc: Exception) -> IntegrationError:
        err = as_integration_error(exc, not_found_is_configuration=False)
        message = f"{self.label}: {err.message}"
        body = getattr(exc, "response_body", None)
        if err.status_code == 400 and body:
            message = f"{message} — {str(body)[:300]}"
        labelled = type(err)(message, status_code=err.status_code)
        labelled.site_role = self.role
        return labelled

    async def _call(self, awaitable, *, none_on_404: bool = False):
        try:
            return await awaitable
        except _ER_CALL_FAILURES as e:
            if none_on_404 and _status_of(e) == 404:
                return None
            raise self._translate(e) from e

    async def get_me(self) -> dict:
        return await self._call(self._client.get_me())

    async def can_read(self, path: str, params: Optional[dict] = None) -> bool:
        """Read one item from an ER list endpoint. ER grants subject and track access
        through subject-group permission sets, so user/me's model permissions can't
        answer this; only a 403 means the token can't read it."""
        try:
            await self._call(self._client._get(path, params={"page_size": 1, **(params or {})}))
        except IntegrationAuthError as e:
            if e.status_code == 403:
                return False
            raise
        return True

    async def iter_events_updated_since(
        self, since: datetime, *, page_size: int = 100, with_notes_and_files: bool = True
    ) -> AsyncIterator[List[dict]]:
        events = self._client.get_events(
            filter=json.dumps({"update_date": {"lower": since.isoformat()}}),
            sort_by="updated_at",
            include_details=True,
            include_notes=with_notes_and_files,
            include_files=with_notes_and_files,
            include_updates=False,
            include_related_events=False,
            page_size=page_size,
            batch_size=page_size,
        )
        try:
            async for page in events:
                yield page
        except _ER_CALL_FAILURES as e:
            raise self._translate(e) from e

    async def get_event(self, event_id: str, *, with_notes_and_files: bool = False) -> Optional[dict]:
        return await self._call(
            self._client.get_event(
                event_id=event_id, include_details=True,
                include_notes=with_notes_and_files, include_files=with_notes_and_files,
            ),
            none_on_404=True,
        )

    async def post_event(self, event: dict) -> dict:
        return await self._call(self._client.post_report(event))

    async def patch_event(self, event_id: str, data: dict) -> dict:
        return await self._call(self._client.patch_report(event_id, data))

    async def delete_event(self, event_id: str):
        return await self._call(self._client.delete_event(event_id))

    async def post_note(self, event_id: str, text: str) -> dict:
        created = await self._call(self._client.post_event_note(event_id, text))
        return created[0] if isinstance(created, list) else created

    async def patch_note(self, event_id: str, note_id: str, text: str):
        return await self._call(self._client._patch(f"activity/event/{event_id}/note/{note_id}", payload={"text": text}))

    async def delete_note(self, event_id: str, note_id: str):
        return await self._call(self._client.delete_event_note(event_id, note_id))

    def _attachment_failure(self, status: int) -> IntegrationError:
        """A file the site won't hand over is skipped on its own; site-wide trouble
        (credentials, rate limit, server errors) halts the site like any other call."""
        if status == 403:
            message = "the source site didn't allow downloading it"
        elif status in (404, 410):
            message = "the attachment wasn't found"
        elif 300 <= status < 500 and status not in (401, 409, 429):
            message = "the source site wouldn't send it"
        else:
            return self._translate(ERClientException(status_code=status))
        err = IntegrationBadResponseError(f"{self.label}: {message}", status_code=status)
        err.site_role = self.role
        return err

    def _is_own_url(self, url: str) -> bool:
        try:
            https = urlparse(url if "://" in url else f"https://{url}").scheme == "https"
            return https and site_host(url) == site_host(self.url)
        except ValueError:
            return False

    async def _stream_to(self, url: str, path: str, max_bytes: int) -> bool:
        """Write the file to `path`; False (and stop reading) once it's known to exceed `max_bytes`."""
        client = self._client
        if not url.startswith("http"):
            url = client._er_url(url)
        headers = {"User-Agent": client.user_agent}
        if self._is_own_url(url):
            headers.update(await client.auth_headers())
        async with client._http_session.stream("GET", url, headers=headers) as response:
            if not response.is_success:
                raise self._attachment_failure(response.status_code)
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                return False
            received = 0
            with open(path, "wb") as f:
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        return False
                    f.write(chunk)
        return True

    @asynccontextmanager
    async def download_file(self, url: str, filename: str, max_bytes: int):
        """Yield the path of a temp copy of the file named `filename`, or None when it is
        over `max_bytes` (never read past that). The copy is deleted on exit."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, safe_filename(filename))
            try:
                fits = await self._stream_to(url, path, max_bytes)
            except _ER_CALL_FAILURES as e:
                raise self._translate(e) from e
            yield path if fits else None

    async def post_file(self, event_id: str, path: str, comment: str = "") -> dict:
        return await self._call(self._client.post_event_file(event_id, filepath=path, comment=comment))

    async def delete_file(self, event_id: str, file_id: str):
        return await self._call(self._client.delete_event_file(event_id, file_id))

    async def add_to_incident(self, event_id: str, incident_id: str):
        return await self._call(self._client.add_event_to_incident(event_id, incident_id))

    async def remove_from_incident(self, event_id: str, incident_id: str):
        return await self._call(self._client.remove_event_from_incident(event_id, incident_id))

    async def get_event_categories(self) -> List[dict]:
        return _results(await self._call(self._client.get_event_categories(include_inactive=True)))

    async def post_event_category(self, data: dict):
        return await self._call(self._client.post_event_category(data))

    async def patch_event_category(self, data: dict):
        return await self._call(self._client.patch_event_category(data))

    async def list_event_types_v2(self) -> List[dict]:
        return _results(await self._call(self._client.get_event_types(include_schema=True, version=VERSION_2_0)))

    async def get_event_type_v2(self, value: str) -> Optional[dict]:
        return await self._call(
            self._client.get_event_type(value, version=VERSION_2_0, include_schema=True), none_on_404=True
        )

    async def post_event_type_v2(self, data: dict):
        return await self._call(self._client.post_event_type(data, version=VERSION_2_0))

    async def patch_event_type_v2(self, data: dict):
        return await self._call(self._client.patch_event_type(data, version=VERSION_2_0))

    async def list_subject_groups(self) -> List[dict]:
        return _results(await self._call(
            self._client.get_subjectgroups(include_inactive=False, include_hidden=True, flat=True)
        ))

    async def get_source_assignments(self, subject_ids: List[str]) -> List[dict]:
        params = {"subjects": ",".join(subject_ids), "page_size": ASSIGNMENTS_PAGE_SIZE}
        out = []
        while True:
            data = await self._call(self._client._get("subjectsources", params=params))
            out.extend(_results(data))
            next_page = parse_qs(urlparse(data.get("next") or "").query).get("page") if isinstance(data, dict) else None
            if not next_page:
                return out
            params = {**params, "page": next_page[0]}

    async def get_subject_sources(self, subject_id: str) -> List[dict]:
        return _results(await self._call(self._client.get_subject_sources(subject_id)))

    async def get_observations(self, subject_id: str, start: datetime) -> List[dict]:
        out = []
        # A string: erclient turns a falsy 0 into filter=null, which includes points flagged as excluded.
        observations = self._client.get_observations(
            subject_id=subject_id, start=start, filter_flag="0", include_details=True
        )
        try:
            async for observation in observations:
                out.append(observation)
        except _ER_CALL_FAILURES as e:
            raise self._translate(e) from e
        return out

    async def list_feature_groups(self) -> List[dict]:
        return _results(await self._call(self._client._get("spatialfeaturegroup", params={})))

    async def get_feature_group(self, feature_group_id: str) -> dict:
        return await self._call(self._client.get_feature_group(feature_group_id))

    async def post_sharing_status(self, provider_key: str, payloads: List[dict]):
        return await self._call(self._client._post(f"sensors/sharing/{provider_key}/status", payload=payloads))


def open_site(url: str, auth: AuthenticateConfig, role: str) -> ERSite:
    try:
        return ERSite(build_er_client(url, auth), url, role=role)
    except ValueError as e:
        err = IntegrationConfigurationError(f"{role.capitalize()} site URL has no hostname; fix the site URL.")
        err.site_role = role
        raise err from e
