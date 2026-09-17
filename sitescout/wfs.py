"""Shared helper for querying EPA's GeoServer (gis.epa.ie) via WFS
`GetFeature` — same role as `arcgis.py` (Esri REST) and `wms.py` (OPW's
GeoServer via `GetFeatureInfo`), extracted from `epa.py` once a second
module (`water_quality.py`) needed the same primitive.

Gotcha, confirmed by testing (don't re-derive): the `bbox` filter
parameter MUST carry an explicit CRS suffix
(`bbox=minx,miny,maxx,maxy,EPSG:4326`). Without it, GeoServer silently
interprets the bbox numbers in the layer's native storage CRS (Irish
Transverse Mercator, EPSG:2157) instead of WGS84 degrees — a bbox sized in
degrees is then a nonsensically tiny sliver in ITM metres, so the query
just returns zero features. No error, no warning — reads exactly like
"nothing nearby," confirmed by querying a real landfill's own centroid and
still getting zero results until the CRS suffix was added.
"""
from __future__ import annotations

import math

import requests

from . import config

WFS_URL = "https://gis.epa.ie/geoserver/wfs"


def _bbox_around(lat: float, lon: float, half_m: float) -> str:
    dlat = half_m / 111_000
    dlon = half_m / (111_000 * math.cos(math.radians(lat)))
    return f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat},EPSG:4326"


def wfs_query(type_name: str, lat: float, lon: float, half_m: float, max_features: int = 25) -> dict:
    """Returns the raw parsed WFS response (not just `features`) so callers
    can check `numberMatched` vs `numberReturned` — same "exactly N found"
    vs "N found, capped, more exist" distinction as arcgis.point_query_full().
    """
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": type_name,
        "bbox": _bbox_around(lat, lon, half_m),
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": max_features,
    }
    resp = requests.get(WFS_URL, params=params, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "features" not in data:
        raise RuntimeError(f"EPA WFS error for {type_name}: {data}")
    return data
