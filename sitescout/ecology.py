"""Ecology & nature conservation designations — NPWS Designated Areas
(Special Areas of Conservation, Special Protection Areas, Natural Heritage
Areas, and proposed Natural Heritage Areas), queried within a radius of
the site.

Found by pulling apart NPWS's own public map viewer ("NPWS Designations
Viewer", dahg.maps.arcgis.com/apps/webappviewer) the same way as the
National Monuments Service and OPW endpoints elsewhere in this app — see
CLAUDE.md for the full writeup. All four designation types live as
separate layers on one FeatureServer — unlike protected structures (RPS),
which are published per-local-authority, this is a single national
dataset, confirmed live at North Bull Island, Dublin (SAC + SPA both
correctly returned).

This is arguably the single most consequential category in the whole
report: development affecting a SAC or SPA (even from outside its
boundary) can trigger a legal requirement for Appropriate Assessment under
the EU Habitats/Birds Directives — a much harder constraint than most of
what else this app reports.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import boundary
from .arcgis import point_query, point_query_full

log = logging.getLogger("sitescout.ecology")

BASE_URL = (
    "https://services-eu1.arcgis.com/Jhij7i46ouO8Cc0N/arcgis/rest/services/"
    "NPWSDesignatedAreas/FeatureServer"
)

# (key, layer url, human label) — layer indices confirmed live against the
# NPWS Designations Viewer's own web map.
DESIGNATION_TYPES = [
    ("sac", f"{BASE_URL}/3/query", "Special Area of Conservation (SAC)"),
    ("spa", f"{BASE_URL}/0/query", "Special Protection Area (SPA)"),
    ("nha", f"{BASE_URL}/2/query", "Natural Heritage Area (NHA)"),
    ("pnha", f"{BASE_URL}/1/query", "proposed Natural Heritage Area (pNHA)"),
]

OUT_FIELDS = "SITECODE,SITE_NAME,HA,URL"
SEARCH_RADIUS_M = 2000


def _site_dict(key: str, label: str, attrs: dict, rings=None, contains_site: bool = False) -> dict:
    return {
        "type": key,
        "type_label": label,
        "site_code": attrs.get("SITECODE"),
        "site_name": attrs.get("SITE_NAME"),
        "area_hectares": round(attrs["HA"], 1) if attrs.get("HA") else None,
        "url": attrs.get("URL"),
        "polygon_rings_wgs84": rings,
        "contains_site": contains_site,
    }


def get_protected_sites(lat: float, lon: float, boundary_ring_sets_wgs84: Optional[list] = None) -> dict:
    """Two queries per designation type, mirroring heritage.get_smr_zone():
    an exact point-intersect (does the site fall inside this designation?)
    and a buffered search at SEARCH_RADIUS_M (every nearby designated area,
    for drawing as shaded regions on the map).

    When `boundary_ring_sets_wgs84` (the CONFIRMED plot boundary — see
    boundary.py) is given, "within this designation type" is decided by a
    real polygon-vs-polygon overlap test against every candidate returned
    by the buffered search, instead of a single point — same class of fix
    as heritage.get_smr_zone(), applied here since this is arguably the
    single most consequential category in the whole report (see this
    module's own docstring) and deserves the same correctness. Falls back
    to the original point-only check when no boundary is available (CLI,
    or the web UI's own pre-confirmation fetch).
    """
    within = {}
    sites = []
    more_exist = False
    search_radius_m = (
        boundary.radius_covering_m(lat, lon, boundary_ring_sets_wgs84, SEARCH_RADIUS_M)
        if boundary_ring_sets_wgs84 else SEARCH_RADIUS_M
    )

    for key, layer_url, label in DESIGNATION_TYPES:
        log.info("Querying %s within %dm (for map display + overlap check)…", label, search_radius_m)
        data = point_query_full(
            layer_url, lon, lat,
            out_fields=OUT_FIELDS,
            distance_m=search_radius_m,
            return_geometry=True,
            result_record_count=25,
        )
        feats = data.get("features", [])

        if boundary_ring_sets_wgs84:
            overlapping_idx = boundary.find_overlapping(
                boundary_ring_sets_wgs84,
                [(i, [f.get("geometry", {}).get("rings")]) for i, f in enumerate(feats)],
            ) or set()
            current_code = None
            for i in overlapping_idx:
                current_code = feats[i]["attributes"].get("SITECODE")
                break  # just need ONE for the singular `within[key]` summary below; every overlapping site is still marked individually
        else:
            log.info("Checking %s at the exact site point…", label)
            exact_feats = point_query(layer_url, lon, lat, out_fields=OUT_FIELDS)
            current_code = exact_feats[0]["attributes"].get("SITECODE") if exact_feats else None
            overlapping_idx = (
                {i for i, f in enumerate(feats) if f["attributes"].get("SITECODE") == current_code}
                if current_code else set()
            )

        within[key] = _site_dict(key, label, feats[next(iter(overlapping_idx))]["attributes"], contains_site=True) if overlapping_idx else None

        for i, f in enumerate(feats):
            attrs = f["attributes"]
            rings = f.get("geometry", {}).get("rings")
            sites.append(_site_dict(key, label, attrs, rings=rings, contains_site=(i in overlapping_idx)))
        if data.get("exceededTransferLimit"):
            more_exist = True
            log.warning("-> %s: more sites exist within %dm than were returned", label, search_radius_m)

    any_within = any(within.values())
    if any_within:
        log.info("-> Within: %s", ", ".join(v["type_label"] for v in within.values() if v))
    else:
        log.info("-> Not within any NPWS-designated area")
    log.info("-> %d designated area(s) mapped within %dm%s", len(sites), search_radius_m, " (capped, more exist)" if more_exist else "")

    if any_within:
        names = "; ".join(f"{v['type_label']} — {v['site_name']} ({v['site_code']})" for v in within.values() if v)
        caveat = (
            f"This site falls within: {names}. Development affecting a SAC or SPA — even from "
            "outside its boundary — can trigger a legal requirement for Appropriate Assessment "
            "under the EU Habitats/Birds Directives. NHAs are protected under Irish law (Wildlife "
            "Acts); proposed NHAs aren't yet statutorily protected but are an established planning "
            "policy consideration. Get a professional ecological assessment before proceeding."
        )
    else:
        caveat = (
            "Not within a mapped SAC, SPA, NHA, or proposed NHA" + (" at this exact point" if not boundary_ring_sets_wgs84 else "") + ". "
            "Proximity to one (see map) can still matter — Appropriate Assessment screening isn't "
            "strictly bounded by the designation's own boundary."
        )

    return {
        "any_within": any_within,
        "within": within,
        "site_count": len(sites),
        "more_exist": more_exist,
        "sites": sites,
        "source": "National Parks & Wildlife Service (NPWS) Designated Areas",
        "caveat": caveat,
    }
