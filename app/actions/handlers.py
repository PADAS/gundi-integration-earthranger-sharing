"""Action handlers. Keep these thin: the work lives in app/ersharing.

app/actions/__init__.py imports this module at import time, so app.ersharing is
imported inside each handler; a top-level import would be circular.
"""
from datetime import datetime, timedelta, timezone

from gundi_core.schemas.v2 import Integration

from app.services.action_scheduler import crontab_schedule
from app.services.activity_logger import activity_logger
from .configurations import (
    AuthenticateConfig, ERAuthenticationType, ListEventTypesQuery, ListFeatureGroupsQuery, ListSubjectGroupsQuery,
    SyncEventsConfig, SyncObservationsConfig,
)
from .core import ReferenceDataResponse, ReferenceOption, action_title


@action_title("Share Events")
@crontab_schedule("*/5 * * * *")
@activity_logger()
async def action_sync_events(integration: Integration, action_config: SyncEventsConfig):
    from app.ersharing.sync_events import run_sync_events
    return await run_sync_events(integration, action_config)


@action_title("Share Observations")
@crontab_schedule("*/5 * * * *")
@activity_logger()
async def action_sync_observations(integration: Integration, action_config: SyncObservationsConfig):
    from app.ersharing.sync_observations import run_sync_observations
    return await run_sync_observations(integration, action_config)


@action_title("Authentication")
async def action_auth(integration: Integration, action_config: AuthenticateConfig):
    from app.ersharing import clients
    from app.services.errors import IntegrationError

    if action_config.authentication_type == ERAuthenticationType.TOKEN and not action_config.token:
        return {"valid_credentials": False, "error": "Please provide a token."}
    if action_config.authentication_type == ERAuthenticationType.USERNAME_PASSWORD and not (
            action_config.username and action_config.password):
        return {"valid_credentials": False, "error": "Please provide both a username and a password."}
    source_url = clients.source_url_of(integration, action_config)
    if not source_url:
        return {"valid_credentials": False, "error": "Please provide the EarthRanger site URL."}
    try:
        async with clients.open_site(source_url, action_config, role="source") as site:
            me = await site.get_me()
            if not me.get("is_active", True):
                return {"valid_credentials": False}
            # Without a recent `since` and a cursor, ER counts the whole observations table and big sites time out.
            recent = {"since": (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(), "use_cursor": "true"}
            capabilities = {"events": await site.can_read("activity/events"),
                            "subject_tracks": await site.can_read("subjects")
                            and await site.can_read("observations", recent)}
    except IntegrationError as e:
        return {"valid_credentials": False, "error": e.message}
    result = {"valid_credentials": True, "capabilities": capabilities}
    missing = [name for name, ok in capabilities.items() if not ok]
    if missing:
        result["warning"] = (f"This token can't read: {', '.join(missing)}. "
                             "Sharing those will fail until the token's permissions are updated.")
    return result


async def _source_site(integration):
    from app.ersharing import clients
    auth = clients.auth_config_of(integration.configurations)
    return clients.open_site(clients.source_url_of(integration, auth), auth, role="source")


async def action_list_event_types(integration: Integration, action_config: ListEventTypesQuery):
    async with await _source_site(integration) as site:
        event_types = await site.list_event_types_v2()

    def group(event_type):
        category = event_type.get("category")
        return category.get("display") if isinstance(category, dict) else category

    options = [ReferenceOption(value=t["value"], label=t.get("display") or t["value"], group=group(t))
               for t in event_types]
    return ReferenceDataResponse(options=sorted(options, key=lambda o: (o.label or "").lower())).dict()


async def action_list_subject_groups(integration: Integration, action_config: ListSubjectGroupsQuery):
    async with await _source_site(integration) as site:
        groups = await site.list_subject_groups()
    options = [ReferenceOption(value=str(g["id"]), label=g.get("name")) for g in groups]
    return ReferenceDataResponse(options=sorted(options, key=lambda o: (o.label or "").lower())).dict()


async def action_list_feature_groups(integration: Integration, action_config: ListFeatureGroupsQuery):
    async with await _source_site(integration) as site:
        feature_groups = await site.list_feature_groups()
    options = [ReferenceOption(value=str(f["id"]), label=f.get("name")) for f in feature_groups]
    return ReferenceDataResponse(options=sorted(options, key=lambda o: (o.label or "").lower())).dict()
