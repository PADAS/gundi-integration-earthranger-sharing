"""What the source token can do at the site, for the Show Permissions action.

The same card as gundi-integration-earthranger's show_permissions (user details,
global permissions, event categories with their permissions, subject groups), so
admins see the two connectors alike.
"""
import logging
from collections import defaultdict
from typing import Iterator, List

logger = logging.getLogger(__name__)

# user/me permission keys that aren't event categories.
NOT_CATEGORIES = {
    "eventcategory", "eventtype", "event", "message", "observation", "patrol", "patrolconfiguration",
    "tsvectormodel", "community", "patroltype", "team", "eventsource", "person", "notificationmethod",
    "teammembership", "eventphoto", "eventclassfactor", "eventdetails", "patrolconfigurationsubjectgroup",
    "eventclass", "eventrelationshiptype", "eventnotification", "eventprovider", "alertrulenotificationmethod",
    "eventnote", "eventrelatedsubject", "alertruleeventtype", "membershiptype", "refreshrecreateeventdetailview",
    "dummy_events", "eventfactor", "eventrelatedsegments", "eventsourceevent", "formbuilderproxy",
    "eventgeometry", "eventfilter", "smart", "eventattachment", "alertrule", "eventfile", "eventrelationship",
    "event_for_eventsource",
}


def connected_as(user: dict):
    """Who a token signs in as, e.g. "Gundi Service Account (gundi_service_account)", or just the
    username when the account has no name. None without a username."""
    username = (user.get("username") or "").strip()
    if not username:
        return None
    full_name = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
    return f"{full_name} ({username})" if full_name else username


def user_details(user: dict) -> dict:
    return {
        "Full Name": f"{user.get('first_name', '') or ''} {user.get('last_name', '') or ''}".strip(),
        "Username": user.get("username", ""),
        "Is Superuser": user.get("is_superuser", False),
    }


def global_permissions(permissions: dict) -> dict:
    out = {}
    for key, label in (("eventcategory", "Event Category"), ("eventtype", "Event Type"), ("event", "Event"),
                       ("message", "Message"), ("observation", "Observation")):
        if permissions.get(key):
            out[label] = sorted(permissions[key])
    return out


def category_permissions(permissions: dict) -> dict:
    return {key: sorted(perms) for key, perms in permissions.items() if key not in NOT_CATEGORIES}


def categories_with_types(category_perms: dict, event_types: List[dict]) -> dict:
    """Event types grouped by category display name, with the token's permissions on each
    category. A category the token sees without an explicit permission is at least viewable."""
    categories = defaultdict(lambda: {"Event Types": set(), "Permissions": set()})
    for event_type in event_types:
        category = event_type.get("category") or {}
        if not isinstance(category, dict) or not category.get("is_active", False):
            continue
        name, value, type_name = category.get("display"), category.get("value"), event_type.get("display")
        if not name or not value or not type_name:
            continue
        if value in category_perms:
            if not categories[name]["Permissions"]:
                categories[name]["Permissions"].update(category_perms[value])
        else:
            categories[name]["Permissions"].add("view")
        categories[name]["Event Types"].add(type_name)
    return {name: {"Event Types": sorted(v["Event Types"]), "Permissions": sorted(v["Permissions"])}
            for name, v in categories.items()}


def as_v1_shape(v1_types: List[dict], v2_types: List[dict], categories: List[dict]) -> List[dict]:
    """v1 and v2 event types together, with v2's bare category slug expanded into v1's
    nested category, named from the categories list (v2 lists only active categories)."""
    display = {et["category"]["value"]: et["category"]["display"] for et in v1_types
               if isinstance(et.get("category"), dict) and et["category"].get("value") and et["category"].get("display")}
    display.update({c["value"]: c["display"] for c in categories or [] if c.get("value") and c.get("display")})
    out = []
    for et in v1_types:
        category = et.get("category")
        if isinstance(category, dict) and category.get("value") in display:
            et = {**et, "category": {**category, "display": display[category["value"]]}}
        out.append(et)
    for et in v2_types:
        slug = et.get("category")
        if isinstance(slug, str) and slug:
            et = {**et, "category": {"value": slug, "display": display.get(slug, slug), "is_active": True}}
        out.append(et)
    return out


def walk_groups(groups: List[dict]) -> Iterator[dict]:
    for group in groups or []:
        yield group
        yield from walk_groups(group.get("subgroups") or [])


def subject_groups(groups: List[dict]) -> dict:
    """Subject names per group; a parent group also lists its subgroups' subjects."""
    result = defaultdict(set)

    def visit(group, parents):
        names = {s["name"] for s in group.get("subjects") or [] if s.get("name")}
        result[group["name"]].update(names)
        for parent in parents:
            result[parent].update(names)
        for sub in group.get("subgroups") or []:
            visit(sub, parents + [group["name"]])

    for group in groups or []:
        visit(group, [])
    return {name: sorted(names) for name, names in result.items()}


def group_ids(groups: List[dict]) -> List[str]:
    return sorted({str(g["id"]) for g in walk_groups(groups) if g.get("id")})
