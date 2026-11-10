"""What Share Events / Share Subjects would share, for the portal's confirmation.

The portal runs these with the section's unsaved values (config_overrides)
before it saves, and draws the answer as a grouped list. The shape is the
gundi:share_preview contract; see gundi-portal
docs/superpowers/specs/2026-11-09-er2er-sharing-preview-modal-design.md.

They read the same things the sharing actions read when they run: event
categories and event types (v2, and the v1 types ER's v2 list leaves out),
and subject groups with their subjects.
"""
import asyncio
from typing import Dict, Iterable, List, Optional

from app.services.errors import IntegrationAuthError

from .event_types import category_value
from .naming import site_host

# Items listed per group; a group's total_count stays exact when it is cut.
ITEM_CAP = 200

# The list can't tell which events or points fall inside an area filter, so it
# says the filter narrows what is shared.
AREA_NOTE_EVENTS = "Only events inside the chosen areas are shared."
AREA_NOTE_TRACKS = "Only track points inside the chosen areas are shared."


def _wanted(values: Optional[Iterable]) -> List[str]:
    """The picked values, blank rows left out. Empty means "share all". Kept
    exactly as given: the sharing actions compare them as stored."""
    return [str(v) for v in (values or []) if v is not None and str(v).strip()]


def _cap(groups: List[dict]) -> bool:
    truncated = False
    for group in groups:
        if len(group["items"]) > ITEM_CAP:
            group["items"] = group["items"][:ITEM_CAP]
            truncated = True
    return truncated


def _by_label(item: dict) -> str:
    return (item.get("label") or "").lower()


def _cant_read(what: str, site_url: str, error: IntegrationAuthError) -> str:
    """A 401 means EarthRanger rejected the token itself; a 403 that it can't
    read this part."""
    if error.status_code == 401:
        return f"EarthRanger rejected this token on {site_host(site_url)}."
    return f"This token can't read {what} on {site_host(site_url)}."


def _response(site_url: str, mode: str, selected_count: int, total_count: int, groups: List[dict],
              missing: List[str], warning: Optional[str] = None) -> dict:
    truncated = _cap(groups)
    return {
        "source_site": site_url,
        "mode": mode,
        "selected_count": selected_count,
        "total_count": total_count,
        "groups": groups,
        "missing_values": missing,
        "truncated": truncated,
        "warning": warning,
    }


async def preview_events(site, event_types: Optional[Iterable], area_feature_groups: Optional[Iterable] = None) -> dict:
    """Event types grouped by category, each with its icon and default priority.

    A token that can't read categories or types gets an empty list with a
    warning rather than an error, so the user still sees why and can save.
    """
    site_url = site.url
    selected = _wanted(event_types)
    mode = "selected" if selected else "all"
    try:
        # Read together; the schemas aren't needed for a list. Events of v1
        # types are shared too (copied into a type the destination already
        # has), and ER's v2 list leaves those types out.
        categories, types, v1_types = await asyncio.gather(
            site.get_event_categories(),
            # Inactive ones too: a picked type that was deactivated is still
            # listed (as inactive) rather than reported as missing.
            site.list_event_types_v2(include_schema=False, include_inactive=True),
            site.list_event_types_v1(),
        )
    except IntegrationAuthError as e:
        return _response(site_url, mode, 0, 0, [], [], _cant_read("event types", site_url, e))
    v2_values = {t.get("value") for t in types}
    types = types + [t for t in v1_types if t.get("value") not in v2_values]

    known = {t.get("value") for t in types}
    active = [t for t in types if t.get("is_active") is not False]
    picked = active if mode == "all" else [t for t in types if t.get("value") in selected]

    def item(t: dict) -> dict:
        return {
            "value": t["value"],
            "label": t.get("display") or t["value"],
            "icon_url": f"{site_url}/static/sprite-src/{t.get('icon_id') or t['value']}.svg",
            "is_active": t.get("is_active") is not False,
            "priority": t.get("default_priority") or 0,
            "detail": None,
        }

    # Every category a type points at gets a group, even one the categories
    # call didn't return (the type's own category record is used then), so a
    # type never drops out of the list.
    known_categories = {c.get("value"): c for c in categories}
    for t in types:
        slug = category_value(t)
        if slug and slug not in known_categories:
            raw = t.get("category")
            known_categories[slug] = raw if isinstance(raw, dict) else {"value": slug}

    groups = []
    for category in known_categories.values():
        slug = category.get("value")
        items = sorted((item(t) for t in picked if category_value(t) == slug), key=_by_label)
        if not items:
            continue
        groups.append({
            "value": slug,
            "label": category.get("display") or slug,
            "order": category.get("ordernum") or 0,
            "total_count": sum(1 for t in types if category_value(t) == slug),
            "items": items,
        })
    groups.sort(key=lambda g: (g["order"], (g["label"] or "").lower()))
    return _response(
        site_url, mode,
        selected_count=sum(len(g["items"]) for g in groups),
        total_count=len(active),
        groups=groups,
        missing=[v for v in selected if v not in known],
        warning=AREA_NOTE_EVENTS if _wanted(area_feature_groups) else None,
    )


async def preview_subjects(site, subject_groups: Optional[Iterable],
                           area_feature_groups: Optional[Iterable] = None) -> dict:
    """Subjects grouped by subject group, each with its icon and last point.

    Counts are distinct subjects, as sync_observations shares them: a subject
    in two picked groups is shared once.
    """
    site_url = site.url
    selected = _wanted(subject_groups)
    mode = "selected" if selected else "all"
    try:
        groups_raw = await site.list_subject_groups()
    except IntegrationAuthError as e:
        return _response(site_url, mode, 0, 0, [], [], _cant_read("subject groups", site_url, e))

    known = {str(g.get("id")) for g in groups_raw}
    picked = ([g for g in groups_raw if g.get("subjects")] if mode == "all"
              else [g for g in groups_raw if str(g.get("id")) in selected])

    def icon(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        if url.startswith(("http://", "https://")):
            return url
        return f"{site_url}{'' if url.startswith('/') else '/'}{url}"

    def distinct(groups: List[dict]) -> int:
        return len({str(s.get("id")) for g in groups for s in (g.get("subjects") or [])})

    groups = []
    for g in picked:
        subjects = g.get("subjects") or []
        groups.append({
            "value": str(g.get("id")),
            "label": g.get("name"),
            "order": 0,
            "total_count": len(subjects),
            "items": sorted(({
                "value": str(s.get("id")),
                "label": s.get("name"),
                "icon_url": icon(s.get("image_url")),
                "is_active": s.get("is_active") is not False,
                "detail": s.get("last_position_date"),
            } for s in subjects), key=_by_label),
        })
    groups.sort(key=lambda grp: (grp["label"] or "").lower())
    return _response(
        site_url, mode,
        selected_count=distinct(picked),
        total_count=distinct(groups_raw),
        groups=groups,
        missing=[v for v in selected if v not in known],
        warning=AREA_NOTE_TRACKS if _wanted(area_feature_groups) else None,
    )
