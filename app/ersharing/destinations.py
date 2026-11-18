"""The connection's EarthRanger destinations and their stored credentials (spec §5)."""
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from gundi_client_v2 import GundiClient
from pydantic import ValidationError

from app.actions.configurations import AuthenticateConfig, ERAuthenticationType
from app.services.utils import find_config_for_action
from .naming import same_site, site_host

logger = logging.getLogger(__name__)

EARTHRANGER_TYPE = "earth_ranger"


@dataclass(frozen=True)
class Destination:
    id: str
    name: str
    url: str
    auth: AuthenticateConfig
    # The route's field mapping for this destination's track points, as the routing service
    # applies it to other connections: kept from v1 so points land on the same devices.
    provider_key: Optional[str] = None


@dataclass(frozen=True)
class SkippedDestination:
    id: str
    name: str
    reason: str


def _secret(value) -> str:
    if value is None:
        return ""
    return (value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)).strip()


def _missing_credentials(credentials: AuthenticateConfig) -> str:
    """Why stored credentials can't be used as they are, or "" when they can. An
    empty token would only be turned down by EarthRanger as wrong, which points
    admins at the wrong fix."""
    if credentials.authentication_type == ERAuthenticationType.USERNAME_PASSWORD:
        if not (credentials.username or "").strip() or not _secret(credentials.password):
            return "it has no username and password in Gundi"
        return ""
    if not _secret(credentials.token):
        return "it has no token in Gundi"
    return ""


PROVIDER_KEY_FIELD = "provider_key"
OBSERVATIONS_STREAM = "obv"


def provider_keys_from(field_mappings: dict, connection_id: str) -> Dict[str, str]:
    """Provider key per destination from a route's field mappings, shaped
    {provider: {stream type: {destination: rule}}}. Only a fixed key (the rule's
    `default`) applies here: these points carry no provider field to map from."""
    by_destination = ((field_mappings or {}).get(str(connection_id)) or {}).get(OBSERVATIONS_STREAM) or {}
    keys = {}
    for destination_id, rule in by_destination.items():
        if (isinstance(rule, dict) and rule.get("destination_field") == PROVIDER_KEY_FIELD
                and isinstance(rule.get("default"), str) and rule["default"].strip()):
            keys[str(destination_id)] = rule["default"].strip()
    return keys


async def _route_provider_keys(client, connection, connection_id: str) -> Dict[str, str]:
    """Provider keys set as field mappings on the connection's routes. A route that can't
    be read leaves its destinations on the share's own provider key."""
    keys: Dict[str, str] = {}
    for rule in getattr(connection, "routing_rules", None) or []:
        route_id = getattr(rule, "id", None) or (rule.get("id") if isinstance(rule, dict) else rule)
        try:
            route = await client.get_route_details(route_id)
        except Exception:
            logger.exception("Could not read route %s from Gundi", route_id)
            continue
        data = getattr(getattr(route, "configuration", None), "data", None) or {}
        keys.update(provider_keys_from(data.get("field_mappings") or {}, connection_id))
    return keys


@asynccontextmanager
async def _gundi_client(client):
    if client is not None:
        yield client
        return
    async with GundiClient() as owned:
        yield owned


async def resolve_destinations(connection_id: str, source_url: str, gundi=None
                               ) -> Tuple[List[Destination], List[SkippedDestination]]:
    destinations, skipped = [], []
    async with _gundi_client(gundi) as client:
        connection = await client.get_connection_details(connection_id)
        provider_keys = await _route_provider_keys(client, connection, connection_id)
        for item in connection.destinations or []:
            item_id, name = str(item.id), item.name or str(item.id)
            type_value = item.type.value if item.type else ""
            if type_value != EARTHRANGER_TYPE:
                skipped.append(SkippedDestination(item_id, name, "not an EarthRanger destination"))
                continue
            try:
                details = await client.get_integration_details(item_id)
            except Exception:
                logger.exception("Could not read destination %s from Gundi", item_id)
                skipped.append(SkippedDestination(item_id, name, "its credentials could not be read from Gundi"))
                continue
            if details.enabled is False:
                skipped.append(SkippedDestination(item_id, name, "it's turned off in Gundi"))
                continue
            url = details.base_url or item.base_url
            if not url:
                skipped.append(SkippedDestination(item_id, name, "it has no site URL in Gundi"))
                continue
            try:
                site_host(url)
            except ValueError:
                skipped.append(SkippedDestination(item_id, name, "its site URL in Gundi isn't a valid web address"))
                continue
            if same_site(url, source_url):
                skipped.append(SkippedDestination(item_id, name, "same site as the source"))
                continue
            auth = find_config_for_action(details.configurations or [], "auth")
            if auth is None or not auth.data:
                skipped.append(SkippedDestination(item_id, name, "has no EarthRanger credentials"))
                continue
            try:
                credentials = AuthenticateConfig.parse_obj(auth.data)
            except ValidationError:
                skipped.append(SkippedDestination(item_id, name, "its EarthRanger credentials in Gundi couldn't be read"))
                continue
            missing = _missing_credentials(credentials)
            if missing:
                skipped.append(SkippedDestination(item_id, name, missing))
                continue
            destinations.append(Destination(item_id, name, url, credentials, provider_keys.get(item_id)))
    return destinations, skipped
