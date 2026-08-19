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

from . import cadastral, ecology, gsi, heritage, rps, utilities, planning, report

log = logging.getLogger("sitescout.pipeline")


def _attach_boundaries(planning_applications: dict) -> None:
    """Mutates `planning_applications` in place, adding a `boundary` key
    (cadastral.get_boundary()'s shape, or None) to every application in
    both the radius search and the exact-Eircode match — the property
    boundary for that specific application's own site, not the searched
    site. Looked up concurrently (see cadastral.get_boundaries_for_points)
    since there can be dozens of applications in one report.
    """
    all_apps = list(planning_applications.get("applications", []))
    if planning_applications.get("site_match"):
        all_apps += planning_applications["site_match"].get("applications", [])
    points = [(a["lat"], a["lon"]) for a in all_apps if a.get("lat") is not None and a.get("lon") is not None]
    if not points:
        return
    boundaries = cadastral.get_boundaries_for_points(points)
    for a in all_apps:
        if a.get("lat") is not None and a.get("lon") is not None:
            a["boundary"] = boundaries.get((a["lat"], a["lon"]))


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
        sections["planning_applications"] = planning.get_planning_applications(geo.lat, geo.lon, resolved.eircode)
        _attach_boundaries(sections["planning_applications"])
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

    try:
        sections["rps_aca"] = rps.get_protected_structures(geo.lat, geo.lon)
    except Exception as exc:
        log.error("RPS/ACA lookup failed: %s", exc)

    sections["utilities"] = utilities.draft_requests(geo.lat, geo.lon, geo.label)
    sections["planning"] = planning.get_planning_links(geo.lat, geo.lon)

    return report.build_report(query, resolved, geo, sections)
