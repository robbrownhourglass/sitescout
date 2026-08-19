"""Shared orchestration: given a resolved address + geocode result, query
all live data sources and compile the final report dict.

Extracted from `cli.py` so the CLI and the local web UI (`webapp.py`) run
the exact same sequence of lookups and can't drift out of sync — both call
`run()` after they've each done their own `autoaddress` + `geocode` step
(which differ: the CLI can block on stdin to disambiguate, the web UI
can't).
"""
from __future__ import annotations

import logging

from . import cadastral, ecology, gsi, heritage, utilities, planning, report

log = logging.getLogger("sitescout.pipeline")


def run(query: str, resolved, geo) -> dict:
    sections = {}

    try:
        sections["boundary"] = cadastral.get_boundary(geo.lat, geo.lon)
    except Exception as exc:
        log.error("Cadastral boundary lookup failed: %s", exc)

    try:
        sections["geology"] = gsi.get_geology(geo.lat, geo.lon)
    except Exception as exc:
        log.error("Geology lookup failed: %s", exc)

    try:
        sections["groundwater"] = gsi.get_groundwater(geo.lat, geo.lon)
    except Exception as exc:
        log.error("Groundwater lookup failed: %s", exc)

    try:
        sections["archaeology"] = heritage.get_archaeology(geo.lat, geo.lon)
    except Exception as exc:
        log.error("Archaeology lookup failed: %s", exc)

    try:
        sections["smr_zone"] = heritage.get_smr_zone(geo.lat, geo.lon)
    except Exception as exc:
        log.error("SMR Zone lookup failed: %s", exc)

    try:
        sections["niah"] = heritage.get_niah(geo.lat, geo.lon)
    except Exception as exc:
        log.error("NIAH lookup failed: %s", exc)

    try:
        sections["planning_applications"] = planning.get_planning_applications(geo.lat, geo.lon)
    except Exception as exc:
        log.error("Planning application lookup failed: %s", exc)

    try:
        sections["flood_risk"] = planning.get_flood_risk(geo.lat, geo.lon)
    except Exception as exc:
        log.error("Flood risk lookup failed: %s", exc)

    try:
        sections["ecology"] = ecology.get_protected_sites(geo.lat, geo.lon)
    except Exception as exc:
        log.error("Ecology (NPWS designated areas) lookup failed: %s", exc)

    sections["utilities"] = utilities.draft_requests(geo.lat, geo.lon, geo.label)
    sections["planning"] = planning.get_planning_links(geo.lat, geo.lon)

    return report.build_report(query, resolved, geo, sections)
