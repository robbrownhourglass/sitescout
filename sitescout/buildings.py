"""Building footprints AND roads/tracks near a site, from OpenStreetMap —
used only by the /terrain-3d rotatable elevation view
(elevation.get_terrain_mesh()'s `buildings`/`roads` params), to give the
terrain model real visual context (the site's own house/outbuildings and
any laneway/road/track on it, not just bare clipped ground). Not one of
pipeline.py's SECTION_SPECS — this has nothing to do with the report
itself, only that one 3D view, and it's fetched by webapp.py's own
/api/terrain-mesh route composition, not the CLI. Buildings and roads are
fetched together in ONE Overpass query (get_nearby_features()) rather than
two separate ones — deliberately, to not double the load/latency on a
public service that's already confirmed to be a real bottleneck (see the
"Follow-up" note below).

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

   **Follow-up, after a real user report of the /terrain-3d page taking
   ~40s to load**: the original retry budget (3 attempts x 30s timeout +
   3s delay, worst case ~99s) was confirmed live to actually cost ~38s on
   a bad run (three real 504s in a row) at a genuinely rural test site
   that DOES have buildings (confirmed: a bare retry a minute later found
   10 real buildings there in 8s) — so this wasn't a "no buildings here"
   case, it was Overpass having a slow moment. Fixed three ways: (a) the
   retry budget itself is now much tighter (2 attempts, 12s timeout, 1.5s
   delay — worst case ~26s instead of ~99s) since buildings are a visual
   nice-to-have, not core report data, so failing faster and just showing
   no buildings is the right trade; (b) webapp.py now fetches buildings
   and builds the terrain mesh CONCURRENTLY (same ThreadPoolExecutor
   pattern pipeline.py already uses for report sections) instead of
   sequentially, so Overpass's latency no longer sits entirely on top of
   the (normally sub-second) mesh build; (c) a small on-disk cache (below)
   means only the FIRST lookup near a given site ever pays Overpass's
   latency at all.

**Most OSM buildings here carry no real height data — confirmed, not
assumed.** In that same 55-building Fermoy test, only 8 had a
`building:levels` tag and none had a `height` tag at all. A disclosed
default height (`DEFAULT_BUILDING_HEIGHT_M`) is therefore doing real work
for most buildings shown, not covering some rare edge case — every
building the frontend renders carries its own `height_is_estimated` flag
so the 3D view (and CLAUDE.md) can be honest about which ones are a real
OSM tag and which are a guess.

Only queries `way["building"]`/`way["highway"]` — multipolygon `relation`
buildings (used for buildings with courtyards/holes) are skipped. Rare
enough for typical Irish rural/suburban sites, and this is a visual aid
for a site-scouting report, not a survey-grade 3D city model, so the
extra geometry-assembly complexity for that case isn't worth it here.

Roads use the same "no real dimension data" situation as buildings: OSM's
`width` tag is rarely present, so each road is drawn at a standard rough
width by its `highway` type (a motorway is wider than a footway) rather
than a real surveyed width — see elevation.ROAD_WIDTH_M.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 12
OVERPASS_RETRIES = 2
OVERPASS_RETRY_DELAY_S = 1.5
OVERPASS_USER_AGENT = "ireland-site-scout (local site-scouting tool; building footprints for a 3D terrain view)"

METRES_PER_LEVEL = 3.0  # a standard assumed per-floor height, same order of magnitude used by common OSM-building-to-3D tools
DEFAULT_BUILDING_HEIGHT_M = 6.0  # ~2 storeys — used whenever neither `height` nor `building:levels` is tagged (confirmed the common case, see module docstring)

# On-disk cache, same spirit as elevation.py's .cache/lidar_tiles/ (repeat
# lookups near the same site shouldn't re-pay Overpass's latency) but a
# plain small JSON file per area rather than a multi-MB GeoTIFF — and,
# unlike LIDAR survey data (which never changes), OSM buildings genuinely
# do get added/edited over time, hence the TTL rather than caching forever.
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "osm_features"
CACHE_TTL_S = 30 * 24 * 3600  # 30 days


def _cache_path(lat: float, lon: float, radius_m: float) -> Path:
    # Rounded coarsely on purpose: repeat lookups for "the same site" won't
    # land on the exact same float lat/lon/radius (the mesh's own bounding
    # box shifts slightly with parcel selection), but real buildings/roads
    # don't move — a ~11m/10m-coarse key still reuses the cache for what's
    # obviously the same area.
    key = f"{round(lat, 4)}_{round(lon, 4)}_{round(radius_m / 10) * 10}"
    return CACHE_DIR / f"{key}.json"


def _read_cache(path: Path) -> Optional[dict]:
    try:
        if time.time() - path.stat().st_mtime > CACHE_TTL_S:
            return None
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, features: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(features))
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Could not write OSM features cache %s: %s", path, exc)


def get_nearby_features(lat: float, lon: float, radius_m: float) -> dict:
    """Every OSM building AND road/track way within `radius_m` of
    (lat, lon), in one combined Overpass query. Returns
    `{"buildings": [{"footprint_wgs84": [[lon, lat], ...] (closed ring),
    "height_m": float, "height_is_estimated": bool, "name": str | None}, ...],
    "roads": [{"path_wgs84": [[lon, lat], ...] (open path), "highway_type":
    str, "name": str | None}, ...]}` — ready for elevation.get_terrain_mesh()'s
    `buildings`/`roads` params to ground and extrude. Both lists are empty
    (not an exception) if Overpass is unreachable after retries — a 3D
    terrain view with nothing extra drawn on it is a graceful degradation,
    not a broken page. Cached on disk (CACHE_TTL_S) so only the first
    lookup near a given site ever pays Overpass's latency.
    """
    cache_path = _cache_path(lat, lon, radius_m)
    cached = _read_cache(cache_path)
    if cached is not None:
        log.info("-> %d building(s), %d road(s) within %dm (cached)",
                  len(cached.get("buildings", [])), len(cached.get("roads", [])), radius_m)
        return cached

    query = f"""
