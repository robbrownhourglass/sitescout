"""Small shared helper for querying OPW's flood-map WMS service via
`GetFeatureInfo`. Unlike everything else in this app (GSI, SMR, Tailte
Éireann, NIAH, planning applications), OPW's predictive flood extent maps
aren't on an Esri ArcGIS REST service — floodinfo.ie runs its own
GeoServer, found by pulling apart its map viewer's JS (see CLAUDE.md /
planning.py for the writeup). GeoServer speaks WMS, which is a different
query shape: no lon/lat point query, just `GetFeatureInfo` against a small
bounding box in Web Mercator (EPSG:900913/3857), reading off one pixel.

Confirmed live and public, no auth: a GetFeatureInfo call for the fluvial
1%-AEP layer at Fermoy, Co. Cork returned a real flood-extent polygon.
Also confirmed GeoServer returns each matched feature's FULL geometry, not
clipped to the query bbox — so a small bbox is enough for the hit-test and
still gives back the whole polygon to draw on a map.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import requests

from . import config

log = logging.getLogger("sitescout.wms")

FLOOD_WMS_URL = "https://www.floodinfo.ie/geoserver/wms"


def lonlat_to_webmercator(lon: float, lat: float) -> tuple[float, float]:
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180)
    y = y * 20037508.34 / 180
    return x, y


def webmercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon = x * 180 / 20037508.34
    lat = (2 * math.atan(math.exp((y / 20037508.34 * 180) * math.pi / 180)) - math.pi / 2) * 180 / math.pi
    return lon, lat


def _reproject_coords(coords):
    """Recursively walks a GeoJSON `coordinates` array (Point/LineString/
    Polygon/MultiPolygon — any nesting depth) and reprojects every
    [x, y] pair from EPSG:3857 to WGS84 lon/lat in place.
    """
    if not coords:
        return coords
    if isinstance(coords[0], (int, float)):
        lon, lat = webmercator_to_lonlat(coords[0], coords[1])
        return [lon, lat]
    return [_reproject_coords(c) for c in coords]


def reproject_geometry(geometry: Optional[dict]) -> Optional[dict]:
    """Reprojects a GeoJSON geometry dict (as returned by GeoServer's
    `INFO_FORMAT=application/json`) from EPSG:3857 to WGS84.
    """
    if not geometry:
        return None
    return {
        "type": geometry["type"],
        "coordinates": _reproject_coords(geometry["coordinates"]),
    }


def get_feature_info(layer: str, lon: float, lat: float, half_extent_m: int = 150) -> list[dict]:
    """Runs a WMS `GetFeatureInfo` for `layer` at (lon, lat), via a small
    bounding box centred on the point (WMS has no native point-query verb).
    Returns a list of `{"properties": ..., "geometry": <WGS84 GeoJSON>}` —
    empty if the point isn't covered by this layer's mapped extent (which,
    for CFRAM flood layers, doesn't mean "not at risk" — it may just mean
    that area hasn't been studied; see planning.py's caveat text).
    """
    x, y = lonlat_to_webmercator(lon, lat)
    bbox = f"{x - half_extent_m},{y - half_extent_m},{x + half_extent_m},{y + half_extent_m}"
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetFeatureInfo",
        "LAYERS": layer,
        "QUERY_LAYERS": layer,
        "STYLES": "",
        "BBOX": bbox,
        "WIDTH": 101,
        "HEIGHT": 101,
        "X": 50,
        "Y": 50,
        "SRS": "EPSG:900913",
        "INFO_FORMAT": "application/json",
        "FEATURE_COUNT": 5,
    }
    resp = requests.get(FLOOD_WMS_URL, params=params, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return [
        {"properties": f.get("properties", {}), "geometry": reproject_geometry(f.get("geometry"))}
        for f in data.get("features", [])
    ]
