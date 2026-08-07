"""Small shared helper for querying Esri ArcGIS REST FeatureServer/MapServer
layers by point (optionally with a buffer distance). All of GSI, the
National Monuments Service, and Tailte Éireann publish their open data this
way — see CLAUDE.md for the full list of verified endpoints.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

from . import config

log = logging.getLogger("sitescout.arcgis")


def point_query_full(
    layer_url: str,
    lon: float,
    lat: float,
    out_fields: str = "*",
    distance_m: Optional[int] = None,
    return_geometry: bool = False,
    result_record_count: Optional[int] = None,
    order_by: Optional[str] = None,
) -> dict:
    """Queries an ArcGIS `.../query` endpoint for features intersecting (or
    within `distance_m` of) a WGS84 point. Returns the raw parsed response
    (not just `features`) so callers can check `exceededTransferLimit` —
    i.e. tell "exactly N found" from "N found, capped, more exist".
    """
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "f": "json",
        "returnGeometry": "true" if return_geometry else "false",
    }
    if return_geometry:
        params["outSR"] = "4326"
    if distance_m:
        params["distance"] = distance_m
        params["units"] = "esriSRUnit_Meter"
        params["resultRecordCount"] = result_record_count or 10
    if order_by:
        params["orderByFields"] = order_by

    log.debug("ArcGIS query: %s params=%s", layer_url, params)
    resp = requests.get(layer_url, params=params, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS service error: {data['error'].get('message', data['error'])}")
    return data


def point_query(
    layer_url: str,
    lon: float,
    lat: float,
    out_fields: str = "*",
    distance_m: Optional[int] = None,
    return_geometry: bool = False,
    result_record_count: Optional[int] = None,
) -> list[dict]:
    """Convenience wrapper around `point_query_full()` for the common case
    of just wanting the `features` list.
    """
    data = point_query_full(
        layer_url, lon, lat, out_fields, distance_m, return_geometry, result_record_count,
    )
    return data.get("features", [])
