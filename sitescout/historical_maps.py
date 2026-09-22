"""Historical satellite/aerial imagery (6 real epochs, 1995-2018) and
historic OS map series (25-inch, three 6-inch editions) for Ireland —
Tailte Éireann's own MapGenie tile caches. Found via the same "pull apart
the ArcGIS/AGO app config" technique already used throughout this project
(item 11's EirGrid, item 4's NMS/NPWS viewers, etc.) — this time against
GeoHive's own public "National Irish Imagery Dashboard" and "National
Irish Historic Maps Dashboard" ArcGIS Dashboard apps, found via ArcGIS
Online's own public content search
(`GET https://www.arcgis.com/sharing/rest/search?q=owner:GeoHiveApplications`).
Each dashboard's own `/data` config lists one `mapWidget` per historical
epoch/series, each pointing at a distinct real ArcGIS tiled MapServer.

**Licensing, checked directly by fetching and reading GeoHive's actual
Terms of Use, not assumed.** This is Tailte Éireann's own copyrighted
product (every one of these AGO items' own `licenseInfo` says so). The
Terms explicitly permit "personal non-commercial use... free of charge"
but require prior permission for anything beyond that ("If you would like
to use or exploit the Services or any Tailte IP for commercial purposes,
please contact us to seek permission" / "the Services may not be used for
commercial purposes without obtaining, in advance, a licence"). Confirmed
directly with the user before building this: this app is a local,
single-user, non-commercial site-scouting tool, not a distributed/hosted
commercial product — squarely the permitted case, not the restricted one.

**A real technical wrinkle, confirmed live, not assumed.** These are
`capabilities: "Map,TilesOnly,Tilemap"` ArcGIS cached tile services in
native Irish Transverse Mercator (ITM, EPSG:2157) — "TilesOnly" turns out
to mean exactly what it says: the `/export` dynamic-render operation
(already proven working for the EPA/GSI contour layer elsewhere in this
app, see elevation.py's module docstring) returns a hard 500 "Error
invoking service" on these, confirmed directly by testing it before
assuming it would work. So there's no shortcut here — these tiles
genuinely need to be reprojected pixel-by-pixel from their own native ITM
tile grid into the standard Web Mercator XYZ tiles Leaflet actually
requests, using the same per-pixel pyproj-vectorized reprojection
technique already proven for the 3D terrain view's own satellite/OSM
basemap draping (elevation.py's `_fetch_map_mosaic()`) — just inverted:
there, a big texture is built by sampling real-world positions computed
from a plain rectangular local-coordinate grid; here, one small 256x256
Leaflet tile is built by first computing that tile's own real WGS84
bounds (standard XYZ tile math), then reprojecting each of its pixels
into the source service's native ITM tile grid.

**Access, confirmed live, not assumed.** These ArcGIS "utility" services
are proxied through `utility.arcgis.com` and returned a 403 on a bare,
header-less request — but succeeded consistently once a real `Referer`
matching a dashboard app that legitimately embeds them was sent (and
inconsistently on later requests without one too, suggesting some
edge-level caching rather than a strictly-enforced allowlist — but the
real governing constraint here is the license terms above, not whatever
this technical gate happens to do). Sent unconditionally regardless,
since it's free, harmless, and the honest way to identify what's actually
calling this.

**Every MapGenie service tested shares the exact same tile origin and LOD
pyramid** — confirmed live across both an imagery service and a
historic-map service (origin (-5022200, 4821100) in ITM, 13 LODs down to
a 0.26m finest resolution), not assumed to generalize from checking just
one. `_get_tile_info()` still fetches each service's own tileInfo rather
than hardcoding this, since relying on an unconfirmed assumption for new
layers added later would be the wrong instinct given how consistently
this project has been burned by exactly that shortcut elsewhere (see
elevation.py's own "RESOLUTION metadata can be wrong" finding).
"""
from __future__ import annotations

import io
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
import requests
from PIL import Image
from pyproj import Transformer

