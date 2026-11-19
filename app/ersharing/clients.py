"""Handler-side access to EarthRanger: Gundi credentials, retries within a run, and
the runner's classified errors around the pure EarthRanger access in er_site.py.

Every site opened here raises an IntegrationError subclass labelled with the site and
carrying `site_role`, never erclient or httpx exceptions (spec §9).
"""
import asyncio
import logging

import httpx
from erclient import ERClientException

from app import settings
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
from . import er_site
from .er_site import (  # noqa: F401  (re-exported for callers and tests)
    ASSIGNMENTS_PAGE_SIZE, OBSERVATIONS_PAGE_SIZE, AttachmentUnavailable, _results, is_rejected_login,
    safe_filename, status_of,
)

logger = logging.getLogger(__name__)

# erclient's 20 s default is too short for a page of events with notes and files from a large
# site; the v1 jobs raised it to 60 s for the same reason.
DATA_TIMEOUT_SECONDS = settings.ER_DATA_TIMEOUT_SECONDS


def build_er_client(url: str, auth: AuthenticateConfig):
    # erclient prefers the token whenever one is set, so only one kind of credentials goes in.
    use_token = auth.authentication_type == ERAuthenticationType.TOKEN and bool(auth.token)
    return er_site.build_client(
        url,
        token=auth.token.get_secret_value() if use_token else None,
        username=None if use_token else auth.username or None,
        password=None if use_token or not auth.password else auth.password.get_secret_value(),
        data_timeout=DATA_TIMEOUT_SECONDS,
    )


_ER_CALL_FAILURES = (ERClientException, httpx.HTTPError, ValueError, KeyError)

# Pauses between tries within a run (spec §9): three tries in all.
RETRY_WAITS = (1.0, 4.0)


def is_retryable(exc: Exception, *, repeat_safe: bool = True) -> bool:
    """A 429 is always worth another try: the site didn't act on the request. Timeouts,
    lost connections and server errors are retried only when sending the request twice
    can't create a second copy of anything."""
    status = status_of(exc)
    if status == 429:
        return True
    if not repeat_safe or is_rejected_login(exc):
        return False
    if isinstance(exc, httpx.TransportError) or (type(exc) is ERClientException and status is None):
        return True
    return status is not None and status >= 500


async def with_retries(call, *, repeat_safe: bool = True):
    """Await `call()`, trying again after each pause in RETRY_WAITS while the failure is retryable."""
    for wait in (*RETRY_WAITS, None):
        try:
            return await call()
        except Exception as e:
            if wait is None or not is_retryable(e, repeat_safe=repeat_safe):
                raise
            logger.info("EarthRanger call failed (%s); trying again in %s s", type(e).__name__, wait)
            await asyncio.sleep(wait)


def as_integration_error(exc: Exception, *, not_found_is_configuration: bool = True) -> IntegrationError:
    """Translate an ER call failure into the runner's classified errors. Messages
    are fixed text: str(exc) can carry the request (for a login, the password)."""
    if is_rejected_login(exc):
        return IntegrationAuthError("EarthRanger rejected the credentials", status_code=401)
    status = status_of(exc)
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


def refused_by_destination(error: IntegrationError) -> bool:
    """A 403 from the destination on one item: the token may not write that event, category
    or link. Only that item is skipped; credentials EarthRanger rejects answer 401."""
    return error.status_code == 403 and getattr(error, "site_role", None) == "destination"


def auth_config_of(configurations) -> AuthenticateConfig:
    config = find_config_for_action(configurations or [], "auth")
    if config is None or not config.data:
        raise IntegrationConfigurationError("The connection has no EarthRanger credentials; open its settings and save them.")
    return AuthenticateConfig.parse_obj(config.data)


def source_url_of(integration, auth: AuthenticateConfig) -> str:
    """The source site's URL: the connection's own, as for the earth_ranger type."""
    return integration.base_url


def translate(site: "er_site.ERSite", exc: Exception) -> IntegrationError:
    """The runner's error for a failed call on `site`, labelled with the site."""
    if isinstance(exc, IntegrationError):
        return exc
    label = site.label
    if isinstance(exc, AttachmentUnavailable):
        err = IntegrationBadResponseError(f"{label}: {exc.message}", status_code=exc.status_code)
    else:
        base = as_integration_error(exc, not_found_is_configuration=False)
        message = f"{label}: {base.message}"
        body = getattr(exc, "response_body", None)
        if base.status_code == 400 and body:
            message = f"{message} — {str(body)[:300]}"
        err = type(base)(message, status_code=base.status_code)
    err.site_role = site.role
    return err


class ERSite(er_site.ERSite):
    """An EarthRanger site whose calls are retried within the run and fail with the
    runner's classified errors."""

    def __init__(self, client, url: str, *, role: str):
        super().__init__(client, url, role=role, call_hook=self._retried)

    async def _retried(self, call, *, repeat_safe: bool = True):
        try:
            return await with_retries(call, repeat_safe=repeat_safe)
        except (*_ER_CALL_FAILURES, AttachmentUnavailable) as e:
            raise translate(self, e) from e

    def _attachment_failure(self, status: int) -> IntegrationError:
        return translate(self, super()._attachment_failure(status))


def open_site(url: str, auth: AuthenticateConfig, role: str) -> ERSite:
    try:
        return ERSite(build_er_client(url, auth), url, role=role)
    except ValueError as e:
        err = IntegrationConfigurationError(f"{role.capitalize()} site URL has no hostname; fix the site URL.")
        err.site_role = role
        raise err from e
