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


def get_protected_sites(lat: float, lon: float) -> dict:
    """Two queries per designation type, mirroring heritage.get_smr_zone():
    an exact point-intersect (does the site fall inside this designation?)
    and a buffered search at SEARCH_RADIUS_M (every nearby designated area,
    for drawing as shaded regions on the map).
    """
    within = {}
    sites = []
    more_exist = False

    for key, layer_url, label in DESIGNATION_TYPES:
        log.info("Checking %s at the exact site point…", label)
        exact_feats = point_query(layer_url, lon, lat, out_fields=OUT_FIELDS)
        current_code = None
        if exact_feats:
            attrs = exact_feats[0]["attributes"]
            current_code = attrs.get("SITECODE")
            within[key] = _site_dict(key, label, attrs, contains_site=True)
        else:
            within[key] = None

        log.info("Querying %s within %dm (for map display)…", label, SEARCH_RADIUS_M)
        data = point_query_full(
            layer_url, lon, lat,
            out_fields=OUT_FIELDS,
            distance_m=SEARCH_RADIUS_M,
            return_geometry=True,
            result_record_count=25,
        )
        for f in data.get("features", []):
            attrs = f["attributes"]
            rings = f.get("geometry", {}).get("rings")
            sites.append(_site_dict(
                key, label, attrs, rings=rings,
                contains_site=(attrs.get("SITECODE") == current_code and current_code is not None),
            ))
        if data.get("exceededTransferLimit"):
            more_exist = True
            log.warning("-> %s: more sites exist within %dm than were returned", label, SEARCH_RADIUS_M)

    any_within = any(within.values())
    if any_within:
        log.info("-> Within: %s", ", ".join(v["type_label"] for v in within.values() if v))
    else:
        log.info("-> Not within any NPWS-designated area at this exact point")
    log.info("-> %d designated area(s) mapped within %dm%s", len(sites), SEARCH_RADIUS_M, " (capped, more exist)" if more_exist else "")

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
            "Not within a mapped SAC, SPA, NHA, or proposed NHA at this exact point. Proximity to "
            "one (see map) can still matter — Appropriate Assessment screening isn't strictly "
            "bounded by the designation's own boundary."
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