[out:json][timeout:10];
(
  way["building"](around:{radius_m},{lat},{lon});
  way["highway"](around:{radius_m},{lat},{lon});
);
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
        log.error("Overpass unreachable after %d attempt(s)%s — continuing without buildings/roads",
                   OVERPASS_RETRIES, f": {last_exc}" if last_exc else "")
        return {"buildings": [], "roads": []}

    buildings, roads = [], []
    for el in data.get("elements", []):
        geom = el.get("geometry")
        if not geom or len(geom) < 2:
            continue  # degenerate/incomplete way geometry — skip rather than build a bad mesh from it
        tags = el.get("tags") or {}
        if tags.get("building"):
            if len(geom) < 4:
                continue  # a footprint needs at least a real triangle, not just a couple of points
            height_m, is_estimated = _estimate_height(tags)
            buildings.append({
                "footprint_wgs84": [[pt["lon"], pt["lat"]] for pt in geom],
                "height_m": height_m,
                "height_is_estimated": is_estimated,
                "name": tags.get("name"),
            })
        elif tags.get("highway"):
            roads.append({
                "path_wgs84": [[pt["lon"], pt["lat"]] for pt in geom],
                "highway_type": tags.get("highway"),
                "name": tags.get("name"),
            })

    real_height_count = sum(1 for b in buildings if not b["height_is_estimated"])
    log.info("-> %d building(s) within %dm (%d with a real height/levels tag, %d estimated at %.0fm), %d road(s)",
              len(buildings), radius_m, real_height_count, len(buildings) - real_height_count,
              DEFAULT_BUILDING_HEIGHT_M, len(roads))
    result = {"buildings": buildings, "roads": roads}
    _write_cache(cache_path, result)
    return result


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