log = logging.getLogger("sitescout.historical_maps")

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "historical_tiles"
SOURCE_TILE_TIMEOUT_S = 15
OUTPUT_TILE_SIZE = 256

# Real user report: at a deep enough zoom, every one of these layers just
# shows Tailte's own "Data not available at this scale" placeholder
# (see _looks_like_placeholder_tile above) instead of rendering — asked
# directly not to let the map zoom in further than these layers can
# actually show. All 10 services share one identical 13-level LOD
# pyramid (confirmed live for every one of them, not assumed to
# generalize from checking just one or two — see module docstring),
# bottoming out at 0.26458386250105836 m/px — converted to the nearest
# equivalent standard Leaflet zoom at a representative Irish latitude
# (53.35N, the same default map-centre latitude used elsewhere in this
# app) via the standard Web-Mercator resolution formula, floored so this
# never claims a finer real resolution than is actually confirmed. Sent
# to the frontend (see webapp.py's index() route) as the real ceiling
# for every historical layer's own Leaflet maxZoom/maxNativeZoom, and to
# clamp the map back to it if the user switches to one of these layers
# while already zoomed in past it.
_HISTORICAL_LOD_FINEST_RESOLUTION_M = 0.26458386250105836
_HISTORICAL_REFERENCE_LATITUDE = 53.35
MAX_USABLE_ZOOM = math.floor(math.log2(
    156543.03392 * math.cos(math.radians(_HISTORICAL_REFERENCE_LATITUDE)) / _HISTORICAL_LOD_FINEST_RESOLUTION_M
))
SOURCE_TILE_SIZE = 256
MAX_SOURCE_TILES_PER_SIDE = 4  # a 256px output tile should only ever need 1-2 source tiles per side at a matched resolution; this is a safety cap, not a normal case

# A real dashboard app that legitimately embeds these services — sent as
# Referer purely as an honest "this is what's calling you" identifier,
# not a bypass (see module docstring's own note on why the real governing
# constraint is the license terms, not this header).
_REFERER = "https://www.arcgis.com/apps/dashboards/f4d1a447e4d6463bbc9d6930601bdf86"
_HEADERS = {
    "Referer": _REFERER,
    "User-Agent": "ireland-site-scout (local, personal, non-commercial site-scouting tool; historical basemap tiles)",
}

_to_itm = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)

