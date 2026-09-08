"""Geohazards & aquifer — landslide susceptibility, bedrock/sand-gravel
aquifer classification, karst features, and groundwater source protection
areas. All four come off the same GSI ArcGIS server (gsi.geodata.gov.ie)
already used in gsi.py for bedrock/subsoil/groundwater vulnerability.

Found via a simpler route than most other sources in this app (no need to
pull apart a WebAppViewer's JS) — gsi.geodata.gov.ie's own ArcGIS REST
catalogue lists every service folder directly at
`/server/rest/services?f=json`. Sibling folders to the ones gsi.py already
uses (Bedrock, Quaternary, Groundwater) include `Geohazards` and more of
`Groundwater` than gsi.py taps — confirmed live by listing those folders'
own `?f=json` and then each service's layer schema.

Landslide susceptibility and both aquifer layers are point-in-polygon
classifications with national coverage (there's always a bedrock aquifer
category and a landslide class at any point on land; sand/gravel aquifers
only exist in some areas, so that one is a bonus). Karst features and
groundwater source protection areas are radius searches — same "list
what's nearby, flag what's directly underneath the site" pattern as
ecology.py / heritage.get_smr_zone().
"""
from __future__ import annotations

import logging

from .arcgis import point_query, point_query_full

log = logging.getLogger("sitescout.geohazards")

LANDSLIDE_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Geohazards/"
    "IE_GSI_Landslide_Susceptibility_Classification_50K_IE26_ITM/MapServer/0/query"
)
BEDROCK_AQUIFER_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Groundwater/"
    "IE_GSI_Aquifer_Datasets_IE26_ITM/MapServer/2/query"
)
SAND_GRAVEL_AQUIFER_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Groundwater/"
    "IE_GSI_Aquifer_Datasets_IE26_ITM/MapServer/0/query"
)
KARST_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Groundwater/"
    "IE_GSI_Karst_Datasets_40K_IE32_ITM/MapServer/0/query"
)
SOURCE_PROTECTION_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Groundwater/"
    "IE_GSI_Group_Water_Scheme_Public_Water_Supply_Source_Protection_Areas_20K_IE26_ITM/MapServer/0/query"
)
GWS_ZOC_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Groundwater/"
    "IE_GSI_Group_Water_Scheme_Public_Water_Supply_Source_Protection_Areas_20K_IE26_ITM/MapServer/1/query"
)

KARST_SEARCH_RADIUS_M = 1000
SOURCE_PROTECTION_SEARCH_RADIUS_M = 2000

# Confirmed against the layer's own full domain (pulled every distinct
# LSSUSCLASS/LSSUSDESC pair live via an ArcGIS returnDistinctValues query)
# — GSI's landslide susceptibility mapping uses six natural classes plus
# two special non-susceptibility codes: "Made" (artificial/made ground —
# its own geotechnical flag, not a natural-slope rating) and "Water" (a
# water body — no land classification applies). The "(inferred)" suffix
# means the classification was interpolated from surrounding data rather
# than directly field-mapped at that spot — kept in the description text
# shown to the user, not folded into severity here.
LANDSLIDE_SEVERITY = {
    "High": "bad",
    "Moderately High": "warn",
    "Moderately Low": "ok",
    "Low": "ok",
    "Made": "warn",
    "Water": "ok",
}


def _landslide_severity(desc: str | None) -> str:
    base = (desc or "").replace(" (inferred)", "").strip()
    return LANDSLIDE_SEVERITY.get(base, "warn")  # unrecognised/missing -> treat as "unknown", not "safe"


def get_landslide_susceptibility(lat: float, lon: float) -> dict:
    log.info("Querying GSI landslide susceptibility…")
    feats = point_query(LANDSLIDE_URL, lon, lat)
    if not feats:
        log.info("-> No landslide susceptibility classification at this exact point")
        return {"found": False, "class_description": None, "severity": "warn"}
    attrs = feats[0]["attributes"]
    desc = attrs.get("LSSUSDESC")
    log.info("-> Landslide susceptibility: %s", desc)
    return {
        "found": True,
        "class_code": attrs.get("LSSUSCLASS"),
        "class_description": desc,
        "severity": _landslide_severity(desc),
    }


def get_aquifer(lat: float, lon: float) -> dict:
    log.info("Querying GSI bedrock aquifer classification…")
    bedrock_feats = point_query(BEDROCK_AQUIFER_URL, lon, lat)
    log.info("Querying GSI sand & gravel aquifer classification…")
    sg_feats = point_query(SAND_GRAVEL_AQUIFER_URL, lon, lat)

    b = bedrock_feats[0]["attributes"] if bedrock_feats else None
    sg = sg_feats[0]["attributes"] if sg_feats else None
    bedrock_cat = b.get("AQUIFERCAT") if b else None

    # Karstified aquifers (Rk/Rkc/Rkd — "conduit" or "diffuse" flow through
    # fissured limestone) mean very fast, poorly-filtered groundwater
    # movement: the classic pathway for contamination to spread quickly and
    # far. Relevant to on-site wastewater (septic) siting in particular —
    # confirmed by testing the Burren, Co. Clare, a well-known karst
    # landscape, which returned "Rkc" here.
    severity = "warn" if bedrock_cat and "Rk" in bedrock_cat else "ok"

    log.info("-> Bedrock aquifer: %s", b.get("AQUIFERDES") if b else "no data at this exact point")
    log.info("-> Sand & gravel aquifer: %s", sg.get("AQUIFERDES") if sg else "none at this point")

    return {
        "bedrock_aquifer_category": bedrock_cat,
        "bedrock_aquifer_description": b.get("AQUIFERDES") if b else None,
        "sand_gravel_aquifer_description": sg.get("AQUIFERDES") if sg else None,
        "severity": severity,
    }


