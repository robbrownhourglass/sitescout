"""Shared orchestration: given a resolved address + geocode result, query
all live data sources and compile the final report dict.

Extracted from `cli.py` so the CLI and the local web UI (`webapp.py`) run
the exact same sequence of lookups and can't drift out of sync — both call
`run()` after they've each done their own `autoaddress` + `geocode` step
(which differ: the CLI can block on stdin to disambiguate, the web UI
can't).

Two entry points:
- `run()` — the full report in one call, used by the CLI (which has no
  plot-picker UI, so `boundary` here is always cadastral.get_boundary()'s
  single-parcel-at-the-point result). Runs every independent section
  concurrently — these lookups don't depend on each other, only on
  geo.lat/geo.lon, and running them one after another was the main reason
  a site scout was creeping past 15-20s as more sections got added.
- `run_section()` — one named section at a time, used by the web UI's
  `/api/scout/section/<name>` endpoint. The web flow fetches every section
  in the background (parallel requests, browser + gunicorn both handle the
  concurrency) while the user is still on the plot-confirmation step, so
  by the time they've picked their parcel(s) most sections are already
  in. `boundary` isn't in SECTION_SPECS — it's computed client-side from
  whichever parcels the user confirms (see cadastral.summarise_selected_parcels()),
  since it's the one section that actually depends on that choice.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging

from . import cadastral, ecology, gsi, heritage, rps, utilities, planning, report

log = logging.getLogger("sitescout.pipeline")


def _planning_applications(lat: float, lon: float, eircode: str | None) -> dict:
    data = planning.get_planning_applications(lat, lon, eircode)
    attach_boundaries(data)
    return data


def attach_boundaries(planning_applications: dict) -> None:
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


# Every section except "boundary" — that one's special in the web flow
# (comes from the user's confirmed plot selection, not a fresh point
# query), but the CLI still fetches it here like everything else since it
# has no picker UI to get a selection from.
SECTION_SPECS = {
    "geology": lambda lat, lon, eircode, label: gsi.get_geology(lat, lon),
    "groundwater": lambda lat, lon, eircode, label: gsi.get_groundwater(lat, lon),
    "archaeology": lambda lat, lon, eircode, label: heritage.get_archaeology(lat, lon),
    "smr_zone": lambda lat, lon, eircode, label: heritage.get_smr_zone(lat, lon),
    "niah": lambda lat, lon, eircode, label: heritage.get_niah(lat, lon),
    "planning_applications": lambda lat, lon, eircode, label: _planning_applications(lat, lon, eircode),
    "flood_risk": lambda lat, lon, eircode, label: planning.get_flood_risk(lat, lon),
    "ecology": lambda lat, lon, eircode, label: ecology.get_protected_sites(lat, lon),
    "rps_aca": lambda lat, lon, eircode, label: rps.get_protected_structures(lat, lon),
    "utilities": lambda lat, lon, eircode, label: utilities.draft_requests(lat, lon, label or ""),
    "planning": lambda lat, lon, eircode, label: planning.get_planning_links(lat, lon),
}

SECTION_NAMES = tuple(SECTION_SPECS.keys())


def run_section(name: str, lat: float, lon: float, eircode: str | None = None, label: str | None = None) -> dict:
    """Runs exactly one named section — used by the web UI's per-section
    endpoint so the browser can fetch all of them in parallel and update
    the sidebar as each one lands, instead of waiting on one big response.
    Raises ValueError for an unrecognised name, and lets the underlying
    lookup's own exceptions propagate — unlike run() below, a single
    section request should surface its own error, not silently disappear.
    """
    if name not in SECTION_SPECS:
        raise ValueError(f"Unknown section: {name}")
    return SECTION_SPECS[name](lat, lon, eircode, label)


def run(query: str, resolved, geo) -> dict:
    sections = {}

    try:
        sections["boundary"] = cadastral.get_boundary(geo.lat, geo.lon)
    except Exception as exc:
        log.error("Cadastral boundary lookup failed: %s", exc)

    def _run_one(name):
        try:
            return name, run_section(name, geo.lat, geo.lon, resolved.eircode, geo.label), None
        except Exception as exc:
            return name, None, exc

    with ThreadPoolExecutor(max_workers=len(SECTION_NAMES)) as executor:
        for name, result, exc in executor.map(_run_one, SECTION_NAMES):
            if exc is not None:
                log.error("%s lookup failed: %s", name, exc)
            else:
                sections[name] = result

    return report.build_report(query, resolved, geo, sections)
