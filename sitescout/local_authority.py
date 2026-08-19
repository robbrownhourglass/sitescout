"""Resolves a point to its local authority (city/county council).

Needed because RPS and ACA (rps.py) are published per-local-authority, not
nationally, unlike everything else in this app — a query needs to be
routed to the right council's own dataset.

Uses Tailte Éireann's own official administrative boundaries — same
ArcGIS org as the cadastral parcels already used elsewhere in this app.
Confirmed live, and confirmed to correctly distinguish adjacent
authorities (South Dublin vs Fingal, tested at Tallaght vs Swords).

Deliberately NOT using the "LocalAuthorityBoundaries" FeatureServer found
via myplan.ie's own web map (services.arcgis.com/NzlPQPKn5QF9v2US) — tried
it first, and it's missing whole counties (a 20km buffer around Tallaght,
Co. Dublin returned only "Wicklow County", nothing for any Dublin
authority). Don't reuse that one.
"""
from __future__ import annotations

import logging
from typing import Optional

from .arcgis import point_query

log = logging.getLogger("sitescout.local_authority")

ADMIN_AREAS_URL = (
    "https://services-eu1.arcgis.com/FH5XCsx8rYXqnjF5/arcgis/rest/services/"
    "Administrative_Areas___OSi_National_Statutory_Boundaries/FeatureServer/0/query"
)


def get_local_authority_raw(lat: float, lon: float) -> Optional[str]:
    """Returns the local authority name exactly as Tailte Éireann has it
    (all-caps, e.g. "SOUTH DUBLIN COUNTY COUNCIL") — this is the form
    `rps.SOURCES` is keyed by. Use `get_local_authority()` for a
    display-friendly version.
    """
    log.info("Resolving local authority…")
    feats = point_query(ADMIN_AREAS_URL, lon, lat, out_fields="ENGLISH")
    if not feats:
        log.warning("-> No local authority polygon found at this point")
        return None
    name = feats[0]["attributes"].get("ENGLISH")
    log.info("-> %s", name)
    return name


def get_local_authority(lat: float, lon: float) -> Optional[str]:
    """Display-friendly version, e.g. "South Dublin County Council"."""
    raw = get_local_authority_raw(lat, lon)
    return raw.title() if raw else None
