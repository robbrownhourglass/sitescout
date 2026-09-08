"""Property boundary — Tailte Éireann cadastral parcels (Freehold, falling
back to Leasehold). These are real, live, queryable ArcGIS feature layers —
verified directly, including returned polygon geometry.

Important caveat (from Tailte Éireann's own documentation, not ours): these
boundaries are NOT a legally guaranteed property boundary. For that, get a
folio + filed plan from landdirect.ie. Only registered titles appear here.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

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


NEARBY_PARCELS_RADIUS_M = 150


def get_nearby_parcels(lat: float, lon: float, radius_m: int = NEARBY_PARCELS_RADIUS_M) -> list[dict]:
    """Every cadastral parcel (freehold + leasehold) within `radius_m` of
    the point — for the plot-confirmation step, not `get_boundary()`'s
    single-parcel-at-the-exact-point lookup. Many real properties span more
    than one registered parcel (a house plus a separate laneway or shed
    parcel, one legal owner but several `SP_ID`s) — the picker needs the
    neighbours too so the user can select/merge the ones that actually
    make up their site, not just whichever one the point happened to land
    in. `contains_point` flags the one(s) get_boundary() would have picked,
    for the frontend to pre-select.
    """
    log.info("Querying nearby cadastral parcels within %dm…", radius_m)
    primary_ids = set()
    for tenure, url in ((("Freehold", FREEHOLD_URL)), ("Leasehold", LEASEHOLD_URL)):
        exact = point_query(url, lon, lat, out_fields="OBJECTID")
        primary_ids.update(f["attributes"].get("OBJECTID") for f in exact)

    parcels = []
    for tenure, url in (("Freehold", FREEHOLD_URL), ("Leasehold", LEASEHOLD_URL)):
        feats = point_query(
            url, lon, lat,
            out_fields="OBJECTID,SP_ID,COUNTY_NAM,Shape__Area",
            distance_m=radius_m,
            return_geometry=True,
            result_record_count=100,
        )
        for f in feats:
            a = f["attributes"]
            oid = a.get("OBJECTID")
            area_m2 = a.get("Shape__Area")
            parcels.append({
                "parcel_id": f"{tenure[0]}{oid}",
                "tenure": tenure,
                "county": a.get("COUNTY_NAM"),
                "area_hectares": round(area_m2 / 10000, 3) if area_m2 else None,
                "area_acres": round(area_m2 / 4046.86, 3) if area_m2 else None,
                "polygon_rings_wgs84": f.get("geometry", {}).get("rings"),
                "contains_point": oid in primary_ids,
            })
    log.info(
        "-> %d parcel(s) within %dm (%d contain the exact point)",
        len(parcels), radius_m, sum(1 for p in parcels if p["contains_point"]),
    )
    return parcels


def summarise_selected_parcels(parcels: list[dict]) -> dict:
    """Aggregates a set of parcels the user picked/merged in the plot
    picker into one "boundary" section — same shape as get_boundary(),
    used everywhere else in this app, but summed/combined across however
    many parcels the user selected instead of a single point-query result.
    No true polygonal union (would need a geometry library this app
    doesn't otherwise depend on) — just draws every selected parcel's own
    outline and sums the areas, which is what "join into one" means for
    reporting purposes here.
    """
    if not parcels:
        return {
            "found": False,
            "source": "Tailte Éireann Cadastral Parcels (Freehold/Leasehold)",
            "note": "No parcel selected. Title may be unregistered, or the point falls outside "
                    "a mapped parcel. Check https://www.landdirect.ie directly.",
        }
    area_ha = sum(p["area_hectares"] for p in parcels if p.get("area_hectares"))
    area_acres = sum(p["area_acres"] for p in parcels if p.get("area_acres"))
    tenures = sorted({p["tenure"] for p in parcels})
    return {
        "found": True,
        "tenure": " + ".join(tenures),
        "county": parcels[0].get("county"),
        "area_hectares": round(area_ha, 3),
        "area_acres": round(area_acres, 3),
        "parcel_count": len(parcels),
        # A LIST of ring-sets, one per selected parcel — deliberately a
        # different key from get_boundary()'s singular "polygon_rings_itm_wgs84"
        # (one ring-set) so callers can't mix the two shapes up by accident.
        "polygon_ring_sets_wgs84": [p.get("polygon_rings_wgs84") for p in parcels if p.get("polygon_rings_wgs84")],
        "source": "Tailte Éireann Cadastral Parcels (" + " + ".join(tenures) + ")",
        "caveat": (
            ("Merged from " + str(len(parcels)) + " selected parcels. " if len(parcels) > 1 else "")
            + "This is Tailte Éireann's mapped extent of a registered title — "
            "NOT a legally guaranteed boundary. For the legal folio & filed "
            "plan see https://www.landdirect.ie"
        ),
    }


def get_boundaries_for_points(points: list[tuple[float, float]], max_workers: int = 10) -> dict[tuple[float, float], Optional[dict]]:
    """Looks up the cadastral parcel at each of `points` — used to draw a
    property boundary for every planning application found nearby, not
    just the searched site itself. Runs concurrently (one call can mean
    dozens of points, one per planning application within the search
    radius) since `get_boundary()` is up to two sequential HTTP requests
    on its own (freehold, falling back to leasehold); doing that serially
    for 25+ points would make a single site-scout request noticeably
    slower. Dedupes identical points first — multiple planning
    applications often share the same site.

    Returns a dict keyed by the exact (lat, lon) tuple passed in, so
    callers can map results back onto whatever they're enriching; a
    failed/not-found lookup maps to None or `{"found": False, ...}"`
    (never raises — one bad point shouldn't sink the whole batch).
    """
    unique_points = list({p for p in points if p[0] is not None and p[1] is not None})
    if not unique_points:
        return {}

    log.info("Looking up cadastral boundaries for %d point(s)…", len(unique_points))

    def _lookup(pt: tuple[float, float]):
        lat, lon = pt
        try:
            return pt, get_boundary(lat, lon)
        except Exception as exc:
            log.warning("-> boundary lookup failed for (%.6f, %.6f): %s", lat, lon, exc)
            return pt, None

    results: dict[tuple[float, float], Optional[dict]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for pt, boundary in executor.map(_lookup, unique_points):
            results[pt] = boundary
    found_count = sum(1 for b in results.values() if b and b.get("found"))
    log.info("-> %d/%d point(s) had a mapped parcel", found_count, len(unique_points))
    return results