def get_karst_features(lat: float, lon: float) -> dict:
    log.info("Querying GSI karst features within %dm…", KARST_SEARCH_RADIUS_M)
    # This layer never returns geometry via its query endpoint — confirmed
    # with returnGeometry=true, a plain where=1=1 query, and f=geojson, all
    # three still coming back with geometry: null despite the layer's own
    # schema advertising esriGeometryPoint. The X_ITM/Y_ITM attribute
    # fields do carry real coordinates, but they're Irish Transverse
    # Mercator (EPSG:2157) — reprojecting those by hand without a verified
    # transform (this app has no geospatial library) risks silently wrong
    # pins, so karst features are reported as a list only, no map markers.
    feats = point_query(KARST_URL, lon, lat, distance_m=KARST_SEARCH_RADIUS_M, result_record_count=25)
    features = [
        {"type": f["attributes"].get("KARST_TYPE"), "name": f["attributes"].get("KARST_NAME")}
        for f in feats
    ]
    log.info("-> %d karst feature(s) within %dm", len(features), KARST_SEARCH_RADIUS_M)
    for feat in features[:6]:
        log.info("   - %s%s", feat["type"], f" ({feat['name']})" if feat["name"] else "")
    return {
        "feature_count": len(features),
        "features": features,
        "search_radius_m": KARST_SEARCH_RADIUS_M,
        "severity": "warn" if features else "ok",
    }


def get_source_protection(lat: float, lon: float) -> dict:
    """Mirrors ecology.get_protected_sites()'s two-query shape: an exact
    point-intersect (is the site inside one?) plus a buffered search for
    everything nearby, to draw as shaded areas on the map. Public Water
    Supply Source Protection Areas and Group Water Scheme Zones of
    Contribution are two separate layers on this service but reported
    together — both describe the same underlying concern (development near
    the catchment of a drinking-water source).
    """
    out_fields = "SPA_NAME,SPA_CODE,COUNTY,REPORT_URL"
    log.info("Checking public water supply source protection areas at the exact site point…")
    exact_spa = point_query(SOURCE_PROTECTION_URL, lon, lat, out_fields=out_fields)
    exact_zoc = point_query(GWS_ZOC_URL, lon, lat, out_fields="GWS_NAME")

    in_spa = bool(exact_spa)
    in_zoc = bool(exact_zoc)
    exact_spa_code = exact_spa[0]["attributes"].get("SPA_CODE") if in_spa else None

    log.info("Querying source protection areas within %dm (for map display)…", SOURCE_PROTECTION_SEARCH_RADIUS_M)
    nearby_data = point_query_full(
        SOURCE_PROTECTION_URL, lon, lat,
        out_fields=out_fields,
        distance_m=SOURCE_PROTECTION_SEARCH_RADIUS_M,
        return_geometry=True,
        result_record_count=25,
    )
    nearby_areas = [
        {
            "name": f["attributes"].get("SPA_NAME"),
            "code": f["attributes"].get("SPA_CODE"),
            "county": f["attributes"].get("COUNTY"),
            "report_url": f["attributes"].get("REPORT_URL"),
            "polygon_rings_wgs84": f.get("geometry", {}).get("rings"),
            "contains_site": exact_spa_code is not None and f["attributes"].get("SPA_CODE") == exact_spa_code,
        }
        for f in nearby_data.get("features", [])
    ]

    log.info(
        "-> Within a public water supply source protection area: %s (%s); %d nearby within %dm",
        in_spa, exact_spa[0]["attributes"].get("SPA_NAME") if in_spa else "n/a",
        len(nearby_areas), SOURCE_PROTECTION_SEARCH_RADIUS_M,
    )

    return {
        "in_source_protection_area": in_spa,
        "source_protection_area_name": exact_spa[0]["attributes"].get("SPA_NAME") if in_spa else None,
        "source_protection_area_report_url": exact_spa[0]["attributes"].get("REPORT_URL") if in_spa else None,
        "in_group_water_scheme_zone": in_zoc,
        "group_water_scheme_name": exact_zoc[0]["attributes"].get("GWS_NAME") if in_zoc else None,
        "nearby_areas": nearby_areas,
        "nearby_count": len(nearby_areas),
        "search_radius_m": SOURCE_PROTECTION_SEARCH_RADIUS_M,
        "severity": "warn" if (in_spa or in_zoc) else "ok",
    }


def get_geohazards(lat: float, lon: float) -> dict:
    """One combined section — landslide susceptibility, aquifer
    classification, karst features, and groundwater source protection —
    mirroring ecology.py/epa.py's pattern of bundling several related
    layers behind one report section/sidebar tile rather than four
    separate ones.
    """
    landslide = get_landslide_susceptibility(lat, lon)
    aquifer = get_aquifer(lat, lon)
    karst = get_karst_features(lat, lon)
    source_protection = get_source_protection(lat, lon)

    severities = [landslide["severity"], aquifer["severity"], karst["severity"], source_protection["severity"]]
    overall_severity = "bad" if "bad" in severities else ("warn" if "warn" in severities else "ok")

    return {
        "landslide": landslide,
        "aquifer": aquifer,
        "karst": karst,
        "source_protection": source_protection,
        "overall_severity": overall_severity,
        "source": "Geological Survey Ireland (GSI), gsi.geodata.gov.ie",
    }
