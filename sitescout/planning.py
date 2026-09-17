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
  fluvial, coastal, and pluvial (surface water), at three probability
  bands each, across current-climate plus mid/high-end future-climate
  scenarios (fluvial/coastal only — pluvial has no future scenario in this
  dataset). Different query shape (WMS `GetFeatureInfo`, not an ArcGIS
  point query) — see `wms.py`.

Zoning specifically (as opposed to planning application history) isn't in
the NPAD dataset — Ireland's ~31 local authorities each publish their own
zoning maps, no single national layer was found. That stays a link-out.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
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

# OPW's CFRAM predictive flood-extent layers. Found the full layer set by
# querying floodinfo.ie's own GeoServer GetCapabilities directly (simpler
# than re-deriving it from JS a second time) — it lists far more than the
# current-climate-only set this app originally used:
#   ext_{hazard}_{scenario}_{AEP} — hazard: f=fluvial, c=coastal, p=pluvial
#   (surface water, a genuinely different flood mechanism, not previously
#   covered at all); scenario: c=current climate, m=mid-range future,
#   h=high-end future. Pluvial only has a current-climate scenario in this
#   dataset (no ext_p_m_*/ext_p_h_* exist). Confirmed live: mid/high-future
#   fluvial layers hit at Fermoy (a known flood-prone town) even where
#   current-climate doesn't extend as far; pluvial hit in Dublin city
#   centre; coastal mid-future hit at Cork city centre.
# AEP = Annual Exceedance Probability. Bands per OPW's published thresholds:
# fluvial High/Medium/Low = 10% / 1% / 0.1% AEP; coastal = 10% / 0.5% / 0.1%;
# pluvial uses the same 10% / 1% / 0.1% convention as fluvial.
FLOOD_LAYERS = [
    ("fluvial", "current", "High", "esds_floodmaps:ext_f_c_0010", "Fluvial (river), current climate, 10% AEP — ~1-in-10-year event"),
    ("fluvial", "current", "Medium", "esds_floodmaps:ext_f_c_0100", "Fluvial (river), current climate, 1% AEP — ~1-in-100-year event"),
    ("fluvial", "current", "Low", "esds_floodmaps:ext_f_c_1000", "Fluvial (river), current climate, 0.1% AEP — ~1-in-1000-year event"),
    ("coastal", "current", "High", "esds_floodmaps:ext_c_c_0010", "Coastal, current climate, 10% AEP — ~1-in-10-year event"),
    ("coastal", "current", "Medium", "esds_floodmaps:ext_c_c_0200", "Coastal, current climate, 0.5% AEP — ~1-in-200-year event"),
    ("coastal", "current", "Low", "esds_floodmaps:ext_c_c_1000", "Coastal, current climate, 0.1% AEP — ~1-in-1000-year event"),
    ("fluvial", "mid_future", "High", "esds_floodmaps:ext_f_m_0010", "Fluvial (river), mid-range future climate, 10% AEP"),
    ("fluvial", "mid_future", "Medium", "esds_floodmaps:ext_f_m_0100", "Fluvial (river), mid-range future climate, 1% AEP"),
    ("fluvial", "mid_future", "Low", "esds_floodmaps:ext_f_m_1000", "Fluvial (river), mid-range future climate, 0.1% AEP"),
    ("fluvial", "high_future", "High", "esds_floodmaps:ext_f_h_0010", "Fluvial (river), high-end future climate, 10% AEP"),
    ("fluvial", "high_future", "Medium", "esds_floodmaps:ext_f_h_0100", "Fluvial (river), high-end future climate, 1% AEP"),
    ("fluvial", "high_future", "Low", "esds_floodmaps:ext_f_h_1000", "Fluvial (river), high-end future climate, 0.1% AEP"),
    ("coastal", "mid_future", "High", "esds_floodmaps:ext_c_m_0010", "Coastal, mid-range future climate, 10% AEP"),
    ("coastal", "mid_future", "Medium", "esds_floodmaps:ext_c_m_0200", "Coastal, mid-range future climate, 0.5% AEP"),
    ("coastal", "mid_future", "Low", "esds_floodmaps:ext_c_m_1000", "Coastal, mid-range future climate, 0.1% AEP"),
    ("coastal", "high_future", "High", "esds_floodmaps:ext_c_h_0010", "Coastal, high-end future climate, 10% AEP"),
    ("coastal", "high_future", "Medium", "esds_floodmaps:ext_c_h_0200", "Coastal, high-end future climate, 0.5% AEP"),
    ("coastal", "high_future", "Low", "esds_floodmaps:ext_c_h_1000", "Coastal, high-end future climate, 0.1% AEP"),
    ("pluvial", "current", "High", "esds_floodmaps:ext_p_c_0010", "Pluvial (surface water), current climate, 10% AEP"),
    ("pluvial", "current", "Medium", "esds_floodmaps:ext_p_c_0100", "Pluvial (surface water), current climate, 1% AEP"),
    ("pluvial", "current", "Low", "esds_floodmaps:ext_p_c_1000", "Pluvial (surface water), current climate, 0.1% AEP"),
]
BAND_RANK = {"High": 3, "Medium": 2, "Low": 1}
HAZARDS = ("fluvial", "coastal", "pluvial")
SCENARIOS = ("current", "mid_future", "high_future")


