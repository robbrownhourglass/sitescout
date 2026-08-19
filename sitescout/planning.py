"""Planning context — zoning, planning application history, radon risk, and
(as of this module's second half) flood risk.

Land-use zoning and radon still don't have a confirmed-working open
point-query API from this dev environment — they stay link-outs. Planning
application history and flood risk, however, were found and verified live
by reverse-engineering the public map viewers that sit behind myplan.ie and
floodinfo.ie (see CLAUDE.md for the full writeup of how each was found):

- Planning applications: myplan.ie embeds an Esri "LIVE-NPAD WAB" app
  (National Planning Application Database) backed by a public ArcGIS
  FeatureServer with full per-application detail (status, decision, dates,
  appeals) — same query shape as the rest of this app (`arcgis.py`).
- Flood risk: floodinfo.ie's map viewer is a custom OpenLayers app calling
  OPW's own GeoServer directly — CFRAM predictive flood-extent polygons,
  fluvial and coastal, at three probability bands each. Different query
  shape (WMS `GetFeatureInfo`, not an ArcGIS point query) — see `wms.py`.

Zoning specifically (as opposed to planning application history) isn't in
the NPAD dataset — Ireland's ~31 local authorities each publish their own
zoning maps, no single national layer was found. That stays a link-out.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from . import wms
from .arcgis import attribute_query, point_query_full

log = logging.getLogger("sitescout.planning")

PLANNING_APPLICATIONS_URL = (
    "https://services.arcgis.com/NzlPQPKn5QF9v2US/arcgis/rest/services/"
    "IrishPlanningApplications_FVLayer/FeatureServer/0/query"
)
PLANNING_APPLICATIONS_SEARCH_RADIUS_M = 500
PLANNING_APPLICATIONS_OUT_FIELDS = (
    "ApplicationNumber,DevelopmentDescription,DevelopmentAddress,ApplicationStatus,"
    "Decision,ReceivedDate,DecisionDate,AppealStatus,AppealDecision"
)

# For the exact-Eircode match (get_planning_applications' `site_match`).
# Confirmed live: only ~2,528 of ~390,773 records nationally (0.6%) have
# DevelopmentPostcode populated at all, and it's inconsistently formatted
# where it is (spaced "D24 VE22", unspaced "D24VE22", and at least one
# garbage value that was just "14") — so this is a bonus confirmation on
# top of the radius search below, never the primary source.
EIRCODE_PATTERN = re.compile(r"^[A-Z0-9]{3} ?[A-Z0-9]{4}$")


def _eircode_where_clause(field: str, eircode: str) -> Optional[str]:
    """Builds a `WHERE field = 'X' OR field = 'Y'` clause matching both the
    spaced and unspaced form of an Eircode. Returns None if `eircode`
    doesn't look like a real one — defensive, since this becomes a raw SQL
    WHERE clause (this backend doesn't support parameter binding, and
    doesn't support SQL functions like REPLACE/UPPER either — tested, got
    a 400 "invalid query parameters" — hence building literal OR candidates
    instead of normalising server-side).
    """
    normalised = (eircode or "").strip().upper()
    if not EIRCODE_PATTERN.match(normalised):
        return None
    unspaced = normalised.replace(" ", "")
    spaced = f"{unspaced[:3]} {unspaced[3:]}"
    escape = lambda s: s.replace("'", "''")
    return f"{field} = '{escape(spaced)}' OR {field} = '{escape(unspaced)}'"

# OPW's CFRAM predictive flood-extent layers, current climate scenario only
# (future-scenario and depth-grid layers exist too — see floodmap.js on
# floodinfo.ie — not wired in here to keep this to one clear headline
# number per hazard type, matching OPW's own High/Medium/Low framing).
# AEP = Annual Exceedance Probability. Bands per OPW's published thresholds:
# fluvial High/Medium/Low = 10% / 1% / 0.1% AEP; coastal = 10% / 0.5% / 0.1%.
FLOOD_LAYERS = [
    ("fluvial", "High", "esds_floodmaps:ext_f_c_0010", "Fluvial (river), 10% AEP — ~1-in-10-year event"),
    ("fluvial", "Medium", "esds_floodmaps:ext_f_c_0100", "Fluvial (river), 1% AEP — ~1-in-100-year event"),
    ("fluvial", "Low", "esds_floodmaps:ext_f_c_1000", "Fluvial (river), 0.1% AEP — ~1-in-1000-year event"),
    ("coastal", "High", "esds_floodmaps:ext_c_c_0010", "Coastal, 10% AEP — ~1-in-10-year event"),
    ("coastal", "Medium", "esds_floodmaps:ext_c_c_0200", "Coastal, 0.5% AEP — ~1-in-200-year event"),
    ("coastal", "Low", "esds_floodmaps:ext_c_c_1000", "Coastal, 0.1% AEP — ~1-in-1000-year event"),
]
BAND_RANK = {"High": 3, "Medium": 2, "Low": 1}


def get_planning_links(lat: float, lon: float) -> dict:
    log.info("Zoning and radon are link-outs, not live queries — see module docstring")
    return {
        "myplan_zoning": "https://www.myplan.ie",
        "epa_radon_risk_map": "https://www.epa.ie/environment-and-you/radon/radon-map/",
        "opw_flood_maps": "https://www.floodinfo.ie",
        "note": "Zoning designations and radon risk still need a manual map-viewer check. "
                "Radon barriers may be a building-regulation requirement depending on risk category.",
    }


def _epoch_ms_to_date(value) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _extract_application(attrs: dict, geom: dict) -> dict:
    return {
        "application_number": attrs.get("ApplicationNumber"),
        "description": attrs.get("DevelopmentDescription"),
        "address": attrs.get("DevelopmentAddress"),
        "status": attrs.get("ApplicationStatus"),
        "decision": attrs.get("Decision"),
        "received_date": _epoch_ms_to_date(attrs.get("ReceivedDate")),
        "decision_date": _epoch_ms_to_date(attrs.get("DecisionDate")),
        "appeal_status": attrs.get("AppealStatus"),
        "appeal_decision": attrs.get("AppealDecision"),
        "lat": geom.get("y"),
        "lon": geom.get("x"),
    }


def get_planning_applications(lat: float, lon: float, eircode: Optional[str] = None) -> dict:
    """Two searches: an exact-Eircode match against this specific site
    (`site_match`, precise regardless of our coordinate imprecision — but
    only works when the application record has that field populated, see
    EIRCODE_PATTERN's docstring above), and a radius search around the
    geocoded point for everything nearby (the main, complete picture).
    """
    site_match = None
    where = _eircode_where_clause("DevelopmentPostcode", eircode) if eircode else None
    if where:
        log.info("Checking for planning applications with an exact Eircode match…")
        try:
            feats = attribute_query(
                PLANNING_APPLICATIONS_URL, where,
                out_fields=PLANNING_APPLICATIONS_OUT_FIELDS,
                return_geometry=True,
                result_record_count=25,
            )
            site_applications = [_extract_application(f["attributes"], f.get("geometry") or {}) for f in feats]
            log.info("-> %d application(s) with exact Eircode match", len(site_applications))
            site_match = {"application_count": len(site_applications), "applications": site_applications}
        except Exception as exc:
            log.warning("-> exact Eircode match query failed: %s", exc)

    log.info("Querying National Planning Application Database within %dm…", PLANNING_APPLICATIONS_SEARCH_RADIUS_M)
    data = point_query_full(
        PLANNING_APPLICATIONS_URL, lon, lat,
        out_fields=PLANNING_APPLICATIONS_OUT_FIELDS,
        distance_m=PLANNING_APPLICATIONS_SEARCH_RADIUS_M,
        return_geometry=True,
        result_record_count=25,
        order_by="ReceivedDate DESC",
    )
    feats = data.get("features", [])
    applications = [_extract_application(f["attributes"], f.get("geometry") or {}) for f in feats]
    exceeded = bool(data.get("exceededTransferLimit"))
    log.info(
        "-> %d planning application(s) within %dm%s",
        len(applications), PLANNING_APPLICATIONS_SEARCH_RADIUS_M, " (capped, more exist)" if exceeded else "",
    )

    note = (
        "Zoning designation isn't in this dataset — check myplan.ie's zoning map directly "
        "for the relevant local authority's development plan."
    )
    if eircode:
        note += (
            " Exact-Eircode matching only works when an application's own record has that field "
            "filled in — most historic records don't (roughly 0.6% nationally are populated), so "
            "the radius-based list is the complete picture; an Eircode match is a bonus "
            "confirmation on top of it, not the other way round."
        )

    return {
        "application_count": len(applications),
        "more_exist": exceeded,
        "applications": applications,
        "search_radius_m": PLANNING_APPLICATIONS_SEARCH_RADIUS_M,
        "site_match": site_match,
        "source": "National Planning Application Database (myplan.ie)",
        "note": note,
    }


def get_flood_risk(lat: float, lon: float) -> dict:
    log.info("Querying OPW flood-extent maps (fluvial + coastal, current climate)…")
    bands = {"fluvial": None, "coastal": None}
    features = []
    for hazard, band, layer, label in FLOOD_LAYERS:
        try:
            hits = wms.get_feature_info(layer, lon, lat)
        except Exception as exc:
            log.warning("-> %s (%s) query failed: %s", label, layer, exc)
            continue
        if not hits:
            continue
        current_rank = BAND_RANK.get(bands[hazard], 0)
        if BAND_RANK[band] > current_rank:
            bands[hazard] = band
        for hit in hits:
            features.append({
                "hazard": hazard,
                "band": band,
                "label": label,
                "geometry": hit["geometry"],
            })
    for hazard, band in bands.items():
        log.info("-> %s flood extent: %s", hazard, band or "not mapped at this point")
    return {
        "fluvial_probability": bands["fluvial"],
        "coastal_probability": bands["coastal"],
        "features": features,
        "source": "OPW CFRAM predictive flood-extent maps (floodinfo.ie)",
        "caveat": (
            "Indicative only, current-climate scenario, not a substitute for a site-specific "
            "Flood Risk Assessment. CFRAM studies don't cover every watercourse or coastline in "
            "Ireland — no result here means 'not mapped', not 'confirmed safe'. See floodinfo.ie "
            "for future-scenario and depth-grid layers not queried here."
        ),
    }
