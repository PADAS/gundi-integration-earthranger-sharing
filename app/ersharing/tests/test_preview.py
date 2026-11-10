import pytest

from app.ersharing import preview
from app.ersharing.preview import preview_events, preview_subjects
from app.ersharing.tests.fakes import FakeSite
from app.services.errors import IntegrationAuthError

SITE = "https://sitea.pamdas.org"


@pytest.fixture
def site():
    s = FakeSite(SITE, role="source")
    s.categories = {
        "security": {"value": "security", "display": "Security", "ordernum": 1},
        "monitoring": {"value": "monitoring", "display": "Monitoring", "ordernum": 2},
    }
    s.event_types = {
        "poaching": {"value": "poaching", "display": "Poaching", "category": "security",
                     "icon_id": "poaching_rep", "default_priority": 300, "is_active": True},
        "fire": {"value": "fire", "display": "Fire", "category": {"value": "monitoring"},
                 "default_priority": 200, "is_active": True},
        "old": {"value": "old", "display": "Old type", "category": "monitoring", "is_active": False},
    }
    s.subject_groups = [
        {"id": "g1", "name": "Vultures", "subjects": [
            {"id": "s1", "name": "Kali", "image_url": "/static/vulture.svg",
             "last_position_date": "2026-11-09T09:41:00+00:00"},
            {"id": "s2", "name": "Tumbo", "image_url": "https://cdn.example.org/t.svg"},
        ]},
        {"id": "g2", "name": "Elephants", "subjects": [{"id": "s1", "name": "Kali"}]},
        {"id": "g3", "name": "Empty", "subjects": []},
    ]
    return s


@pytest.mark.asyncio
async def test_all_event_types_when_none_are_picked(site):
    result = await preview_events(site, [])
    assert result["mode"] == "all"
    assert result["source_site"] == SITE
    # Active types only, grouped by category in category order.
    assert [(g["value"], [i["value"] for i in g["items"]]) for g in result["groups"]] == [
        ("security", ["poaching"]), ("monitoring", ["fire"])]
    assert (result["selected_count"], result["total_count"]) == (2, 2)
    poaching = result["groups"][0]["items"][0]
    assert poaching == {
        "value": "poaching", "label": "Poaching",
        "icon_url": f"{SITE}/static/sprite-src/poaching_rep.svg",
        "is_active": True, "priority": 300, "detail": None,
    }
    # A type without its own icon id uses its value, as the ER web app does.
    assert result["groups"][1]["items"][0]["icon_url"] == f"{SITE}/static/sprite-src/fire.svg"
    # The group counts every type in the category, inactive ones too.
    assert result["groups"][1]["total_count"] == 2


@pytest.mark.asyncio
async def test_picked_event_types_with_missing_and_inactive_ones(site):
    result = await preview_events(site, ["old", "fire", "", None, "gone"])
    assert result["mode"] == "selected"
    assert [i["value"] for g in result["groups"] for i in g["items"]] == ["fire", "old"]
    assert [i["is_active"] for g in result["groups"] for i in g["items"]] == [True, False]
    assert result["selected_count"] == 2
    assert result["missing_values"] == ["gone"]


@pytest.mark.asyncio
async def test_v1_event_types_are_listed_too(site):
    # A site whose types are all v1: ER's v2 list is empty, yet those events
    # are shared, so the preview must list them.
    site.event_types = {}
    site.event_types_v1 = {
        "cluster": {"value": "cluster", "display": "Cluster", "category": {"value": "monitoring"},
                    "icon_id": "cluster_rep", "default_priority": 100, "is_active": True},
        "retired": {"value": "retired", "display": "Retired", "category": {"value": "monitoring"},
                    "is_active": False},
    }
    result = await preview_events(site, [])
    assert [i["value"] for g in result["groups"] for i in g["items"]] == ["cluster"]
    assert result["groups"][0]["items"][0]["icon_url"] == f"{SITE}/static/sprite-src/cluster_rep.svg"
    assert (result["selected_count"], result["total_count"]) == (1, 1)
    picked = await preview_events(site, ["retired"])
    assert picked["missing_values"] == []
    assert [i["is_active"] for g in picked["groups"] for i in g["items"]] == [False]


@pytest.mark.asyncio
async def test_a_type_on_both_lists_is_listed_once(site):
    site.event_types_v1 = {"fire": {"value": "fire", "display": "Fire (v1)", "category": {"value": "monitoring"}}}
    result = await preview_events(site, [])
    labels = [i["label"] for g in result["groups"] for i in g["items"]]
    assert labels.count("Fire") == 1 and "Fire (v1)" not in labels


@pytest.mark.asyncio
async def test_events_a_token_that_cant_read_them_gets_a_warning(site):
    site.failures["list_event_types_v2"] = IntegrationAuthError("denied", status_code=403)
    result = await preview_events(site, [])
    assert result["groups"] == []
    assert result["warning"] == "This token can't read event types on sitea.pamdas.org."


@pytest.mark.asyncio
async def test_all_subject_groups_when_none_are_picked(site):
    result = await preview_subjects(site, [])
    assert result["mode"] == "all"
    # Empty groups are left out of "all"; groups sorted by name.
    assert [g["label"] for g in result["groups"]] == ["Elephants", "Vultures"]
    # Distinct subjects: Kali is in two groups and shared once.
    assert (result["selected_count"], result["total_count"]) == (2, 2)
    vultures = result["groups"][1]
    assert vultures["items"][0] == {
        "value": "s1", "label": "Kali", "icon_url": f"{SITE}/static/vulture.svg",
        "is_active": True, "detail": "2026-11-09T09:41:00+00:00",
    }
    assert vultures["items"][1]["icon_url"] == "https://cdn.example.org/t.svg"
    assert vultures["items"][1]["detail"] is None


@pytest.mark.asyncio
async def test_picked_subject_groups_keep_empty_ones_and_name_missing_ones(site):
    result = await preview_subjects(site, ["g3", "nope"])
    assert [(g["label"], g["total_count"]) for g in result["groups"]] == [("Empty", 0)]
    assert result["selected_count"] == 0
    assert result["missing_values"] == ["nope"]


@pytest.mark.asyncio
async def test_subjects_a_token_that_cant_read_groups_gets_a_warning(site):
    site.failures["list_subject_groups"] = IntegrationAuthError("denied", status_code=403)
    result = await preview_subjects(site, [])
    assert result["groups"] == []
    assert result["warning"] == "This token can't read subject groups on sitea.pamdas.org."


@pytest.mark.asyncio
async def test_long_groups_are_cut_and_say_so(site, monkeypatch):
    monkeypatch.setattr(preview, "ITEM_CAP", 1)
    result = await preview_subjects(site, ["g1"])
    assert result["truncated"] is True
    assert len(result["groups"][0]["items"]) == 1
    assert result["groups"][0]["total_count"] == 2
