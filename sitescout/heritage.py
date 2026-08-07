"""Archaeology & heritage — National Monuments Service Sites and Monuments
Record (SMR), SMR Zones (archaeological notification zones), and the
National Inventory of Architectural Heritage (NIAH) — all queried within a
radius of the site.

NIAH: the documented endpoint at webservices.npws.ie didn't resolve from
this dev sandbox (see CLAUDE.md). Found a different, working one instead by
pulling apart the National Monuments Service's own public map viewer
("Historic Environment Viewer", heritagedata.maps.arcgis.com) — it's on the
same ArcGIS org as the SMR layer below, confirmed live the same way.

SMR Zone: a polygon layer from that same viewer marking the area around
each recorded monument within which the Minister must be given notice
before any ground disturbance (National Monuments Acts) — distinct from,
and usually larger than, the monument point itself.
"""
from __future__ import annotations

import logging

from .arcgis import point_query, point_query_full

log = logging.getLogger("sitescout.heritage")

SMR_URL = (
    "https://services-eu1.arcgis.com/HyjXgkV6KGMSF3jt/arcgis/rest/services/"
    "SMROpenData/FeatureServer/0/query"
)
SMR_ZONE_URL = (
    "https://services-eu1.arcgis.com/HyjXgkV6KGMSF3jt/arcgis/rest/services/"
    "SMRZone/FeatureServer/0/query"
)
NIAH_URL = (
    "https://services-eu1.arcgis.com/HyjXgkV6KGMSF3jt/arcgis/rest/services/"
    "NIAHBuildings/FeatureServer/0/query"
)

SEARCH_RADIUS_M = 2000
NIAH_SEARCH_RADIUS_M = 500


def get_archaeology(lat: float, lon: float) -> dict:
    log.info("Querying National Monuments Service SMR within %dm…", SEARCH_RADIUS_M)
    data = point_query_full(
        SMR_URL, lon, lat,
        out_fields="SMRS,MONUMENT_CLASS,COUNTY",
        distance_m=SEARCH_RADIUS_M,
        return_geometry=True,
    )
    feats = data.get("features", [])
    monuments = [
        {
            "smr_ref": f["attributes"].get("SMRS"),
            "class": f["attributes"].get("MONUMENT_CLASS"),
            "county": f["attributes"].get("COUNTY"),
            "lat": (f.get("geometry") or {}).get("y"),
            "lon": (f.get("geometry") or {}).get("x"),
        }
        for f in feats
    ]
    exceeded = bool(data.get("exceededTransferLimit"))
    log.info(
        "-> %d recorded monument(s) within %dm%s",
        len(monuments), SEARCH_RADIUS_M, " (capped, more exist)" if exceeded else "",
    )
    for m in monuments[:6]:
        log.info("   - %s (%s)", m["class"], m["smr_ref"])
    return {
        "monument_count": len(monuments),
        "more_exist": exceeded,
        "monuments": monuments,
        "source": "National Monuments Service Sites & Monuments Record (SMR)",
        "also_check": [
            "Heritage Maps — https://www.heritagemaps.ie",
        ],
    }


def get_smr_zone(lat: float, lon: float) -> dict:
    """Two queries here, deliberately: an exact point-intersect (does the
    site itself fall inside a zone?) and a buffered search at the same
    radius as `get_archaeology()` (every zone in the vicinity, for drawing
    as shaded areas on the map — matching how the National Monuments
    Service's own viewer shows them, not just a single point-in-polygon
    fact).
    """
    log.info("Checking SMR Zone at the exact site point…")
    exact_feats = point_query(SMR_ZONE_URL, lon, lat, out_fields="ZONE_ID,Shape__Area")
    in_zone = bool(exact_feats)
    current_zone_id = exact_feats[0]["attributes"].get("ZONE_ID") if exact_feats else None
    current_area_ha = None
    if exact_feats:
        area_m2 = exact_feats[0]["attributes"].get("Shape__Area")
        current_area_ha = round(area_m2 / 10000, 2) if area_m2 else None

    log.info("Querying SMR Zones within %dm (for map display)…", SEARCH_RADIUS_M)
    data = point_query_full(
        SMR_ZONE_URL, lon, lat,
        out_fields="ZONE_ID,Shape__Area",
        distance_m=SEARCH_RADIUS_M,
        return_geometry=True,
        result_record_count=50,
    )
    nearby_feats = data.get("features", [])
    zones = []
    for z in nearby_feats:
        area_m2 = z["attributes"].get("Shape__Area")
        zones.append({
            "zone_id": z["attributes"].get("ZONE_ID"),
            "area_hectares": round(area_m2 / 10000, 2) if area_m2 else None,
            "polygon_rings_wgs84": z.get("geometry", {}).get("rings"),
            "contains_site": z["attributes"].get("ZONE_ID") == current_zone_id and in_zone,
        })
    more_exist = bool(data.get("exceededTransferLimit"))

    if in_zone:
        log.info(
            "-> Within SMR Zone %s (~%.2f ha); %d zone(s)%s mapped within %dm",
            current_zone_id, current_area_ha or 0, len(zones),
            "+" if more_exist else "", SEARCH_RADIUS_M,
        )
    else:
        log.info(
            "-> Not within a mapped SMR Zone; %d zone(s)%s nearby within %dm",
            len(zones), "+" if more_exist else "", SEARCH_RADIUS_M,
        )

    return {
        "in_zone": in_zone,
        "zone_id": current_zone_id,
        "area_hectares": current_area_ha,
        "zone_count": len(zones),
        "more_exist": more_exist,
        "zones": zones,
        "source": "National Monuments Service SMR Zones",
        "caveat": (
            "This site falls within an SMR Zone — the area around a recorded monument in which "
            "the National Monuments Service must be given notice (min. 2 months) before any "
            "ground disturbance, under the National Monuments Acts."
        ) if in_zone else "Not within a mapped SMR Zone at this exact point.",
    }


def get_niah(lat: float, lon: float) -> dict:
    log.info("Querying NIAH (protected structures) within %dm…", NIAH_SEARCH_RADIUS_M)
    data = point_query_full(
        NIAH_URL, lon, lat,
        out_fields="REG_NO,NAME,RATING,APPRAISAL,NUMBER,STREET1,TOWN,COUNTY",
        distance_m=NIAH_SEARCH_RADIUS_M,
        return_geometry=True,
        result_record_count=25,
    )
    feats = data.get("features", [])
    structures = []
    for f in feats:
        a = f["attributes"]
        geom = f.get("geometry") or {}
        address = ", ".join(p for p in [a.get("NUMBER"), a.get("STREET1"), a.get("TOWN"), a.get("COUNTY")] if p)
        appraisal = a.get("APPRAISAL") or ""
        structures.append({
            "reg_no": a.get("REG_NO"),
            "name": a.get("NAME"),
            "rating": a.get("RATING"),
            "address": address,
            "appraisal": (appraisal[:300] + "…") if len(appraisal) > 300 else appraisal,
            "lat": geom.get("y"),
            "lon": geom.get("x"),
        })
    exceeded = bool(data.get("exceededTransferLimit"))
    log.info(
        "-> %d NIAH structure(s) within %dm%s",
        len(structures), NIAH_SEARCH_RADIUS_M, " (capped, more exist)" if exceeded else "",
    )
    return {
        "structure_count": len(structures),
        "more_exist": exceeded,
        "structures": structures,
        "source": "National Inventory of Architectural Heritage (NIAH), buildingsofireland.ie",
        "caveat": "NIAH rating is an architectural heritage assessment, not a statutory protection "
                  "in itself — check the local authority's Record of Protected Structures (RPS) directly.",
    }