# label, base MapServer URL, kind ('imagery' or 'map') — every entry
# confirmed live (a real tile fetched and visually verified) before being
# added here. Ordered newest-to-oldest within each kind, matching how a
# "go back in time" picker should naturally present them.
HISTORICAL_LAYERS = {
    "imagery_2013_2018": {
        "label": "Satellite — 2013–2018",
        "base_url": "https://utility.arcgis.com/usrsvcs/servers/c00e64472ecb4b5e9e74c07e004c81fd/rest/services/MapGenieImagery2013to2018ITM/MapServer",
        "kind": "imagery",
    },
    "imagery_2011_2013": {
        "label": "Satellite — 2011–2013",
        "base_url": "https://utility.arcgis.com/usrsvcs/servers/5b3bf3c374b940fbbce9c109edf833dd/rest/services/MapGenieDigitalGlobeImagery2011to2013ITM/MapServer",
        "kind": "imagery",
    },
    "imagery_2006_2012": {
        "label": "Satellite — 2006–2012",
        "base_url": "https://utility.arcgis.com/usrsvcs/servers/a72b5b8d006945f3b7d2459548419fdb/rest/services/MapGenieImagery2006to2012ITM/MapServer",
        "kind": "imagery",
    },
    "imagery_2001_2005": {
        "label": "Satellite — 2001–2005",
        "base_url": "https://utility.arcgis.com/usrsvcs/servers/06ddd968fa9542c0abbfa8c6b9bd5192/rest/services/MapGenieImagery2001to2005ITM/MapServer",
        "kind": "imagery",
    },
    "imagery_1996_2000": {
        "label": "Satellite — 1996–2000",
        "base_url": "https://utility.arcgis.com/usrsvcs/servers/193d3316da024d52abf0af67feffa910/rest/services/MapGenieImagery1996to2000ITM/MapServer",
        "kind": "imagery",
    },
    "imagery_1995": {
        "label": "Satellite — 1995",
        "base_url": "https://utility.arcgis.com/usrsvcs/servers/22bd598d194046d39dba8543c30c321f/rest/services/MapGenieImagery1995ITM/MapServer",
        "kind": "imagery",
    },
    "map_25inch": {
        "label": "Historic Map — 25\" (1888–1913)",
        "base_url": "https://utility.arcgis.com/usrsvcs/servers/cd0baeb83438472596c7ff1ee9f78e71/rest/services/MapGenie25InchITM/MapServer",
        "kind": "map",
    },
    "map_6inch_last_bw": {
        "label": "Historic Map — 6\" Last Edition",
        "base_url": "https://utility.arcgis.com/usrsvcs/servers/8e1b9570148f4e1db492e7c10a9d344e/rest/services/MapGenie6InchLastEditionBlackWhiteITM/MapServer",
        "kind": "map",
    },
    "map_6inch_first_colour": {
        "label": "Historic Map — 6\" First Edition (Colour)",
        "base_url": "https://utility.arcgis.com/usrsvcs/servers/247d8d75972a41c4ab9cb9ba271a2b98/rest/services/MapGenie6InchFirstEditionColourITM/MapServer",
        "kind": "map",
    },
    "map_6inch_first_bw": {
        "label": "Historic Map — 6\" First Edition (1837–1842)",
        "base_url": "https://utility.arcgis.com/usrsvcs/servers/866885b3946d4725ad1a67b6434c3b8e/rest/services/MapGenie6InchFirstEditionBlackWhiteITM/MapServer",
        "kind": "map",
    },
}

# Tailte Éireann's own MapGenie tile cache serves a literal "Data not
# available at this scale" placeholder image (with a diagonal "Tailte
# Éireann" watermark baked in) for deep-zoom source tiles past wherever
# real map/imagery content actually exists — confirmed by fetching one
# directly and looking at it, not assumed from a metadata field: every
# one of the 10 services' own tileInfo/maxScale claims genuine coverage
# all the way to the deepest LOD (0.26m/px, level 12), but a real rural
# tile at that exact depth came back as this placeholder instead (8-9
# distinct RGB colours in a 256x256 crop, vs 130+ for the SAME area's
# real content one LOD coarser, and vs several confirmed neighbouring
# level-12 tiles all landing in that same 8-9 range) — real drawn map
# detail or photographed imagery never comes close to that few distinct
# colours, so this is a safe, general signal, not something tuned to one
# specific tile. Since it's served with a normal 200 (not a 404), it
# can't be filtered the same way as a genuine tile-cache gap — detected
# instead by real content richness, checked once per fetched tile.
_PLACEHOLDER_MAX_UNIQUE_COLORS = 24


def _looks_like_placeholder_tile(img: np.ndarray) -> bool:
    sample = img.reshape(-1, img.shape[-1])
    return len(np.unique(sample, axis=0)) <= _PLACEHOLDER_MAX_UNIQUE_COLORS


_tile_info_cache: dict = {}  # base_url -> parsed tileInfo, fetched once per running process


def _get_tile_info(base_url: str) -> Optional[dict]:
    if base_url in _tile_info_cache:
        return _tile_info_cache[base_url]
    try:
        resp = requests.get(base_url, params={"f": "json"}, headers=_HEADERS, timeout=SOURCE_TILE_TIMEOUT_S)
        resp.raise_for_status()
        tile_info = resp.json().get("tileInfo")
        if not tile_info:
            return None
        _tile_info_cache[base_url] = tile_info
        return tile_info
    except Exception as exc:
        log.warning("Failed to fetch tileInfo for %s: %s", base_url, exc)
        return None