def get_planning_links(lat: float, lon: float) -> dict:
    # Radon used to be a link-out here too — it's live now, see epa.py's
    # get_radon_risk() / the "Environmental hazards (EPA)" section. Left
    # here as a stray duplicate link once, which was confusing (looked
    # like radon was still link-out-only when real data existed elsewhere)
    # — don't re-add it.
    log.info("Zoning is a link-out, not a live query — see module docstring")
    return {
        "myplan_zoning": "https://www.myplan.ie",
        "opw_flood_maps": "https://www.floodinfo.ie",
        "note": "Zoning designation still needs a manual map-viewer check.",
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
    """21 WMS layers total (see FLOOD_LAYERS) — up from the original 6
    (current-climate fluvial+coastal only) once pluvial and future-climate
    scenarios were added. Queried concurrently (ThreadPoolExecutor, same
    pattern as pipeline.run()/cadastral.get_boundaries_for_points()) rather
    than the old sequential loop, to keep this one section's response time
    reasonable now that it's 3.5x the layer count.
    """
    log.info("Querying OPW flood-extent maps (%d layers: fluvial/coastal/pluvial x current/mid-future/high-future)…", len(FLOOD_LAYERS))

    def _query(spec):
        hazard, scenario, band, layer, label = spec
        try:
            hits = wms.get_feature_info(layer, lon, lat)
        except Exception as exc:
            log.warning("-> %s (%s) query failed: %s", label, layer, exc)
            return spec, []
        return spec, hits

    # bands[scenario][hazard] -> highest band hit for that scenario+hazard
    bands = {scenario: {hazard: None for hazard in HAZARDS} for scenario in SCENARIOS}
    features = []
    with ThreadPoolExecutor(max_workers=len(FLOOD_LAYERS)) as executor:
        for spec, hits in executor.map(_query, FLOOD_LAYERS):
            hazard, scenario, band, layer, label = spec
            if not hits:
                continue
            current_rank = BAND_RANK.get(bands[scenario][hazard], 0)
            if BAND_RANK[band] > current_rank:
                bands[scenario][hazard] = band
            for hit in hits:
                features.append({
                    "hazard": hazard, "scenario": scenario, "band": band,
                    "label": label, "geometry": hit["geometry"],
                })

    for scenario in SCENARIOS:
        for hazard in HAZARDS:
            log.info("-> %s / %s: %s", scenario, hazard, bands[scenario][hazard] or "not mapped at this point")

    current = bands["current"]
    return {
        "fluvial_probability": current["fluvial"],
        "coastal_probability": current["coastal"],
        "pluvial_probability": current["pluvial"],
        "future_scenarios": {
            "mid_future": {"fluvial_probability": bands["mid_future"]["fluvial"], "coastal_probability": bands["mid_future"]["coastal"]},
            "high_future": {"fluvial_probability": bands["high_future"]["fluvial"], "coastal_probability": bands["high_future"]["coastal"]},
        },
        "features": features,
        "source": "OPW CFRAM predictive flood-extent maps (floodinfo.ie)",
        "caveat": (
            "Indicative only, not a substitute for a site-specific Flood Risk Assessment. CFRAM "
            "studies don't cover every watercourse or coastline in Ireland — no result here means "
            "'not mapped', not 'confirmed safe'. Pluvial (surface water) flooding only has a "
            "current-climate scenario in this dataset; fluvial/coastal future scenarios (mid-range "
            "and high-end climate change) are separate from and typically larger than the "
            "current-climate extent shown as the headline figure."
        ),
    }
