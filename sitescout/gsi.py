"""Geology, subsoil, and groundwater — Geological Survey Ireland (GSI).

All endpoints below were verified live against gsi.geodata.gov.ie.
"""
from __future__ import annotations

import logging

from . import config
from .arcgis import point_query

log = logging.getLogger("sitescout.gsi")

BEDROCK_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Bedrock/"
    "IE_GSI_Bedrock_Geology_Datasets_100K_IE26_ITM/MapServer/3/query"
)
QUATERNARY_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Quaternary/"
    "IE_GSI_Quaternary_Sediments_50K_IE26_ITM/MapServer/0/query"
)
GROUNDWATER_VULN_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Groundwater/"
    "IE_GSI_Groundwater_Vulnerability_40K_IE26_ITM/MapServer/0/query"
)

VULN_LABELS = {
    "X": "Extreme (rock near surface / karst)",
    "E": "Extreme",
    "H": "High",
    "M": "Moderate",
    "L": "Low",
}


def get_geology(lat: float, lon: float) -> dict:
    log.info("Querying GSI bedrock geology…")
    bedrock = point_query(BEDROCK_URL, lon, lat)
    log.info("Querying GSI Quaternary sediments (subsoil)…")
    sediment = point_query(QUATERNARY_URL, lon, lat)

    b = bedrock[0]["attributes"] if bedrock else None
    s = sediment[0]["attributes"] if sediment else None

    bedrock_unit = (b.get("UNIT_NAME") or b.get("DESCRIPT")) if b else None
    subsoil = s.get("LEGENDDESC") if s else None

    log.info("-> Bedrock: %s", bedrock_unit or "no data at this exact point")
    log.info("-> Subsoil: %s", subsoil or "no data at this exact point")

    return {
        "bedrock_unit": bedrock_unit,
        "bedrock_description": b.get("DESCRIPT") if b else None,
        "subsoil_type": subsoil,
        "source": "Geological Survey Ireland (GSI), gsi.ie",
    }


def get_groundwater(lat: float, lon: float) -> dict:
    log.info("Querying GSI groundwater vulnerability…")
    feats = point_query(GROUNDWATER_VULN_URL, lon, lat)
    v = feats[0]["attributes"] if feats else None
    category_code = v.get("VUL_CAT") if v else None
    category = VULN_LABELS.get(category_code, v.get("VUL_DESC") if v else None)
    log.info("-> Groundwater vulnerability: %s", category or "no data at this exact point")
    return {
        "vulnerability_category": category,
        "vulnerability_code": category_code,
        "source": "GSI Groundwater Vulnerability map (1:40,000); see also gwlevel.ie and floodinfo.ie",
    }
