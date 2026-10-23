import pytest

from app.ersharing.geo import load_area, shapes_from_feature_group
from app.ersharing.tests.fakes import FakeSite, make_event
from app.services.errors import IntegrationConfigurationError

# ER nests GeoJSON features one level down inside each spatial feature.
SQUARE = {"id": "fg1", "name": "Park", "features": [{"name": "Park", "features": [
    {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]]}}
]}]}


def source_with_square():
    site = FakeSite("https://sitea.pamdas.org", role="source")
    site.feature_groups["fg1"] = SQUARE
    return site


def test_shapes_are_read_from_nested_features():
    assert len(shapes_from_feature_group(SQUARE)) == 1


@pytest.mark.asyncio
async def test_area_filters_points_and_events():
    area = await load_area(source_with_square(), ["fg1"])
    assert area.contains_point(5, 5) and not area.contains_point(20, 5)
    inside = make_event("e1", "2026-10-01T00:00:00Z")
    outside = make_event("e2", "2026-10-01T00:00:00Z",
                         geojson={"type": "Feature", "geometry": {"type": "Point", "coordinates": [20, 5]}})
    no_geometry = make_event("e3", "2026-10-01T00:00:00Z", geojson=None)
    assert area.intersects_event(inside)
    assert not area.intersects_event(outside)
    assert not area.intersects_event(no_geometry)


@pytest.mark.asyncio
async def test_no_feature_groups_means_no_filter():
    assert await load_area(source_with_square(), []) is None


@pytest.mark.asyncio
async def test_missing_or_empty_feature_group_is_a_configuration_error():
    site = source_with_square()
    with pytest.raises(IntegrationConfigurationError, match="Feature group gone"):
        await load_area(site, ["gone"])
    site.feature_groups["empty"] = {"id": "empty", "features": []}
    with pytest.raises(IntegrationConfigurationError, match="contain no shapes"):
        await load_area(site, ["empty"])