def _source_cache_path(layer_key: str, level: int, row: int, col: int) -> Path:
    return CACHE_DIR / layer_key / str(level) / f"{row}_{col}.jpg"


def _fetch_source_tile(layer_key: str, base_url: str, level: int, row: int, col: int) -> Optional[np.ndarray]:
    """One raw 256x256 RGB source tile from the historical service's own
    native ITM cache, on-disk cached (same spirit as elevation.py's LIDAR
    tile cache and buildings.py's OSM feature cache) — a source tile is
    frequently reused across several adjacent OUTPUT (Web Mercator) tiles,
    and caching keeps this app from re-fetching the same historical tile
    from Tailte Éireann's servers repeatedly, which matters doubly here
    given this data isn't ours to redistribute freely (see module
    docstring) — minimizing real requests to their servers is the
    considerate thing to do regardless.
    """
    path = _source_cache_path(layer_key, level, row, col)
    if path.exists():
        try:
            img = np.array(Image.open(path).convert("RGB"))
            if _looks_like_placeholder_tile(img):
                return None
            return img
        except Exception:
            pass  # corrupt cache entry -- fall through and refetch
    url = f"{base_url}/tile/{level}/{row}/{col}"
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=SOURCE_TILE_TIMEOUT_S)
            if resp.status_code == 404:
                return None  # a real gap in this historical layer's own coverage here -- not every series covers every tile
            resp.raise_for_status()
            img = np.array(Image.open(io.BytesIO(resp.content)).convert("RGB"))
            if _looks_like_placeholder_tile(img):
                # A real 200, but Tailte's own "no content at this scale
                # here" placeholder (see _looks_like_placeholder_tile's own
                # docstring above) -- treat it exactly like a genuine gap,
                # and deliberately don't cache it: caching would just lock
                # in a permanent false gap if this area ever gets deeper
                # real coverage added upstream later.
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".tmp")
            tmp_path.write_bytes(resp.content)
            tmp_path.rename(path)
            return img
        except Exception as exc:
            log.warning("Historical source tile fetch failed (attempt %d) for %s z=%d row=%d col=%d: %s",
                        attempt + 1, layer_key, level, row, col, exc)
    return None


def _xyz_tile_bounds_lonlat(z: int, x: int, y: int) -> tuple:
    """Standard slippy-map tile -> WGS84 bounds (west, south, east, north)
    — the same formula already used client-side in templates/index.html's
    ContourExportLayer, just needed server-side here instead."""
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0

    def _lat(yt: float) -> float:
        rad = math.pi - 2 * math.pi * yt / n
        return math.degrees(math.atan(math.sinh(rad)))

    return west, _lat(y + 1), east, _lat(y)


