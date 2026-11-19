"""Area filters built from the source site's spatial feature groups (spec §7.1, §8)."""
from typing import List, Optional, Sequence

from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

from app.services.errors import IntegrationConfigurationError, IntegrationError
from .clients import halts_site


class Area:
    def __init__(self, shapes: List[BaseGeometry]):
        self._shapes = shapes

    def contains_point(self, lon: float, lat: float) -> bool:
        point = Point(lon, lat)
        return any(s.contains(point) for s in self._shapes)

    def intersects_event(self, event: dict) -> bool:
        geometry = (event.get("geojson") or {}).get("geometry")
        if not geometry:
            return False
        event_shape = shape(geometry)
        return any(s.intersects(event_shape) for s in self._shapes)


def shapes_from_feature_group(feature_group: dict, polygons_only: bool = False) -> List[BaseGeometry]:
    shapes = []
    for spatial_feature in feature_group.get("features") or []:
        for geojson_feature in spatial_feature.get("features") or []:
            if geojson_feature.get("geometry"):
                geometry = shape(geojson_feature["geometry"])
                if not polygons_only or isinstance(geometry, (Polygon, MultiPolygon)):
                    shapes.append(geometry)
    return shapes


async def load_area(site, feature_group_ids: Sequence[str], polygons_only: bool = False) -> Optional[Area]:
    """The area filter for the given feature groups. A track point can only fall inside
    an area, so the tracks filter (polygons_only) uses polygons alone, as v1 did."""
    if not feature_group_ids:
        return None
    shapes = []
    for feature_group_id in feature_group_ids:
        try:
            feature_group = await site.get_feature_group(feature_group_id)
        except IntegrationError as e:
            if halts_site(e):
                raise
            raise IntegrationConfigurationError(
                f"Feature group {feature_group_id} could not be loaded from the source site; "
                "check it still exists and update the area filter."
            ) from e
        shapes.extend(shapes_from_feature_group(feature_group, polygons_only))
    if not shapes and polygons_only:
        raise IntegrationConfigurationError(
            "No tracks were shared because the area filter's feature groups have no areas (polygons) in them."
        )
    if not shapes:
        raise IntegrationConfigurationError("The area filter's feature groups contain no shapes.")
    return Area(shapes)
