"""The connection's EarthRanger destinations and their stored credentials (spec §5)."""
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import List, Tuple

from gundi_client_v2 import GundiClient
from pydantic import ValidationError

from app.actions.configurations import AuthenticateConfig
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


@dataclass(frozen=True)
class SkippedDestination:
    id: str
    name: str
    reason: str


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
            destinations.append(Destination(item_id, name, url, credentials))
    return destinations, skipped
