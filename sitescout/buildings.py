"""Building footprints near a site, from OpenStreetMap — used only by the
/terrain-3d rotatable elevation view (elevation.get_terrain_mesh()'s
`buildings` param), to give the terrain model real visual context (the
site's own house/outbuildings, not just bare clipped ground). Not one of
pipeline.py's SECTION_SPECS — this has nothing to do with the report
itself, only that one 3D view, and it's fetched by webapp.py's own
/api/terrain-mesh route composition, not the CLI.

Uses the public Overpass API (overpass-api.de) — the standard way to query
arbitrary OpenStreetMap data by area, no key needed. Confirmed live before
use, not assumed from docs: a `way["building"](around:200,52.1394,-8.2775)`
query at Fermoy, Co. Cork returned 55 real building footprints with closed
[lat, lon] rings (`out body geom`'s `geometry` field). Two real gotchas
found in that same test, both handled below:

1. **The default python-requests User-Agent gets a plain 406 Not
   Acceptable** from overpass-api.de — confirmed by testing with and
   without a custom `User-Agent` header. Public Overpass mirrors reject
   generic/bot-like clients as an anti-abuse measure; a descriptive UA
   fixes it.
2. **The free public instance genuinely times out under load
   (504/read-timeout), unpredictably** — confirmed by seeing both a 504
   ("server is probably too busy") and a hard connection read-timeout on
   different attempts against the exact same query that then succeeded on
   a later retry. This is real, expected flakiness of a shared free
   service (same category as GSI's own noted flakiness elsewhere in this
   app), not a query bug — handled with a few retries, not a fallback data
   source (Overpass has no widely-used free alternative for this).

**Most OSM buildings here carry no real height data — confirmed, not
assumed.** In that same 55-building Fermoy test, only 8 had a
`building:levels` tag and none had a `height` tag at all. A disclosed
default height (`DEFAULT_BUILDING_HEIGHT_M`) is therefore doing real work
for most buildings shown, not covering some rare edge case — every
building the frontend renders carries its own `height_is_estimated` flag
so the 3D view (and CLAUDE.md) can be honest about which ones are a real
OSM tag and which are a guess.

Only queries `way["building"]` — multipolygon `relation` buildings (used
for buildings with courtyards/holes) are skipped. Rare enough for typical
Irish rural/suburban sites, and this is a visual aid for a site-scouting
report, not a survey-grade 3D city model, so the extra geometry-assembly
complexity for that case isn't worth it here.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 30
OVERPASS_RETRIES = 3
OVERPASS_RETRY_DELAY_S = 3
OVERPASS_USER_AGENT = "ireland-site-scout (local site-scouting tool; building footprints for a 3D terrain view)"

METRES_PER_LEVEL = 3.0  # a standard assumed per-floor height, same order of magnitude used by common OSM-building-to-3D tools
DEFAULT_BUILDING_HEIGHT_M = 6.0  # ~2 storeys — used whenever neither `height` nor `building:levels` is tagged (confirmed the common case, see module docstring)


def get_nearby_buildings(lat: float, lon: float, radius_m: float) -> list[dict]:
    """Every OSM building way within `radius_m` of (lat, lon). Returns
    `[{"footprint_wgs84": [[lon, lat], ...] (closed ring), "height_m": float,
    "height_is_estimated": bool, "name": str | None}, ...]` — ready for
    elevation.get_terrain_mesh()'s `buildings` param to ground and extrude.
    Returns an empty list (not an exception) if Overpass is unreachable
    after retries — a 3D terrain view with no buildings drawn is a graceful
    degradation, not a broken page.
    """
    query = f"""
[out:json][timeout:25];
way["building"](around:{radius_m},{lat},{lon});
out body geom;
"""
    data = None
    last_exc: Optional[Exception] = None
    for attempt in range(1, OVERPASS_RETRIES + 1):
        try:
            resp = requests.post(
                OVERPASS_URL,
                data={"data": query},
                timeout=OVERPASS_TIMEOUT_S,
                headers={"User-Agent": OVERPASS_USER_AGENT},
            )
            if resp.status_code == 200:
                data = resp.json()
                break
            log.warning("Overpass returned %s (attempt %d/%d)", resp.status_code, attempt, OVERPASS_RETRIES)
        except Exception as exc:
            last_exc = exc
            log.warning("Overpass request failed (attempt %d/%d): %s", attempt, OVERPASS_RETRIES, exc)
        if attempt < OVERPASS_RETRIES:
            time.sleep(OVERPASS_RETRY_DELAY_S)

    if data is None:
        log.error("Overpass unreachable after %d attempt(s)%s — continuing without buildings",
                   OVERPASS_RETRIES, f": {last_exc}" if last_exc else "")
        return []

    buildings = []
    for el in data.get("elements", []):
        geom = el.get("geometry")
        if not geom or len(geom) < 4:
            continue  # degenerate/incomplete way geometry — skip rather than build a bad mesh from it
        tags = el.get("tags") or {}
        height_m, is_estimated = _estimate_height(tags)
        buildings.append({
            "footprint_wgs84": [[pt["lon"], pt["lat"]] for pt in geom],
            "height_m": height_m,
            "height_is_estimated": is_estimated,
            "name": tags.get("name"),
        })

    real_height_count = sum(1 for b in buildings if not b["height_is_estimated"])
    log.info("-> %d building(s) within %dm (%d with a real height/levels tag, %d estimated at %.0fm)",
              len(buildings), radius_m, real_height_count, len(buildings) - real_height_count, DEFAULT_BUILDING_HEIGHT_M)
    return buildings


def _estimate_height(tags: dict) -> tuple[float, bool]:
    height_str = tags.get("height")
    if height_str:
        try:
            return float(str(height_str).strip().rstrip("m").strip()), False
        except ValueError:
            pass
    levels_str = tags.get("building:levels")
    if levels_str:
        try:
            return float(levels_str) * METRES_PER_LEVEL, False
        except ValueError:
            pass
    return DEFAULT_BUILDING_HEIGHT_M, True
