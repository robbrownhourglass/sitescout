"""Property boundary — Tailte Éireann cadastral parcels (Freehold, falling
back to Leasehold). These are real, live, queryable ArcGIS feature layers —
verified directly, including returned polygon geometry.

Important caveat (from Tailte Éireann's own documentation, not ours): these
boundaries are NOT a legally guaranteed property boundary. For that, get a
folio + filed plan from landdirect.ie. Only registered titles appear here.
"""
from __future__ import annotations

import logging

from .arcgis import point_query

log = logging.getLogger("sitescout.cadastral")

FREEHOLD_URL = (
    "https://services-eu1.arcgis.com/FH5XCsx8rYXqnjF5/arcgis/rest/services/"
    "Cadastral_Parcels_Freehold/FeatureServer/12/query"
)
LEASEHOLD_URL = (
    "https://services-eu1.arcgis.com/FH5XCsx8rYXqnjF5/arcgis/rest/services/"
    "Cadastral_Parcels_Leasehold/FeatureServer/13/query"
)


def get_boundary(lat: float, lon: float) -> dict:
    log.info("Querying Tailte Éireann cadastral parcels (freehold)…")
    feats = point_query(FREEHOLD_URL, lon, lat, return_geometry=True)
    tenure = "Freehold"
    if not feats:
        log.info("No freehold parcel found — trying leasehold…")
        feats = point_query(LEASEHOLD_URL, lon, lat, return_geometry=True)
        tenure = "Leasehold"

    if not feats:
        log.info("-> No registered parcel found at this exact point")
        return {
            "found": False,
            "source": "Tailte Éireann Cadastral Parcels (Freehold/Leasehold)",
            "note": "Title may be unregistered, or point falls outside a mapped parcel. "
                    "Check https://www.landdirect.ie directly.",
        }

    p = feats[0]
    area_m2 = p["attributes"].get("Shape__Area")
    county = p["attributes"].get("COUNTY_NAM")
    area_ha = round(area_m2 / 10000, 3) if area_m2 else None
    area_acres = round(area_m2 / 4046.86, 3) if area_m2 else None
    rings = p.get("geometry", {}).get("rings")

    log.info("-> %s parcel found in %s, ~%.3f ha (%.3f acres)", tenure, county, area_ha or 0, area_acres or 0)
    return {
        "found": True,
        "tenure": tenure,
        "county": county,
        "area_hectares": area_ha,
        "area_acres": area_acres,
        "polygon_rings_itm_wgs84": rings,  # [[ [lon,lat], ... ]], WGS84
        "source": "Tailte Éireann Cadastral Parcels (" + tenure + ")",
        "caveat": (
            "This is Tailte Éireann's mapped extent of a registered title — "
            "NOT a legally guaranteed boundary. For the legal folio & filed "
            "plan see https://www.landdirect.ie"
        ),
    }