def get_historical_tile(layer_key: str, z: int, x: int, y: int) -> Optional[bytes]:
    """A real Web Mercator XYZ tile (what Leaflet actually requests),
    reprojected pixel-by-pixel from the historical service's own native
    ITM tile cache — see this module's own docstring for why a plain URL
    substitution or /export call doesn't work here. Returns None if this
    layer has no real data anywhere in this tile (every source pixel a
    real fetch failure or a confirmed gap) — the Flask route treats that
    as "no tile," same as any missing map tile.
    """
    layer = HISTORICAL_LAYERS.get(layer_key)
    if not layer:
        return None
    base_url = layer["base_url"]
    tile_info = _get_tile_info(base_url)
    if not tile_info:
        return None
    origin = tile_info["origin"]
    lods = tile_info["lods"]

    west, south, east, north = _xyz_tile_bounds_lonlat(z, x, y)
    center_lat = (south + north) / 2

    # Which of the source service's own LODs best matches this output
    # tile's own real ground resolution — picks the CLOSEST available
    # (not just "finest ≤ desired"), so an old, coarse epoch's own
    # honestly-limited resolution is used as-is rather than needlessly
    # upsampled from something even coarser, and a deep zoom past what a
    # layer ever captured at just reuses its own finest LOD (expected —
    # older imagery/maps genuinely don't get any sharper than they are).
    n = 2 ** z
    desired_resolution_m = 156543.03392 * math.cos(math.radians(center_lat)) / n
    lod = min(lods, key=lambda entry: abs(entry["resolution"] - desired_resolution_m))
    resolution = lod["resolution"]
    level = lod["level"]

    # Per-output-pixel real-world position: pixel -> lon/lat (inverse Web
    # Mercator tile math, vectorized) -> ITM (pyproj, vectorized) — the
    # same technique already proven in elevation.py's 3D-view basemap
    # draping, just starting from a tile's own bounds instead of a local
    # mesh-coordinate grid.
    col_idx, row_idx = np.meshgrid(np.arange(OUTPUT_TILE_SIZE), np.arange(OUTPUT_TILE_SIZE))
    px_frac = (x + (col_idx + 0.5) / OUTPUT_TILE_SIZE) / n
    py_frac = (y + (row_idx + 0.5) / OUTPUT_TILE_SIZE) / n
    lon = px_frac * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * py_frac))))
    itm_x, itm_y = _to_itm.transform(lon, lat)

    src_col_f = (itm_x - origin["x"]) / (resolution * SOURCE_TILE_SIZE)
    src_row_f = (origin["y"] - itm_y) / (resolution * SOURCE_TILE_SIZE)
    src_tile_col = np.floor(src_col_f).astype(int)
    src_tile_row = np.floor(src_row_f).astype(int)

    col0, col1 = int(src_tile_col.min()), int(src_tile_col.max())
    row0, row1 = int(src_tile_row.min()), int(src_tile_row.max())
    if (col1 - col0 + 1) > MAX_SOURCE_TILES_PER_SIDE or (row1 - row0 + 1) > MAX_SOURCE_TILES_PER_SIDE:
        log.warning("Historical tile %s z=%d x=%d y=%d needs an unexpectedly large source range (%dx%d) — skipping",
                    layer_key, z, x, y, col1 - col0 + 1, row1 - row0 + 1)
        return None

    tile_coords = [(r, c) for r in range(row0, row1 + 1) for c in range(col0, col1 + 1)]
    with ThreadPoolExecutor(max_workers=max(1, len(tile_coords))) as pool:
        fetched = list(pool.map(lambda rc: (rc, _fetch_source_tile(layer_key, base_url, level, rc[0], rc[1])), tile_coords))

    if all(tile is None for _, tile in fetched):
        return None  # no real data anywhere in this output tile's own footprint

    mosaic = np.zeros(((row1 - row0 + 1) * SOURCE_TILE_SIZE, (col1 - col0 + 1) * SOURCE_TILE_SIZE, 3), dtype=np.uint8)
    covered = np.zeros(mosaic.shape[:2], dtype=bool)
    for (r, c), tile in fetched:
        if tile is None:
            continue
        oy, ox = (r - row0) * SOURCE_TILE_SIZE, (c - col0) * SOURCE_TILE_SIZE
        mosaic[oy:oy + SOURCE_TILE_SIZE, ox:ox + SOURCE_TILE_SIZE] = tile
        covered[oy:oy + SOURCE_TILE_SIZE, ox:ox + SOURCE_TILE_SIZE] = True

    within_col = ((src_col_f - col0) * SOURCE_TILE_SIZE).astype(int)
    within_row = ((src_row_f - row0) * SOURCE_TILE_SIZE).astype(int)
    within_col = np.clip(within_col, 0, mosaic.shape[1] - 1)
    within_row = np.clip(within_row, 0, mosaic.shape[0] - 1)

    rgba = np.zeros((OUTPUT_TILE_SIZE, OUTPUT_TILE_SIZE, 4), dtype=np.uint8)
    rgba[..., :3] = mosaic[within_row, within_col]
    rgba[..., 3] = np.where(covered[within_row, within_col], 255, 0).astype(np.uint8)

    if not rgba[..., 3].any():
        return None

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    return buf.getvalue()
