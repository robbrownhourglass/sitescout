"""Precise terrain elevation (OPW LIDAR, on-demand + cached) and national
elevation contours (EPA Hydrological DTM, live query) — two different
precision tiers for the same underlying question: how high is this site,
and what does the ground around it do.

## Precise elevation (OPW LIDAR DTM/DSM, real 2m pixels)

Ireland doesn't have a live API returning raw elevation values at LIDAR
resolution — every raster GSI publishes via ArcGIS Online (2m/1m/25cm/
12.5cm) turned out, on actually querying one, to be a "Hillshade" (a
cosmetic shaded-relief rendering, pixelType U8, 0-255) with no elevation
number behind the pixels — confirmed live at three different resolutions,
not assumed from the name. The real elevation values only exist in OPW's
own downloadable LIDAR survey tiles (GeoTIFF, confirmed float32, real
metres — sample values of 21.6, 22.1, 22.7m etc. at a real Dublin-area
tile), found via the same GSI ArcGIS server as the rest of this app,
listed as a "coverage index" (a polygon grid of survey tiles, each with
its own DATA_URL, RESOLUTION, and ITM extent as plain attributes — not
the raster data itself, just a pointer to it).

**This is not full national coverage.** OPW flew this LIDAR for flood-risk
mapping — it's concentrated on rivers, floodplains, and coasts. Confirmed
by testing four deliberately inland/upland points (Slieve Bloom, Wicklow
Mountains interior, Bog of Allen, rural mid-Roscommon): zero coverage at
all four. A site near water is likely to have precise data; a site well
inland/upland often won't.

**On-demand, not a bulk national download.** The full dataset (NASC:
3,444 tiles; older OPW: 635 tiles) is roughly 16GB+ — downloadable, but
not something to bundle into this app's normal deployment, and it
wouldn't even give full coverage (see above). Instead: query the coverage
index live for whatever point is being scouted (same pattern as every
other source in this app), and if a tile covers it, download just that
one ~4MB tile and cache it locally (CACHE_DIR) — repeat lookups near the
same area reuse the cached tile instead of re-downloading.

Sources tried in order:
- OPW NASC (National Aerial Survey Contract): confirmed genuinely 2m,
  ~4MB per tile, DTM + DSM both present, uppercase filenames.
- OPW (older, pre-NASC): also confirmed genuinely 2m, ~4MB per tile,
  DTM + DSM both present, lowercase filenames. **Confirmed dead weight,
  left in place anyway**: this specific coverage-index layer returns
  NEITHER `EXT_*` attributes NOR real geometry for any of its 635
  features (checked the full dataset, not a sample) — `_tile_extent()`
  below always returns None for it, so it can never actually contribute a
  tile. Kept in the list (rather than deleted) only because it costs
  nothing to leave in and would start working again for free if GSI ever
  fixes that service's schema.
- TII (Transport Infrastructure Ireland): road/rail corridor survey, easily
  the single biggest incremental source found (10,268km², confirmed via a
  real geometric union of all 2,567 tiles) — but published differently
  from OPW's own tiles in three separate ways, each confirmed by actually
  downloading a sample tile rather than assumed to match OPW's convention:
  (1) **no `EXT_*` attributes at all** — real `esriGeometryPolygon`
  geometry IS returned though, and confirmed to be the same plain
  axis-aligned-square convention (`SHAPE.AREA` = exactly 4,000,000 =
  2000x2000m) as OPW's tiles, so a geometry-derived bounding box is exact,
  not an approximation — see `_tile_extent()`; (2) **DTM only, no DSM** —
  its zip has exactly one `.tif`, not a `_DTM`/`_DSM` pair (`dsm_pattern`
  is `None` for this source, handled explicitly below); (3) **a different
  NoData sentinel, `-99.0` not `-9999.0`** — confirmed directly (622,256 of
  1,000,000 pixels in a real sample tile are exactly `-99.0`, zero pixels
  at any other implausible value, and the remaining pixels form a smooth,
  plausible 21-193m range for Kerry terrain) — normalized to the app's own
  `-9999.0` convention at mosaic-build time (`_mosaic_dtm()`) so every
  downstream NoData check keeps working unchanged.
- Westmeath County Council: one county's own commissioned survey (2020,
  25cm resolution, Ushnagh Hill area) — confirmed to match OPW's own
  `EXT_*` + `_DTM.tif`/`_DSM.tif` + `-9999.0` conventions exactly (just
  nested one folder level deeper in its zip), so it needed no special
  handling at all once the TII-driven fixes above existed.

**Deliberately not yet wired in: GSI/DCHG/DP (heritage sites), confirmed
1,619km².** Its tiles are a genuinely different file FORMAT — ESRI ASCII
Grid (`.asc`, a plain 6-line text header + space-separated rows), not
GeoTIFF — confirmed by downloading a real sample. Not hard to parse (the
header even states its own `NODATA_value` explicitly, no guessing needed,
arguably more robust than TII's undocumented `-99`) but it's a genuinely
new raster-reading code path through `tifffile.memmap()`/`imread()`
everywhere in this module, and deserves its own careful, separately-tested
pass rather than being bundled into the same change as TII/Westmeath.
GSI Phase2 (144 tiles) and NYU Dublin (4 tiles) are excluded for the same
reason as pre-NASC above — confirmed neither returns `EXT_*` attributes
nor real geometry, so `_tile_extent()` would always return None for them
too; both are small enough (a few km² combined) not to be worth chasing
further right now.

**Deliberately excludes OPW Cork.** Its coverage-index RESOLUTION field
claims 2.0m — confirmed WRONG by actually downloading and reading a tile:
it's really 0.125m (12.5cm), 16003x16003 pixels, ~1GB uncompressed DTM
only (no DSM). A real metadata bug on GSI's side, not something to trust
blindly. Out of scope for "2m data" specifically and a much heavier
download profile (200MB+ compressed) — could be added later as a
separate "ultra-high-resolution where available" feature using the same
tifffile.memmap() approach below (confirmed working on that exact 1GB
file without loading it into memory — single-pixel reads stay near-
instant regardless of file size, since it's read via a memory-mapped
uncompressed strip-per-row TIFF, not decoded whole).

Pixel lookup: the coverage index gives each tile's ITM (EPSG:2157) extent
directly as plain attributes (EXT_LEFT/EXT_TOP/EXT_RIGHT/EXT_BOTTOM) — no
need to parse GeoTIFF georeferencing tags out of the TIFF itself. Convert
the query lon/lat to ITM via pyproj (confirmed against a known Dublin
reference point), then simple linear pixel math against the tile's own
actual raster shape (read from the TIFF itself, not assumed) gives the
row/col to read.

## Contours (EPA Hydrological DTM, live ArcGIS query)

A national contour-line layer (10m vertical interval, derived from a
20m-resolution DTM) — much coarser than the LIDAR above, but genuinely
live-queryable with no download/caching needed, and covers the whole
country (unlike the LIDAR tiles). Used as a fallback headline figure when
no precise LIDAR tile covers the point.

This function only returns attribute values (`CONTOUR_M`), not geometry —
`returnGeometry=true` on this service comes back `exceededTransferLimit`
with zero features for every combination tried (see the full writeup in
CLAUDE.md). The map DOES show contour lines despite that — as a
server-rendered image tile layer (the same MapServer's `export`
operation, Esri's non-WMS equivalent of WMS GetMap), built entirely in
templates/index.html (`ContourExportLayer`), not from anything this
function returns. Don't assume `get_contours()`'s output can drive a
vector map layer — it can't, by design, because the underlying service
can't supply one.
"""
from __future__ import annotations

import base64
import heapq
import io
import logging
import math
import os
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
import requests
import shapely
import tifffile
from PIL import Image, ImageDraw
from pyproj import Transformer
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from . import config
from .arcgis import point_query, point_query_full

log = logging.getLogger("sitescout.elevation")

# (source label, coverage-index query URL, DTM filename pattern,
#  DSM filename pattern (None if the source has no DSM), NoData sentinel
#  used inside that source's own raw TIFF — normalized to -9999.0 at
#  mosaic-build time so every other NoData check in this module can stay
#  written against one single convention)
LIDAR_SOURCES = [
    (
        "OPW NASC",
        "https://gsi.geodata.gov.ie/server/rest/services/Lidar/IE_GSI_LiDAR_Coverage_OPW_NASC_IE26_ITM/MapServer/3/query",
        "{name}_DTM.tif", "{name}_DSM.tif", -9999.0,
    ),
    (
        "OPW (pre-NASC)",
        "https://gsi.geodata.gov.ie/server/rest/services/Lidar/IE_GSI_LiDAR_Coverage_OPW_IE26_ITM/MapServer/3/query",
        "{name}_dtm.tif", "{name}_dsm.tif", -9999.0,
    ),
    (
        "TII",
        "https://gsi.geodata.gov.ie/server/rest/services/Lidar/IE_GSI_LiDAR_Coverage_TII_IE26_ITM/MapServer/0/query",
        "{name}/{name}.tif", None, -99.0,
    ),
    (
        "Westmeath Co Co",
        "https://gsi.geodata.gov.ie/server/rest/services/Lidar/IE_GSI_LiDAR_Coverage_WH_CoCo_IE26_ITM/MapServer/0/query",
        "{name}/{name}_DTM.tif", "{name}/{name}_DSM.tif", -9999.0,
    ),
]

CONTOUR_LAYER_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Third_Party/"
    "IE_GSI_EPA_Hydrologically_Corrected_DTM_20m_Contours_10m_IE26_ITM/MapServer/0/query"
)
CONTOUR_SEARCH_RADIUS_M = 500

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "lidar_tiles"
TILE_DOWNLOAD_TIMEOUT_S = 60  # generous for a confirmed ~4MB tile; not sized for the excluded 200MB+ Cork case

_to_itm = Transformer.from_crs("EPSG:4326", "EPSG:2157", always_xy=True)
_from_itm = Transformer.from_crs("EPSG:2157", "EPSG:4326", always_xy=True)

# A simple 4-stop hypsometric tint (green -> yellow-green -> tan -> white),
# the standard "low to high" terrain-elevation color convention — chosen
# specifically because it's a real, disclosed, per-tile min/max scale (see
# render_dtm_image()), not a hillshade: every pixel's color directly
# encodes that pixel's own real elevation, answering "can we see the real
# 2m data" more directly than a relief shading would.
_COLOR_STOPS = [
    (0.00, (46, 139, 60)),
    (0.35, (154, 205, 50)),
    (0.65, (222, 165, 62)),
    (1.00, (255, 255, 255)),
]


def _cache_path(source_label: str, filename: str) -> Path:
    safe_source = source_label.replace(" ", "_").replace("(", "").replace(")", "")
    return CACHE_DIR / safe_source / filename


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file + rename so a concurrent request for the same
    (uncached) tile can't read a partially-written file.
    """
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _download_and_extract(
    data_url: str, source_label: str, dtm_name: str, dsm_name: Optional[str]
) -> tuple[Optional[Path], Optional[Path]]:
    """`dsm_name` is None for a source with no DSM at all (confirmed for
    TII: its zip has exactly one .tif, not a _DTM/_DSM pair) — the DSM
    half of the return is then always None, not a missing/failed lookup.
    """
    dtm_path = _cache_path(source_label, dtm_name)
    dsm_path = _cache_path(source_label, dsm_name) if dsm_name else None
    if dtm_path.exists():
        return dtm_path, (dsm_path if dsm_path and dsm_path.exists() else None)

    log.info("Downloading LIDAR tile (%s)…", data_url)
    resp = requests.get(data_url, timeout=TILE_DOWNLOAD_TIMEOUT_S)
    resp.raise_for_status()

    dtm_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = {n.lower(): n for n in zf.namelist()}
        if dtm_name.lower() not in names:
            log.warning("-> %s not found in %s (zip contents: %s)", dtm_name, data_url, zf.namelist())
            return None, None
        _atomic_write(dtm_path, zf.read(names[dtm_name.lower()]))
        if dsm_path and dsm_name.lower() in names:
            _atomic_write(dsm_path, zf.read(names[dsm_name.lower()]))

    return dtm_path, (dsm_path if dsm_path and dsm_path.exists() else None)


def _read_pixel(
    tif_path: Path, itm_x: float, itm_y: float,
    ext_left: float, ext_top: float, ext_right: float, ext_bottom: float,
    nodata: float = -9999.0,
) -> Optional[float]:
    mm = tifffile.memmap(str(tif_path))
    height, width = mm.shape
    px_w = (ext_right - ext_left) / width
    px_h = (ext_top - ext_bottom) / height
    col = int((itm_x - ext_left) / px_w)
    row = int((ext_top - itm_y) / px_h)
    if not (0 <= row < height and 0 <= col < width):
        del mm
        return None
    value = float(mm[row, col])
    del mm
    return None if value <= nodata else value


IMAGE_RADIUS_M = 1000  # "cover a 1km radius" — a square crop of this half-width, not a strict circle (simpler; visually equivalent for this purpose)


def _find_touching_tiles(lat: float, lon: float, radius_m: float) -> tuple[Optional[str], list[dict]]:
    """All tiles from the FIRST LIDAR_SOURCES entry that actually covers
    the exact point, intersecting a radius_m buffer around it — used to
    build a multi-tile mosaic instead of a single ~2km tile, since a 1km
    radius can straddle up to 4 tiles depending on where the point falls
    relative to the grid (confirmed live: Trinity College Dublin sits
    close enough to a boundary that its 1km radius touches exactly 4).

    **Gotcha, confirmed by testing — search a circle bigger than the
    square you're about to crop to.** The final mosaic is cropped to a
    `radius_m`-square (see _mosaic_dtm()), but a square's corners are
    further from its centre than its edges: a 1000m square's corners sit
    √2×1000 ≈ 1414m out. Searching this coverage index with
    `distance_m=radius_m` (a circle of that same radius) misses any tile
    that only touches the square's corner region — confirmed live at
    Fermoy, Co. Cork: a 1000m circular search found 3 tiles, but a 1414m
    one (matching the square's true diagonal reach) found 7, the extra 4
    covering exactly the corners the smaller circle didn't reach. So the
    search radius here is deliberately `radius_m * sqrt(2)`, not
    `radius_m` — over-fetches a handful of tiles whose area doesn't
    actually end up in the final square crop (harmless, just a few extra
    cached downloads), but guarantees nothing inside the square is missed.

    Uses arcgis.point_query()'s existing distance_m — a real server-side
    buffered spatial query, no manual sampling needed. "Does a source
    actually cover the exact point" is answered with a plain bbox check
    against each tile's own extent (see _tile_extent()) rather than a
    second query: these coverage-index features are plain axis-aligned
    squares, so their extent IS their exact boundary — no geometry library
    needed for an exact (not approximate) point-in-tile test, same
    reasoning as the karst/contour decisions not to hand-roll real polygon
    geometry elsewhere in this app, just simpler here because these shapes
    are trivial rectangles.

    Deliberately sticks to ONE source per mosaic (whichever the point
    itself falls within, in LIDAR_SOURCES priority order) rather than
    mixing tiles from different surveys/years into one image.
    """
    search_radius_m = radius_m * 1.5  # > radius_m * sqrt(2) (~1.4142), a bit of extra margin against rounding
    itm_x, itm_y = _to_itm.transform(lon, lat)
    for source_label, coverage_url, dtm_pattern, dsm_pattern, nodata in LIDAR_SOURCES:
        log.info("Checking %s LIDAR coverage within %dm (for a %dm-radius square crop)…", source_label, search_radius_m, radius_m)
        try:
            # return_geometry=True: needed as a fallback for sources whose
            # coverage index has no EXT_* attributes at all (confirmed
            # needed for TII) — see _tile_extent(). out_fields="*", not a
            # named list: confirmed live that TII's own layer throws a
            # hard "Failed to execute query" error (not a silent ignore)
            # when asked for EXT_* fields it doesn't have in its schema —
            # requesting everything sidesteps needing to know each
            # source's exact field names up front.
            feats = point_query(
                coverage_url, lon, lat, out_fields="*",
                distance_m=search_radius_m, result_record_count=16, return_geometry=True,
            )
        except Exception as exc:
            log.warning("-> %s coverage query failed: %s", source_label, exc)
            continue
        if not feats:
            continue

        exts = {id(f): _tile_extent(f) for f in feats}
        contains_point = any(
            ext is not None and ext[0] <= itm_x <= ext[2] and ext[3] <= itm_y <= ext[1]
            for ext in exts.values()
        )
        if not contains_point:
            log.info("-> %s has tiles nearby but none covering the exact point — trying next source", source_label)
            continue

        tiles = []
        for f in feats:
            ext = exts[id(f)]
            if ext is None:
                continue  # this source's coverage index has neither EXT_* attributes nor real geometry for this feature — see the module docstring's pre-NASC/GSI-Phase2/NYU note
            a = f["attributes"]
            data_name = a["DATA_NAME"]
            dtm_name = dtm_pattern.format(name=data_name)
            dsm_name = dsm_pattern.format(name=data_name) if dsm_pattern else None
            try:
                dtm_path, dsm_path = _download_and_extract(a["DATA_URL"], source_label, dtm_name, dsm_name)
            except Exception as exc:
                log.warning("-> %s tile %s download/extract failed: %s", source_label, data_name, exc)
                continue
            if not dtm_path:
                continue
            tiles.append({
                "dtm_path": dtm_path,
                "dsm_path": dsm_path,
                "ext": ext,
                "resolution": a.get("RESOLUTION"),
                "survey_date": a.get("DATECAPTUR"),
                "nodata": nodata,
            })

        if not tiles or not _point_has_real_data(tiles, itm_x, itm_y):
            # A tile's bounding box containing the point is NOT the same
            # as that exact pixel having real data — confirmed live: a
            # real query point sat inside an OPW NASC tile's own square,
            # but its survey (like TII's corridor survey, or any real
            # LIDAR flight) doesn't fill every tile edge-to-edge, so the
            # actual pixel there was NoData. Committing to this source
            # anyway (the original behaviour) would report "no coverage"
            # even when a LOWER-priority source (confirmed: TII, at this
            # exact point) has real data — exactly undoing the point of
            # having multiple sources. So: fall through to the next
            # source instead of stopping at the first bbox match.
            log.info("-> %s tile bounding box covers the point, but that exact pixel is NoData — trying next source", source_label)
            continue

        log.info("-> %s: %d tile(s) found (search radius %dm, for a %dm-radius square crop)", source_label, len(tiles), search_radius_m, radius_m)
        return source_label, tiles

    return None, []


def _point_has_real_data(tiles: list, itm_x: float, itm_y: float) -> bool:
    """Whether the exact query point's own pixel — not just its enclosing
    tile's bounding box — has real (non-NoData) elevation data. See the
    caller: a tile's bbox containing a point never guaranteed that exact
    pixel was actually surveyed.

    Uses tifffile.imread() here, not the memmap-based _read_pixel() this
    module otherwise prefers for single-pixel reads — confirmed live that
    memmap raises `ValueError: image data are not memory-mappable` on a
    real, ordinary-looking OPW NASC tile (memmap only works on
    uncompressed, contiguous TIFF data, and not every real tile turns out
    to be one). This check runs on ANY candidate source before it's even
    known whether that source will be used at all, so it needs to work
    unconditionally — a full-array read costs a bit more than a memmapped
    single-pixel one, but only for the couple of tiles actually adjacent
    to the query point, not the whole mosaic.
    """
    for t in tiles:
        left, top, right, bottom = t["ext"]
        if left <= itm_x <= right and bottom <= itm_y <= top:
            arr = tifffile.imread(str(t["dtm_path"]))
            height, width = arr.shape
            col = int((itm_x - left) / ((right - left) / width))
            row = int((top - itm_y) / ((top - bottom) / height))
            if not (0 <= row < height and 0 <= col < width):
                return False
            return float(arr[row, col]) > t.get("nodata", -9999.0)
    return False


def _tile_extent(feature: dict) -> Optional[tuple]:
    """A coverage-index tile's real-world ITM extent (left, top, right,
    bottom) — from the tile's own EXT_* attributes when present (OPW's own
    convention), or derived from its returned WGS84 polygon geometry
    otherwise (confirmed needed for TII's coverage index, which returns
    real geometry but no EXT_* attributes at all). Every source here uses
    the same plain-axis-aligned-square tile convention (confirmed via
    TII's own SHAPE.AREA = exactly 4,000,000 = 2000x2000m), so a geometry-
    derived bounding box is exact, not an approximation. None if a feature
    has neither (confirmed true of every OPW pre-NASC / GSI Phase2 / NYU
    Dublin feature — see the module docstring) — such a feature simply
    can't be used, not a bug to work around further.
    """
    a = feature["attributes"]
    if all(a.get(k) is not None for k in ("EXT_LEFT", "EXT_TOP", "EXT_RIGHT", "EXT_BOTTOM")):
        return a["EXT_LEFT"], a["EXT_TOP"], a["EXT_RIGHT"], a["EXT_BOTTOM"]
    rings = (feature.get("geometry") or {}).get("rings")
    if not rings:
        return None
    itm_points = [_to_itm.transform(x, y) for ring in rings for x, y in ring]
    xs = [p[0] for p in itm_points]
    ys = [p[1] for p in itm_points]
    return min(xs), max(ys), max(xs), min(ys)


def _itm_bounds_to_wgs84(ext: tuple) -> list:
    ext_left, ext_top, ext_right, ext_bottom = ext
    lon_sw, lat_sw = _from_itm.transform(ext_left, ext_bottom)
    lon_ne, lat_ne = _from_itm.transform(ext_right, ext_top)
    return [[lat_sw, lon_sw], [lat_ne, lon_ne]]


def _mosaic_dtm(lat: float, lon: float, radius_m: float) -> Optional[dict]:
    """Downloads every DTM tile touching a radius_m buffer around
    (lat, lon) from one LIDAR source, stitches them into one array
    positioned by each tile's own real ITM extent (all confirmed on the
    same regular 2km grid — tiles fit together exactly edge-to-edge, no
    reprojection/resampling needed), then crops to a radius_m square
    around the point. Returns None if no source covers the point.

    Shared by get_precise_elevation() (point value + bounds + range) and
    render_dtm_image() (the actual picture) so both are guaranteed
    consistent — computed from the exact same assembled data, not two
    independently-built mosaics that could drift apart.
    """
    source_label, tiles = _find_touching_tiles(lat, lon, radius_m)
    if not tiles:
        return None

    resolution = tiles[0]["resolution"] or 2.0
    left = min(t["ext"][0] for t in tiles)
    top = max(t["ext"][1] for t in tiles)
    right = max(t["ext"][2] for t in tiles)
    bottom = min(t["ext"][3] for t in tiles)
    width_px = round((right - left) / resolution)
    height_px = round((top - bottom) / resolution)
    canvas = np.full((height_px, width_px), -9999.0, dtype=np.float32)

    for t in tiles:
        arr = tifffile.imread(str(t["dtm_path"])).astype(np.float32)
        tile_nodata = t.get("nodata", -9999.0)
        if tile_nodata != -9999.0:
            # Normalize this source's own NoData sentinel to the canvas's
            # canonical -9999.0 — confirmed needed for TII, whose tiles use
            # -99.0 instead (see LIDAR_SOURCES/module docstring). Without
            # this, TII's real NoData pixels (most of a corridor-survey
            # tile — a road survey doesn't cover the whole 2km square) would
            # read as a plausible-looking but completely fake "-99m" dip
            # everywhere downstream (mesh, point reads, image rendering).
            arr[arr <= tile_nodata] = -9999.0
        t_left, _t_top, _t_right, t_bottom = t["ext"]
        col_off = round((t_left - left) / resolution)
        row_off = round((top - t["ext"][1]) / resolution)
        h, w = arr.shape
        canvas[row_off:row_off + h, col_off:col_off + w] = arr

    itm_x, itm_y = _to_itm.transform(lon, lat)
    col0 = max(0, round((itm_x - radius_m - left) / resolution))
    col1 = min(width_px, round((itm_x + radius_m - left) / resolution))
    row0 = max(0, round((top - (itm_y + radius_m)) / resolution))
    row1 = min(height_px, round((top - (itm_y - radius_m)) / resolution))
    cropped = canvas[row0:row1, col0:col1]

    cropped_ext = (
        left + col0 * resolution, top - row0 * resolution,
        left + col1 * resolution, top - row1 * resolution,
    )
    return {
        "array": cropped,
        "ext": cropped_ext,
        "resolution": resolution,
        "source_label": source_label,
        "survey_date": tiles[0]["survey_date"],
        "tile_count": len(tiles),
        "tiles": tiles,
    }


def get_precise_elevation(lat: float, lon: float) -> dict:
    itm_x, itm_y = _to_itm.transform(lon, lat)

    mosaic = _mosaic_dtm(lat, lon, IMAGE_RADIUS_M)
    if not mosaic:
        log.info("-> No precise LIDAR coverage at this point")
        return {"found": False}

    arr = mosaic["array"]
    height, width = arr.shape
    ext_left, ext_top, ext_right, ext_bottom = mosaic["ext"]
    col = int((itm_x - ext_left) / mosaic["resolution"])
    row = int((ext_top - itm_y) / mosaic["resolution"])
    if not (0 <= row < height and 0 <= col < width) or arr[row, col] <= -9999:
        log.info("-> Coverage exists nearby but this exact point is NoData")
        return {"found": False}
    ground_m = float(arr[row, col])

    # DSM: a single point read from whichever mosaic tile actually
    # contains (lat, lon) — the image itself is DTM-only, so DSM isn't
    # worth mosaicking, just reused from the tile list _mosaic_dtm()
    # already downloaded (no extra network call).
    surface_m = None
    for t in mosaic["tiles"]:
        tl, tt, tr, tb = t["ext"]
        if tl <= itm_x <= tr and tb <= itm_y <= tt and t["dsm_path"]:
            surface_m = _read_pixel(t["dsm_path"], itm_x, itm_y, tl, tt, tr, tb, nodata=t.get("nodata", -9999.0))
            break

    valid = arr > -9999
    vmin, vmax = float(arr[valid].min()), float(arr[valid].max())

    log.info(
        "-> %s: ground %.2fm%s (%d tile(s) mosaicked)", mosaic["source_label"], ground_m,
        f", surface {surface_m:.2f}m" if surface_m is not None else "", mosaic["tile_count"],
    )
    return {
        "found": True,
        "ground_elevation_m": round(ground_m, 2),
        "surface_elevation_m": round(surface_m, 2) if surface_m is not None else None,
        "canopy_or_building_height_m": round(surface_m - ground_m, 2) if surface_m is not None else None,
        "resolution_m": mosaic["resolution"],
        "survey_date": mosaic["survey_date"],
        "source": f"{mosaic['source_label']} LIDAR",
        "bounds_wgs84": _itm_bounds_to_wgs84(mosaic["ext"]),
        "image_min_elevation_m": round(vmin, 1),
        "image_max_elevation_m": round(vmax, 1),
        "image_radius_m": IMAGE_RADIUS_M,
        "image_tile_count": mosaic["tile_count"],
        # Not rendered here — the image is only generated on demand, when
        # the map layer is actually toggled on (see webapp.py's
        # /api/terrain-image route and render_dtm_image() below), same
        # lazy-fetch pattern as the contour tile layer. bounds_wgs84 and
        # the min/max above ARE computed eagerly here (this function
        # already builds the mosaic anyway, for the point-value read) —
        # not an extra cost specific to the image, so no reason to defer.
        "image_url": f"/api/terrain-image?lat={lat}&lon={lon}",
    }


def _elevation_to_rgb(normalized: np.ndarray) -> np.ndarray:
    """normalized: 2D float array in [0, 1]. Returns an (H, W, 3) uint8
    array by piecewise-linear interpolation through _COLOR_STOPS.
    """
    h, w = normalized.shape
    rgb = np.zeros((h, w, 3), dtype=np.float32)
    for i in range(len(_COLOR_STOPS) - 1):
        t0, c0 = _COLOR_STOPS[i]
        t1, c1 = _COLOR_STOPS[i + 1]
        mask = (normalized >= t0) & (normalized <= t1)
        local_t = np.clip((normalized - t0) / (t1 - t0), 0, 1)
        for ch in range(3):
            rgb[..., ch] = np.where(mask, c0[ch] + (c1[ch] - c0[ch]) * local_t, rgb[..., ch])
    return rgb.astype(np.uint8)


def render_dtm_image(lat: float, lon: float) -> Optional[dict]:
    """Renders a mosaic of every DTM tile touching a IMAGE_RADIUS_M buffer
    around (lat, lon) as a real per-pixel elevation image — every pixel's
    color directly encodes that pixel's own genuine LIDAR elevation value
    (min/max-normalised across the mosaic, since a fixed national scale
    would wash out contrast in any one small area), not a hillshade and
    not a derived/coarser product. This is the actual 2m data requested —
    the "Terrain & elevation" card's single point value only ever showed
    one pixel out of the ~1000x1000 in a tile; this shows all of them
    across the full requested radius, not just whichever single ~2km tile
    happened to contain the exact point (which could clip most of a 1km
    radius if the point sits near a tile edge — confirmed happening at
    Trinity College Dublin, which needed 4 tiles to fully cover 1km).

    Called on demand (see webapp.py's /api/terrain-image route), not
    during the main site lookup itself — same lazy-fetch pattern as the
    contour tile layer, so a site that never gets this layer toggled on
    never pays the rendering cost (get_precise_elevation() DOES build the
    same mosaic eagerly, for its own point-value read — this function
    just reuses that same logic via _mosaic_dtm(), not a separate mosaic).
    """
    mosaic = _mosaic_dtm(lat, lon, IMAGE_RADIUS_M)
    if not mosaic:
        return None

    arr = mosaic["array"]
    valid = arr > -9999
    if not valid.any():
        return None

    vmin, vmax = float(arr[valid].min()), float(arr[valid].max())
    span = max(vmax - vmin, 0.01)  # guard against a div-by-zero on a perfectly flat area
    normalized = np.clip((arr - vmin) / span, 0, 1)
    rgb = _elevation_to_rgb(normalized)
    alpha = np.where(valid, 200, 0).astype(np.uint8)  # NoData pixels fully transparent
    rgba = np.dstack([rgb, alpha])

    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")

    return {
        "png_bytes": buf.getvalue(),
        "bounds_wgs84": _itm_bounds_to_wgs84(mosaic["ext"]),
        "min_elevation_m": round(vmin, 1),
        "max_elevation_m": round(vmax, 1),
        "source_label": mosaic["source_label"],
    }


def get_contours(lat: float, lon: float) -> dict:
    """No map geometry — confirmed, don't re-attempt returnGeometry=True
    here. Tested exhaustively: f=json and f=geojson, distances from 10m to
    500m, with and without maxAllowableOffset/geometryPrecision
    generalization — every combination came back with `features: []` and
    `exceededTransferLimit: true`, even asking for just one feature. This
    isn't the karst.py situation (geometry silently disabled server-side);
    here the response genuinely blows past whatever size cap this ArcGIS
    Server enforces, which only makes sense if each contour "feature" is
    one enormous polyline spanning a huge stretch of the country rather
    than being split into shorter segments. Attribute-only queries (no
    geometry) work fine — that's all this function asks for.
    """
    log.info("Querying nearby elevation contours within %dm…", CONTOUR_SEARCH_RADIUS_M)
    data = point_query_full(
        CONTOUR_LAYER_URL, lon, lat,
        out_fields="CONTOUR_M",
        distance_m=CONTOUR_SEARCH_RADIUS_M,
        return_geometry=False,
        result_record_count=25,
    )
    contours = [{"elevation_m": f["attributes"].get("CONTOUR_M")} for f in data.get("features", [])]
    elevations = [c["elevation_m"] for c in contours if c["elevation_m"] is not None]
    log.info("-> %d contour line(s) within %dm (elevation range %s)", len(contours), CONTOUR_SEARCH_RADIUS_M, (min(elevations), max(elevations)) if elevations else "n/a")
    return {
        "contour_count": len(contours),
        "contours": contours,
        # A range, not a single "nearest" figure — computing true nearest
        # would need real point-to-polyline distance, which this app
        # doesn't have a geometry library for (same reasoning as karst.py's
        # decision not to hand-roll geometry math). A range from whatever's
        # within the search radius is honest without pretending precision
        # this approach doesn't actually have.
        "elevation_range_m": [min(elevations), max(elevations)] if elevations else None,
        "search_radius_m": CONTOUR_SEARCH_RADIUS_M,
        "source": "EPA Hydrologically Corrected DTM, 20m resolution, 10m contour interval",
    }


def get_terrain(lat: float, lon: float) -> dict:
    return {
        "precise": get_precise_elevation(lat, lon),
        "contours": get_contours(lat, lon),
        "source": "Precise LIDAR (OPW, TII, Westmeath Co Co — see LIDAR_SOURCES) + EPA Hydrological DTM contours (national, coarser)",
        "caveat": (
            "Precise elevation (real LIDAR, ~2m grid or better) only exists where one of several "
            "agencies happened to survey it — OPW for flood-risk mapping (rivers, floodplains, coasts), "
            "TII along national road/rail corridors, Westmeath Co Co for its own county — not the whole "
            "country. Where none of them cover a point, the contour lines (10m vertical interval, 20m "
            "grid) are the fallback — real elevation values, but far coarser, and shown as a range "
            "rather than a single figure since this app has no way to compute true point-to-contour "
            "distance."
        ),
    }


# --- 3D terrain mesh, with the site's own boundary and nearby roads
# painted onto it as a texture (not built as separate geometry) ---
#
# Everything above answers "how high is this site" with a single number or
# a picture of a fixed square area around it. This answers a different
# question: "what does the ground under and around this specific plot
# actually look like" — a real mesh over a generous padded area, for the
# browser to render as a rotatable 3D surface, with the plot boundary and
# any nearby roads/tracks drawn onto it.
#
# **Revision history, because the first two approaches were both wrong in
# instructive ways:**
#
# v1 clipped the elevation GRID by testing each cell's corner with a
# hand-rolled point-in-polygon and discarding any cell not fully inside —
# correct, but visibly blocky/jagged at the boundary (the edge could only
# ever land on a grid line).
#
# v2 fixed the blockiness with real `shapely` polygon clipping + triangulation
# (intersecting boundary cells against the actual plot polygon, then
# `shapely.constrained_delaunay_triangles` on the exact fragment) — genuinely
# exact, but this created a NEW, worse problem once roads/buildings were
# added: the rendered terrain existed ONLY inside the tight polygon, so any
# building or road just outside it (a real, relevant part of the site's
# context — a neighbour's shed a few metres over the line, a driveway that
# crosses it) rendered as a disconnected object floating in empty space
# with no ground under it at all. Roads as separate 3D ribbon geometry
# (mitre-less quads per straight segment) also looked visibly blocky/
# discontinuous at corners — a real user report, with a screenshot showing
# both problems clearly.
#
# **v3 (current): stop treating the boundary and roads as geometry that
# has to be matched to a location on the surface — paint them onto the
# surface instead**, closer to how an actual physical site model or an
# aerial-orthophoto drape works. The terrain mesh is now a plain, uncipped
# rectangular grid over `radius_m + MESH_CONTEXT_BUFFER_M` (real context,
# not just the plot itself), with NO shapely clipping/triangulation
# needed for the terrain at all — a real simplification, not just a
# workaround (no more per-quad shapely calls, no more "which cells touch
# the boundary" edge cases). The plot boundary and any roads are drawn as
# lines directly onto a `overlay_texture_png_base64` PNG, in the exact
# same local coordinate frame as the mesh's own vertices (see
# `grid_extent`), and applied as the mesh's own texture map (multiplied
# with its existing per-vertex hypsometric elevation tint) — so they're
# always perfectly flush with the surface by construction, with no
# discontinuity/joint artifacts possible (a drawn line has none), and no
# "floating disconnected object" failure mode (nothing needs to be
# individually positioned relative to a clipped edge any more).
#
# Buildings stay real 3D objects (unlike roads/boundary) — a building is
# a genuine 3D volume, not a 2D marking on the ground, and the user asked
# for that distinction explicitly. Their own real problem (a flat base
# floating above, or gapping from, sloped real terrain) is fixed instead
# by sampling the LOWEST real elevation under the footprint (not just its
# centroid) and embedding the base further below that — see
# `_extrude_building()`/`BUILDING_EMBED_M`.

MESH_MAX_GRID_SIZE = 150  # cap the mesh at ~150x150 vertices regardless of plot size — plenty of detail, stays light in the browser
MESH_CONTEXT_BUFFER_M = 50  # render real terrain/context this far beyond the plot's own bounding box in every direction, not just to its edge
TEXTURE_SIZE = 1024  # overlay PNG resolution (boundary + roads) — compresses to a few KB regardless, since it's mostly flat white
BOUNDARY_LINE_COLOR = (224, 128, 32)  # warm amber — distinct from the green/tan/white hypsometric terrain tint underneath it
BOUNDARY_LINE_WIDTH_M = 1.0
ROAD_LINE_COLOR = (60, 60, 58)  # dark asphalt gray
ROAD_WIDTH_M = {
    "motorway": 10.0, "trunk": 9.0, "primary": 8.0, "secondary": 7.0, "tertiary": 6.0,
    "residential": 5.0, "unclassified": 5.0, "living_street": 5.0,
    "service": 3.5, "track": 2.5, "cycleway": 1.5, "footway": 1.2, "path": 1.0,
}  # standard rough widths by OSM highway type, for the painted line's thickness — real `width` tags are rarely present
DEFAULT_ROAD_WIDTH_M = 4.0
BUILDING_EMBED_M = 0.75  # extra depth below the LOWEST real elevation sampled under a building's footprint, so its base plants firmly into the terrain everywhere under it rather than possibly gapping on a slope

# Satellite imagery drape (get_satellite_overlay()) — same public, unauthenticated
# XYZ tile source templates/index.html's own "Satellite" base layer already
# uses (Esri World Imagery via ArcGIS Online), just fetched server-side and
# resampled into this app's own local mesh-coordinate frame instead of left
# as Leaflet map tiles. Esri's own tile URL convention is z/y/x (row before
# column) — confirmed against the exact same live endpoint already in use.
SATELLITE_TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
SATELLITE_TILE_SIZE = 256
SATELLITE_MAX_ZOOM = 19
SATELLITE_MAX_TILES_PER_SIDE = 8  # caps a single request at 64 tile downloads regardless of how large the padded mesh area is
SATELLITE_TILE_TIMEOUT_S = 15
SATELLITE_TEXTURE_SIZE = 1024


def _ring_sets_to_itm_polygon(polygon_ring_sets_wgs84: list):
    """cadastral.py's polygon_ring_sets_wgs84 convention (a list of ring-sets,
    each [outer_ring, hole_ring, ...] of [lon, lat] pairs) -> one shapely
    (Multi)Polygon in ITM metres. Unions the ring-sets together — matching
    cadastral.summarise_selected_parcels()'s "join into one" semantics
    (each parcel's own outline, not a true dissolve) closely enough for
    this purpose: only used to test which real elevation grid points fall
    inside the union of whatever the user selected.
    """
    polys = []
    for rings in polygon_ring_sets_wgs84:
        if not rings:
            continue
        outer = [_to_itm.transform(lon, lat) for lon, lat in rings[0]]
        holes = [[_to_itm.transform(lon, lat) for lon, lat in hole] for hole in rings[1:]]
        poly = Polygon(outer, holes)
        if not poly.is_valid:
            poly = poly.buffer(0)  # standard shapely fix for minor self-intersecting input
        if not poly.is_empty:
            polys.append(poly)
    if not polys:
        return None
    return unary_union(polys)


def mesh_center_and_radius(polygon_ring_sets_wgs84: list) -> Optional[tuple]:
    """(center_lon, center_lat, radius_m) for a plot boundary's own bounding
    box + MESH_CONTEXT_BUFFER_M (real surrounding context, not just the
    plot's own edge — see the module comment above) — the exact sizing
    get_terrain_mesh() uses to build its LIDAR mosaic. Exposed separately
    so webapp.py can size the nearby-buildings/roads search around the
    same real area (a genuine pyproj-based metre measurement, not a rough
    degrees-to-metres guess).
    """
    all_points = [pt for ring_set in polygon_ring_sets_wgs84 for ring in ring_set for pt in ring]
    if not all_points:
        return None
    lons = [p[0] for p in all_points]
    lats = [p[1] for p in all_points]
    center_lon = (min(lons) + max(lons)) / 2
    center_lat = (min(lats) + max(lats)) / 2
    center_x, center_y = _to_itm.transform(center_lon, center_lat)
    itm_points = [_to_itm.transform(lon, lat) for lon, lat in all_points]
    half_width = max(abs(x - center_x) for x, y in itm_points)
    half_height = max(abs(y - center_y) for x, y in itm_points)
    radius_m = max(half_width, half_height) + MESH_CONTEXT_BUFFER_M
    return center_lon, center_lat, radius_m


MESH_SMOOTHING_WINDOW = 3  # 3x3 median filter — see _median_smooth()


def _median_smooth(arr: np.ndarray, size: int = MESH_SMOOTHING_WINDOW) -> np.ndarray:
    """A small median filter (default 3x3) over the raw DTM array before
    meshing it for the 3D view — real LIDAR data can have a handful of
    genuinely noisy/outlier pixels (sensor edge effects, water-surface
    returns), which are invisible in a single point reading or a flat
    top-down colour-mapped image but show up as sharp, jagged "tearing" in
    a LIT, rotatable 3D surface (a real user report, with a screenshot,
    at the padded area's own outer edge — exactly where a wider render
    area is more likely to reach an actual noisy patch). A median filter
    is the standard, textbook tool for exactly this class of noise — as
    opposed to a mean/Gaussian blur, which would soften real terrain
    detail everywhere while still leaving some residual spike — verified
    directly against a synthetic single-pixel spike before use (removed
    it exactly, left an unrelated pixel untouched).

    NoData (-9999) is excluded from every window: a pixel whose
    neighbourhood contains ANY NoData is left completely untouched at its
    own raw value — smoothing across a real coverage boundary would mix
    "no data here" with real elevations, a worse bug than the noise being
    fixed. Confirmed directly: a pixel one column away from a NoData
    region stayed exactly at its original value, not blended toward -9999.

    Only used for the 3D mesh (get_terrain_mesh()) — deliberately NOT
    applied to get_precise_elevation()'s single reported point value (that
    should stay the exact raw pixel, not a smoothed neighbourhood average)
    or render_dtm_image() (the issue wasn't reported there, and a flat
    top-down image doesn't reveal this kind of noise the way lit 3D
    shading does).
    """
    h, w = arr.shape
    pad = size // 2
    padded = np.pad(arr, pad, mode="edge")
    windows = np.stack([padded[i:i + h, j:j + w] for i in range(size) for j in range(size)], axis=0)
    valid = np.all(windows > -9999, axis=0)
    return np.where(valid, np.median(windows, axis=0), arr)


MESH_EDGE_TRIM_PIXELS = 3  # see _erode_valid_mask() — real user report of anomalies right at a genuine LIDAR coverage edge


def _erode_valid_mask(valid: np.ndarray, pixels: int) -> np.ndarray:
    """Shrinks a boolean valid-data mask inward by `pixels` in every
    direction (standard binary erosion — same stacked-window technique
    _median_smooth() already uses above, so no new dependency like scipy
    is needed for it). Used to crop the 3D mesh back a few pixels from any
    NoData transition before meshing: a real user report found visible
    anomalies (sensor/edge artifacts) clustering right at a genuine LIDAR
    coverage boundary — e.g. R32 E4F8's own real coverage edge, already
    documented in CLAUDE.md item 18 — which the existing 3x3
    `_median_smooth()` alone doesn't fully suppress, since it deliberately
    leaves a pixel untouched whenever ANY of its neighbours is NoData
    (correct for avoiding blending real elevation with "no data", but that
    also means a genuinely noisy real pixel right at the boundary survives
    smoothing unchanged).

    Treats anything outside the array's own bounds as invalid too
    (`mode="constant", constant_values=False` padding), so this also trims
    a few pixels off the mosaic's own outer edge uniformly — harmless and
    unnoticeable given `get_terrain_mesh()`'s own already-generous
    `MESH_CONTEXT_BUFFER_M` padding around the plot.
    """
    if pixels <= 0:
        return valid
    h, w = valid.shape
    padded = np.pad(valid, pixels, mode="constant", constant_values=False)
    eroded = np.ones((h, w), dtype=bool)
    for dr in range(2 * pixels + 1):
        for dc in range(2 * pixels + 1):
            eroded &= padded[dr:dr + h, dc:dc + w]
    return eroded


def _sample_elevation(arr, ext, resolution, itm_x, itm_y) -> Optional[float]:
    """Nearest-pixel elevation lookup at an arbitrary ITM point within an
    already-loaded mosaic array — used to find the real ground level under
    a building footprint. None if the point falls outside the mosaic's own
    extent, or lands on a real NoData pixel.
    """
    ext_left, ext_top, ext_right, ext_bottom = ext
    col = int(round((itm_x - ext_left) / resolution))
    row = int(round((ext_top - itm_y) / resolution))
    h, w = arr.shape
    if not (0 <= row < h and 0 <= col < w):
        return None
    v = arr[row, col]
    return None if v <= -9999 else float(v)


def _local_to_pixel(x: float, y: float, extent: tuple, texture_size: int) -> tuple:
    """Local (already center-relative) metres -> overlay texture pixel
    coordinates. Row 0 of the image = the north edge (largest y) — the
    same orientation get_terrain_mesh() already uses for its own grid
    (row 0 = ext_top) — so a line drawn here from these coordinates lines
    up with the mesh vertex at that same (x, y) without any extra flip
    logic needed on the frontend.
    """
    x_min, x_max, y_min, y_max = extent
    px = (x - x_min) / (x_max - x_min) * texture_size
    py = (y_max - y) / (y_max - y_min) * texture_size
    return px, py


def _meters_to_pixels(width_m: float, extent: tuple, texture_size: int) -> float:
    x_min, x_max, _, _ = extent
    return max(1.0, width_m / (x_max - x_min) * texture_size)


def _draw_plot_boundary(draw, plot_geom, center_x: float, center_y: float, extent: tuple, texture_size: int) -> None:
    """Draws every ring (outer + holes) of the plot polygon as a stroked
    line directly onto the overlay texture — the boundary is "ink on the
    surface", not geometry that has to be positioned relative to the
    terrain mesh, so there's no way for it to end up misaligned or
    disconnected from the ground the way a separate clipped mesh could.
    """
    width_px = int(round(_meters_to_pixels(BOUNDARY_LINE_WIDTH_M, extent, texture_size)))
    polys = plot_geom.geoms if hasattr(plot_geom, "geoms") else [plot_geom]
    for poly in polys:
        for ring in [poly.exterior, *poly.interiors]:
            pts = [_local_to_pixel(x - center_x, y - center_y, extent, texture_size) for x, y in ring.coords]
            if len(pts) >= 2:
                draw.line(pts, fill=BOUNDARY_LINE_COLOR, width=max(1, width_px), joint="curve")


def _draw_road_on_overlay(draw, road: dict, center_x: float, center_y: float, extent: tuple, texture_size: int) -> None:
    """Draws one OSM road/track path as a stroked line onto the overlay
    texture — same "ink on the surface" approach as the boundary, and
    directly why this fixes the earlier blocky/discontinuous 3D road
    ribbons: a single stroked polyline has no per-segment joints to be
    discontinuous at in the first place.
    """
    path_itm = [_to_itm.transform(lon, lat) for lon, lat in road["path_wgs84"]]
    if len(path_itm) < 2:
        return
    width_m = ROAD_WIDTH_M.get(road.get("highway_type"), DEFAULT_ROAD_WIDTH_M)
    width_px = int(round(_meters_to_pixels(width_m, extent, texture_size)))
    pts = [_local_to_pixel(x - center_x, y - center_y, extent, texture_size) for x, y in path_itm]
    draw.line(pts, fill=ROAD_LINE_COLOR, width=max(1, width_px), joint="curve")


def _extrude_building(building: dict, center_x: float, center_y: float, arr, ext, resolution) -> Optional[dict]:
    """One OSM building footprint -> a flat-roofed 3D prism (vertical walls
    + a triangulated roof cap, same shapely triangulation used for the
    terrain boundary above). A real, disclosed simplification — actual
    roofs aren't flat, and a sloped site means a real building's floor
    isn't perfectly level either — this is a site-scouting visual aid, not
    a survey-grade building model.

    The base sits at the LOWEST real elevation sampled anywhere under the
    footprint (every footprint vertex, plus the centroid) minus
    BUILDING_EMBED_M — not just the centroid's own single elevation. A
    building's real footprint isn't perfectly flat ground, and sampling
    only the centroid left the base floating above (or gapping from) the
    terrain wherever the rest of the footprint sat higher (or lower) than
    that one point — visible as buildings clearly not touching the ground
    in a real screenshot. Embedding below the lowest real point, not just
    matching it, means the base stays buried under the visible terrain
    surface everywhere under the footprint even on a real slope.
    """
    ring_itm = [_to_itm.transform(lon, lat) for lon, lat in building["footprint_wgs84"]]
    centroid_x = sum(x for x, y in ring_itm) / len(ring_itm)
    centroid_y = sum(y for x, y in ring_itm) / len(ring_itm)
    samples = [_sample_elevation(arr, ext, resolution, x, y) for x, y in ring_itm]
    samples.append(_sample_elevation(arr, ext, resolution, centroid_x, centroid_y))
    valid_samples = [s for s in samples if s is not None]
    if not valid_samples:
        return None  # outside this mosaic's own coverage, or a real LIDAR data gap under this building — skip rather than guess
    ground_m = min(valid_samples) - BUILDING_EMBED_M

    footprint = Polygon(ring_itm)
    if not footprint.is_valid:
        footprint = footprint.buffer(0)
    if footprint.is_empty:
        return None

    # Vertex z is HEIGHT ABOVE THIS BUILDING'S OWN GROUND (0 for the base
    # ring, height_m for the roof ring) rather than an absolute elevation —
    # deliberately, so the frontend can place the base on the terrain's
    # own (vertically-exaggerated, see terrain3d.html) surface via
    # ground_elevation_m below, then add the building's real, TRUE height
    # on top without it also getting stretched by that same exaggeration.
    # A real 6m building should look like a real 6m building next to a
    # deliberately-exaggerated slope, not get 3x taller along with it.
    height_m = building["height_m"]
    vertices: list = []
    faces: list = []

    def add_vertex(x: float, y: float, z: float) -> int:
        vertices.append([round(x - center_x, 2), round(y - center_y, 2), round(z, 2)])
        return len(vertices) - 1

    coords = list(footprint.exterior.coords)  # shapely always returns a closed ring (first == last)
    for i in range(len(coords) - 1):
        x0, y0 = coords[i]
        x1, y1 = coords[i + 1]
        bl, br = add_vertex(x0, y0, 0.0), add_vertex(x1, y1, 0.0)
        tl, tr = add_vertex(x0, y0, height_m), add_vertex(x1, y1, height_m)
        faces.append([bl, br, tr])
        faces.append([bl, tr, tl])

    try:
        roof_tris = shapely.constrained_delaunay_triangles(footprint)
    except Exception:
        roof_tris = None
    if roof_tris is not None:
        for tri in roof_tris.geoms:
            faces.append([add_vertex(x, y, height_m) for x, y in list(tri.exterior.coords)[:3]])

    return {
        "name": building.get("name"),
        "height_m": building["height_m"],
        "height_is_estimated": building["height_is_estimated"],
        "ground_elevation_m": round(ground_m, 2),
        "vertices": vertices,
        "faces": faces,
    }


def get_terrain_mesh(
    polygon_ring_sets_wgs84: list,
    buildings: Optional[list] = None,
    roads: Optional[list] = None,
) -> Optional[dict]:
    """A real triangle mesh (vertices + faces, not a height grid) of the
    real LIDAR elevation over a generous padded area around the plot (see
    the module comment above for why this is no longer clipped to the
    exact boundary), with the plot boundary painted onto it as a texture
    (`overlay_texture_png_base64` — apply as the mesh's own texture map;
    `grid_extent` gives the local-coordinate bounds needed to compute each
    vertex's own UV). `buildings`/`roads` — see
    buildings.get_nearby_features() — are optional; when given, buildings
    are extruded as real 3D objects (_extrude_building()) and roads are
    drawn onto the SAME overlay texture (not built as geometry — see the
    module comment). Everything shares one local (x, y) coordinate frame
    (metres east/north of `origin_lon`/`origin_lat`, returned so callers
    can reproduce the exact frame if needed) so terrain/buildings/the
    overlay all align without the browser needing to know anything about
    ITM or WGS84. Can also be attached later via attach_features() — see
    there for why (running the OSM fetch concurrently with this build).
    """
    center = mesh_center_and_radius(polygon_ring_sets_wgs84)
    if not center:
        return None
    center_lon, center_lat, radius_m = center
    center_x, center_y = _to_itm.transform(center_lon, center_lat)

    mosaic = _mosaic_dtm(center_lat, center_lon, radius_m)
    if not mosaic:
        log.info("-> No precise LIDAR coverage for this plot boundary")
        return None

    plot_geom = _ring_sets_to_itm_polygon(polygon_ring_sets_wgs84)
    if plot_geom is None or plot_geom.is_empty:
        return None

    arr = _median_smooth(mosaic["array"])  # see _median_smooth() — suppresses sensor-noise "tearing" in the lit 3D surface, not applied to the raw mosaic other callers use
    valid_mask = _erode_valid_mask(arr > -9999, MESH_EDGE_TRIM_PIXELS)  # crops a few pixels back from any NoData transition — see _erode_valid_mask()'s own docstring for why (real sensor/edge artifacts right at a genuine coverage boundary)
    ext = mosaic["ext"]
    ext_left, ext_top, ext_right, ext_bottom = ext
    resolution = mosaic["resolution"]
    height, width = arr.shape

    step = max(1, int(np.ceil(max(height, width) / MESH_MAX_GRID_SIZE)))
    row_indices = list(range(0, height, step))
    col_indices = list(range(0, width, step))
    grid_xs = [ext_left + c * resolution for c in col_indices]
    grid_ys = [ext_top - r * resolution for r in row_indices]
    nrows, ncols = len(row_indices), len(col_indices)

    vertices: list = []
    faces: list = []
    valid_heights: list = []
    vertex_cache: dict = {}

    def add_vertex(itm_x: float, itm_y: float, elev: float) -> int:
        key = (round(itm_x, 3), round(itm_y, 3))
        idx = vertex_cache.get(key)
        if idx is not None:
            return idx
        idx = len(vertices)
        vertices.append([round(itm_x - center_x, 2), round(itm_y - center_y, 2), round(float(elev), 2)])
        valid_heights.append(float(elev))
        vertex_cache[key] = idx
        return idx

    # A plain, uncipped rectangular grid — every cell whose 4 real corners
    # are valid (not NoData, and not within MESH_EDGE_TRIM_PIXELS of a
    # NoData transition — see _erode_valid_mask()) becomes 2 triangles,
    # full stop. No polygon test, no per-cell shapely call: dropping the
    # exact-clip approach (see the module comment above) means this loop
    # is now both simpler AND faster than the v2 version.
    for ri in range(nrows - 1):
        r0, r1 = row_indices[ri], row_indices[ri + 1]
        y0, y1 = grid_ys[ri], grid_ys[ri + 1]
        for ci in range(ncols - 1):
            c0, c1 = col_indices[ci], col_indices[ci + 1]
            x0, x1 = grid_xs[ci], grid_xs[ci + 1]
            if not (valid_mask[r0, c0] and valid_mask[r0, c1] and valid_mask[r1, c0] and valid_mask[r1, c1]):
                continue  # a real LIDAR data gap in or near this cell — never guess across NoData
            h00, h10, h01, h11 = arr[r0, c0], arr[r0, c1], arr[r1, c0], arr[r1, c1]
            i00, i10 = add_vertex(x0, y0, h00), add_vertex(x1, y0, h10)
            i01, i11 = add_vertex(x0, y1, h01), add_vertex(x1, y1, h11)
            faces.append([i00, i10, i01])
            faces.append([i10, i11, i01])

    if not vertices:
        log.info("-> LIDAR coverage exists nearby but no cell in this padded area had valid data")
        return None

    # How much of the PLOT BOUNDARY ITSELF (not the wider padded context
    # area around it — a gap out there is expected and not worth
    # flagging) actually has real rendered coverage — asked for directly
    # ("if we don't have everything in the property boundary we can warn
    # the user, if it's outside the boundary then no need"). Sampled on
    # the SAME row/col grid the mesh itself uses (not the full-resolution
    # array), so this reflects exactly what's actually rendered, including
    # the edge-trim above — a strip trimmed for artifact reasons should
    # count as "not shown", same as one that was genuinely NoData.
    grid_x_arr, grid_y_arr = np.meshgrid(grid_xs, grid_ys)  # real ITM coordinates — plot_geom is also in ITM, no reprojection needed
    inside_boundary = shapely.contains_xy(plot_geom, grid_x_arr, grid_y_arr)
    valid_sampled = valid_mask[np.ix_(row_indices, col_indices)]
    boundary_total = int(inside_boundary.sum())
    boundary_covered = int((inside_boundary & valid_sampled).sum())
    boundary_lidar_coverage_fraction = round(boundary_covered / boundary_total, 3) if boundary_total else None

    # Overlay texture: the plot boundary is drawn now (always available);
    # roads are drawn later by attach_features() once the concurrent OSM
    # fetch lands (see webapp.py) — the live PIL Image is kept in `_raw`
    # so that second pass can mutate it directly rather than starting over.
    extent = (
        grid_xs[0] - center_x, grid_xs[-1] - center_x,
        grid_ys[-1] - center_y, grid_ys[0] - center_y,
    )  # (x_min, x_max, y_min, y_max) in local coords — grid_ys[0] is the north edge (largest y)
    overlay_img = Image.new("RGB", (TEXTURE_SIZE, TEXTURE_SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(overlay_img)
    _draw_plot_boundary(draw, plot_geom, center_x, center_y, extent, TEXTURE_SIZE)

    def _encode_overlay() -> str:
        buf = io.BytesIO()
        overlay_img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    log.info(
        "-> Terrain mesh: %d vertices, %d triangles (%.1fm grid, %dm padded context around the plot), %.1f-%.1fm, %s%% of the plot boundary itself covered",
        len(vertices), len(faces), resolution * step, int(radius_m), min(valid_heights), max(valid_heights),
        "?" if boundary_lidar_coverage_fraction is None else round(boundary_lidar_coverage_fraction * 100, 1),
    )
    result = {
        "found": True,
        "vertices": vertices,
        "faces": faces,
        "min_elevation_m": round(min(valid_heights), 2),
        "max_elevation_m": round(max(valid_heights), 2),
        "cell_size_m": resolution * step,
        "resolution_m": resolution,
        "source": f"{mosaic['source_label']} LIDAR",
        "origin_lon": center_lon,
        "origin_lat": center_lat,
        "grid_extent": {"x_min": extent[0], "x_max": extent[1], "y_min": extent[2], "y_max": extent[3]},
        "boundary_lidar_coverage_fraction": boundary_lidar_coverage_fraction,
        "overlay_texture_png_base64": _encode_overlay(),
        "buildings": [],
        "road_count": 0,
        # Raw pieces, kept only long enough for attach_features() to ground
        # buildings and draw roads onto this SAME mesh/texture — stripped
        # before the result ever reaches webapp.py's jsonify() (see
        # attach_features() and api_terrain_mesh()). Not re-fetching the
        # LIDAR tiles a second time (they're disk-cached) is the point:
        # buildings.py's Overpass call and this mesh build now run
        # concurrently (see webapp.py), so the buildings/roads lookup
        # isn't available yet at the moment this function itself returns.
        "_raw": {
            "center_x": center_x, "center_y": center_y, "arr": arr, "ext": ext, "resolution": resolution,
            "extent": extent, "overlay_img": overlay_img, "encode_overlay": _encode_overlay,
        },
    }
    attach_features(result, buildings, roads)
    return result


def attach_features(result: dict, buildings: Optional[list] = None, roads: Optional[list] = None) -> dict:
    """Extrudes OSM buildings as real 3D objects and draws OSM roads onto
    the overlay texture (buildings.py) onto an already-built
    get_terrain_mesh() result, using its stashed `_raw` pieces — lets
    webapp.py fetch buildings/roads CONCURRENTLY with the mesh build
    itself (via a thread pool) rather than paying Overpass's latency
    strictly after the mesh is already done. Safe to call with nothing to
    attach (a no-op) or after `_raw` has already been stripped (also a
    no-op) — always returns `result` either way.
    """
    raw = result.get("_raw")
    if not raw:
        return result
    center_x, center_y, arr, ext, resolution = raw["center_x"], raw["center_y"], raw["arr"], raw["ext"], raw["resolution"]

    if buildings:
        placed = []
        for b in buildings:
            mesh = _extrude_building(b, center_x, center_y, arr, ext, resolution)
            if mesh:
                placed.append(mesh)
        result["buildings"] = placed
        log.info("-> %d/%d nearby building(s) placed on this terrain mesh (rest fell outside this mosaic's own coverage)",
                  len(placed), len(buildings))

    if roads:
        draw = ImageDraw.Draw(raw["overlay_img"])
        for r in roads:
            _draw_road_on_overlay(draw, r, center_x, center_y, raw["extent"], TEXTURE_SIZE)
        result["overlay_texture_png_base64"] = raw["encode_overlay"]()
        result["road_count"] = len(roads)
        log.info("-> %d nearby road(s) painted onto the terrain's overlay texture", len(roads))

    return result


# --- Satellite imagery drape: real aerial/satellite pixels painted onto
# the mesh, masked to the plot boundary only ---
#
# Asked for directly: replace the area inside the property boundary (not
# the whole padded mesh) with real satellite imagery, as a toggle. Same
# "ink on the surface" pattern as the boundary/roads texture and the flow
# overlay above — a transparent PNG applied as a SEPARATE mesh reusing the
# terrain's own shared geometry (identical vertex positions/UVs), so it's
# pixel-aligned by construction and can be toggled independently. Alpha is
# 0 everywhere outside the real plot polygon (the terrain's own hypsometric
# colour + boundary/roads texture shows through unchanged there) and 255
# inside it — the mask IS the plot's own real shape, not a bounding box.
#
# Source: the exact same public, unauthenticated Esri World Imagery XYZ
# tile service templates/index.html's own "Satellite" base layer already
# uses — confirmed live before use (real JPEG tiles returned at z16-18 for
# a known Fermoy-area test point). Fetched server-side (not left as
# Leaflet tiles) because the tiles need to be resampled into this app's
# own local mesh-coordinate frame, not shown as their own independent map.
#
# Reprojection is done per-OUTPUT-pixel (local mesh (x,y) -> ITM -> WGS84
# -> Web Mercator pixel space, all via pyproj's vectorized numpy transform
# — confirmed fast even at 1024x1024 = 1M+ points), not tile-by-tile —
# this is deliberately more robust than assuming ITM and WGS84 axes are
# perfectly aligned at this scale (they're close but not exactly, and a
# per-pixel transform is correct regardless of any small rotation, with no
# extra complexity over an approximate tile-grid alignment).
#
# Verified end-to-end (not just unit-by-unit) with a standalone prototype
# against a real cadastral boundary at Fermoy: rendered a real, correctly-
# oriented, correctly-shaped satellite image cropped exactly to the real
# (irregular, non-rectangular) parcel outline — visually confirmed against
# the same parcel's known outline from earlier boundary-clipping work
# (get_terrain_mesh()'s own module comment, "146x145 grid... irregular
# parcel outline" — this produces the identical shape via a completely
# different mechanism, a real cross-check).


def _mercator_pixel(lon: np.ndarray, lat: np.ndarray, zoom: int) -> tuple:
    """Standard Web Mercator slippy-map global pixel coordinates at a given
    zoom (256px tiles) — vectorized (numpy in, numpy out) so a whole
    texture's worth of points transforms in one call. The same formula
    every XYZ tile provider (Esri, OSM, etc.) uses; not something to
    re-derive per project, just applied here server-side instead of
    inside a map library.
    """
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n * SATELLITE_TILE_SIZE
    lat_rad = np.radians(lat)
    y = (1.0 - np.log(np.tan(lat_rad) + 1.0 / np.cos(lat_rad)) / np.pi) / 2.0 * n * SATELLITE_TILE_SIZE
    return x, y


def _pick_satellite_zoom(lat: float, span_m: float, texture_size: int) -> int:
    """Picks the smallest (least tile downloads) zoom level that's still at
    least as fine as the texture's own per-pixel resolution, capped so a
    large merged-parcel mesh can never balloon into an unreasonable tile
    count. Two independent bounds, take the smaller:
    - `z_for_resolution`: the finest zoom actually useful given the
      texture's own pixel density (no point fetching sharper imagery than
      the output texture can even show).
    - `z_for_tile_cap`: the coarsest zoom that keeps the fetch within
      SATELLITE_MAX_TILES_PER_SIDE per side, regardless of site size.
    Confirmed against a real 348m-wide test site (Fermoy): resolves to
    zoom 18, 25 tiles — well within budget.
    """
    lat_rad = math.radians(lat)
    numerator = 156543.03392 * math.cos(lat_rad)  # metres/pixel at zoom 0 at this latitude
    desired_m_per_px = span_m / texture_size
    z_for_resolution = math.ceil(math.log2(numerator / desired_m_per_px))
    z_for_tile_cap = math.floor(math.log2(SATELLITE_MAX_TILES_PER_SIDE * numerator * SATELLITE_TILE_SIZE / span_m))
    return max(1, min(SATELLITE_MAX_ZOOM, z_for_resolution, z_for_tile_cap))


def _fetch_satellite_tile(zoom: int, tile_x: int, tile_y: int) -> Optional[np.ndarray]:
    """One 256x256 RGB tile from Esri World Imagery, or None if it
    genuinely can't be fetched (a real network hiccup, or no imagery at
    this location/zoom) — a missing tile degrades to a transparent gap in
    the final overlay rather than failing the whole request, since this is
    a visual enhancement layer, not core report data (same philosophy as
    buildings.py's OSM fetch).
    """
    url = SATELLITE_TILE_URL.format(z=zoom, y=tile_y, x=tile_x)
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=SATELLITE_TILE_TIMEOUT_S)
            resp.raise_for_status()
            return np.array(Image.open(io.BytesIO(resp.content)).convert("RGB"))
        except Exception as exc:
            log.warning("Satellite tile fetch failed (attempt %d) for z=%d x=%d y=%d: %s", attempt + 1, zoom, tile_x, tile_y, exc)
    return None


def get_satellite_overlay(
    polygon_ring_sets_wgs84: list,
    origin_lon: float,
    origin_lat: float,
    grid_extent: dict,
    texture_size: int = SATELLITE_TEXTURE_SIZE,
) -> Optional[dict]:
    """Real Esri World Imagery, resampled into the exact same local
    mesh-coordinate frame get_terrain_mesh() already returned (`origin_lon`/
    `origin_lat`/`grid_extent` — pass back exactly what that call returned,
    so this overlay lines up on the SAME geometry/UVs with no risk of two
    independently-computed frames drifting apart), masked to alpha=0
    outside the real plot boundary polygon. None if the boundary itself is
    invalid (mirrors get_terrain_mesh()'s own contract) — a real tile-fetch
    failure does NOT fail the whole call, it just leaves transparent gaps
    (see `_fetch_satellite_tile()`).
    """
    plot_geom = _ring_sets_to_itm_polygon(polygon_ring_sets_wgs84)
    if plot_geom is None or plot_geom.is_empty:
        return None

    center_x, center_y = _to_itm.transform(origin_lon, origin_lat)
    x_min, x_max, y_min, y_max = grid_extent["x_min"], grid_extent["x_max"], grid_extent["y_min"], grid_extent["y_max"]

    # Same pixel-center convention as _local_to_pixel()'s own inverse: row 0
    # = north edge (largest y), so this lines up with the shared geometry's
    # own UVs with no extra flip.
    col_idx, row_idx = np.meshgrid(np.arange(texture_size), np.arange(texture_size))
    local_x = x_min + (col_idx + 0.5) / texture_size * (x_max - x_min)
    local_y = y_max - (row_idx + 0.5) / texture_size * (y_max - y_min)
    itm_x, itm_y = local_x + center_x, local_y + center_y
    lon, lat = _from_itm.transform(itm_x, itm_y)

    # _pick_satellite_zoom()'s tile-cap bound is an analytic estimate
    # assuming a perfectly axis-aligned square — the REAL tile range below
    # (from the actual per-pixel Mercator coordinates, which can be very
    # slightly skewed by ITM<->WGS84 not being exactly axis-aligned, plus
    # min/max rounding) can come out a little larger than that estimate
    # predicted. Confirmed live: a 1024px/348m test case with the estimate
    # landing exactly on the cap boundary actually needed 72 tiles, not 64.
    # So the cap is enforced here for real, by measuring the actual tile
    # range and stepping zoom down until it genuinely fits — not trusted
    # from the formula alone.
    span_m = max(x_max - x_min, y_max - y_min)
    zoom = _pick_satellite_zoom(origin_lat, span_m, texture_size)
    while True:
        merc_x, merc_y = _mercator_pixel(lon, lat, zoom)
        tile_x0, tile_x1 = int(merc_x.min() // SATELLITE_TILE_SIZE), int(merc_x.max() // SATELLITE_TILE_SIZE)
        tile_y0, tile_y1 = int(merc_y.min() // SATELLITE_TILE_SIZE), int(merc_y.max() // SATELLITE_TILE_SIZE)
        if (tile_x1 - tile_x0 + 1) <= SATELLITE_MAX_TILES_PER_SIDE and (tile_y1 - tile_y0 + 1) <= SATELLITE_MAX_TILES_PER_SIDE:
            break
        if zoom <= 1:
            break
        zoom -= 1
    tile_coords = [(tx, ty) for ty in range(tile_y0, tile_y1 + 1) for tx in range(tile_x0, tile_x1 + 1)]

    mosaic = np.zeros(((tile_y1 - tile_y0 + 1) * SATELLITE_TILE_SIZE, (tile_x1 - tile_x0 + 1) * SATELLITE_TILE_SIZE, 3), dtype=np.uint8)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tiles = list(pool.map(lambda t: (t, _fetch_satellite_tile(zoom, t[0], t[1])), tile_coords))
    fetched = 0
    for (tx, ty), tile in tiles:
        if tile is None:
            continue
        oy, ox = (ty - tile_y0) * SATELLITE_TILE_SIZE, (tx - tile_x0) * SATELLITE_TILE_SIZE
        mosaic[oy:oy + SATELLITE_TILE_SIZE, ox:ox + SATELLITE_TILE_SIZE] = tile
        fetched += 1

    origin_px, origin_py = tile_x0 * SATELLITE_TILE_SIZE, tile_y0 * SATELLITE_TILE_SIZE
    sample_col = np.clip((merc_x - origin_px).round().astype(int), 0, mosaic.shape[1] - 1)
    sample_row = np.clip((merc_y - origin_py).round().astype(int), 0, mosaic.shape[0] - 1)
    rgb = mosaic[sample_row, sample_col]

    inside = shapely.contains_xy(plot_geom, itm_x, itm_y)
    rgba = np.zeros((texture_size, texture_size, 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = np.where(inside, 255, 0).astype(np.uint8)

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    log.info("-> Satellite overlay: zoom %d, %d/%d tile(s) fetched, %.1f%% of texture inside the plot boundary",
              zoom, fetched, len(tile_coords), 100.0 * inside.mean())

    return {
        "found": True,
        "satellite_overlay_png_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
        "zoom_level": zoom,
        "tile_count": len(tile_coords),
        "source": "Esri World Imagery (ArcGIS Online)",
    }


# --- Water flow analysis: local minima (where water pools) and the
# drainage network (D8 flow direction + accumulation) that feeds them ---
#
# Standard, textbook computational hydrology, not a hand-rolled guess:
# D8 flow routing (each cell's water flows entirely to whichever of its 8
# neighbours has the steepest downhill slope) and flow accumulation (each
# cell's value = 1 + the accumulated value of every cell that drains into
# it, computed by processing cells from highest to lowest elevation so
# every upstream contributor is finalized before it's passed further
# down) are exactly how GIS hydrology tools derive a stream network from a
# DEM — this is the same class of decision as bilinear interpolation or
# ray-casting point-in-polygon elsewhere in this module: a simple, well-
# defined, easily-verified algorithm, not something at risk of being
# subtly wrong. Verified directly before use, not assumed: a synthetic
# 20x20 V-shaped valley draining to one corner gave a single sink exactly
# at the true basin bottom, with accumulation there equal to the full
# 400-cell grid (real mass conservation — every cell's water is accounted
# for somewhere), and accumulation increasing monotonically along the
# valley floor toward that outlet.
#
# TWO flow computations, deliberately, for two different questions.
# `_compute_flow_network()` (raw, unfilled D8) answers "where are the real
# local minima" — it's run on the ORIGINAL terrain specifically because
# filling would move/hide them (filling exists to route water THROUGH a
# basin, not to decide where a basin's own real floor is).
# `_fill_and_route()` (priority-flood depression filling, standard
# preprocessing real hydrology tools use) answers a different, later
# question, asked directly after the first (unfilled-only) version
# shipped: once a real sink fills up, where does the overflow actually
# GO? A sink that never fills is only half the picture for a real flood
# event — a shallow depression overflows almost immediately and its water
# continues downhill toward whatever's next, which the unfilled model
# can't show at all (flow just stops there). See `_fill_and_route()`'s own
# docstring for why it derives its own flow direction rather than
# re-running D8 on the filled result (a well-known "flat plateau" problem
# that plain D8 can't resolve, confirmed live, not assumed).
#
# Runs on the SAME (already median-smoothed) elevation array
# get_terrain_mesh() renders, at its native resolution — not the coarser
# downsampled mesh grid — since real terrain detail smaller than the
# mesh's own downsampling step can still genuinely redirect real water.
# A site's own padded mosaic is only ever a few hundred pixels per side,
# so full native-resolution flow routing is cheap regardless.

FLOW_CHANNEL_THRESHOLD_FRACTION = 0.005  # only draw a channel where at least this fraction of the site's own total valid area drains through it — a fixed cell-count threshold doesn't scale (confirmed live: 6 cells was fine for a small parcel's own grid but let almost every cell qualify on a bigger padded mosaic, drawing lines over ~90% of the surface instead of highlighting real channels)
FLOW_MIN_CONTRIBUTING_CELLS_FLOOR = 4  # absolute floor so a tiny site's threshold doesn't round down to 0-1 cells
WATER_COLOR = (30, 90, 180)  # one shared colour for every real "this is water" layer — flow lines AND flood pools, so a line and the lake it feeds read as the same substance, not two unrelated colours
FLOW_LINE_COLOR = WATER_COLOR
SINK_MARKER_COLOR = (200, 40, 40)
EXIT_MARKER_COLOR = (140, 140, 140)  # visually distinct from a real basin sink — "this is where our data ends", not "water pools here"
SINK_MARKER_RADIUS_M = 1.0
FLOW_SUPERSAMPLE = 3  # draw at 3x TEXTURE_SIZE then downsample (LANCZOS) for anti-aliased-looking lines — PIL's own line/ellipse drawing has no anti-aliasing at all


def _channel_threshold(valid_cell_count: int) -> int:
    return max(FLOW_MIN_CONTRIBUTING_CELLS_FLOOR, int(valid_cell_count * FLOW_CHANNEL_THRESHOLD_FRACTION))


def _compute_flow_network(arr: np.ndarray, resolution: float) -> tuple:
    """D8 flow direction + accumulation over `arr` (see the module comment
    above). Returns `(flow_acc, flow_to, is_sink)`, all the same shape as
    `arr` (`flow_to` has an extra trailing (dr, dc) axis): `flow_acc[r, c]`
    is the number of cells (including itself) whose water ultimately
    passes through (r, c); `flow_to[r, c]` is the (dr, dc) offset to
    whichever neighbour (r, c) drains into, or (-1, -1) if it's a sink;
    `is_sink[r, c]` is True where (r, c) has no lower valid neighbour at
    all (a real local minimum — water reaching it has nowhere further
    downhill to go) and isn't itself NoData.
    """
    h, w = arr.shape
    valid = arr > -9999
    diag = resolution * 1.4142135623730951
    neighbor_offsets = [
        (-1, -1, diag), (-1, 0, resolution), (-1, 1, diag),
        (0, -1, resolution), (0, 1, resolution),
        (1, -1, diag), (1, 0, resolution), (1, 1, diag),
    ]

    best_drop = np.zeros((h, w), dtype=np.float64)
    flow_to = np.full((h, w, 2), -1, dtype=np.int32)
    for dr, dc, dist in neighbor_offsets:
        shifted = np.roll(np.roll(arr, -dr, axis=0), -dc, axis=1)
        drop = (arr - shifted) / dist
        out_of_bounds = np.zeros((h, w), dtype=bool)
        if dr == -1:
            out_of_bounds[0, :] = True
        elif dr == 1:
            out_of_bounds[-1, :] = True
        if dc == -1:
            out_of_bounds[:, 0] = True
        elif dc == 1:
            out_of_bounds[:, -1] = True
        neighbor_valid = valid & np.roll(np.roll(valid, -dr, axis=0), -dc, axis=1) & ~out_of_bounds
        steeper = neighbor_valid & (drop > best_drop)
        best_drop = np.where(steeper, drop, best_drop)
        flow_to[steeper] = [dr, dc]

    is_sink = valid & (best_drop <= 0)

    flow_acc = np.where(valid, 1, 0).astype(np.int64)
    descending_order = np.argsort(-arr, axis=None)
    flat_flow_to = flow_to.reshape(h * w, 2)
    flat_acc = flow_acc.reshape(h * w)
    flat_valid = valid.reshape(h * w)
    for idx in descending_order:
        if not flat_valid[idx]:
            continue
        dr, dc = flat_flow_to[idx]
        if dr == -1 and dc == -1:
            continue
        r, c = divmod(idx, w)
        nr, nc = r + dr, c + dc
        if 0 <= nr < h and 0 <= nc < w:
            flat_acc[nr * w + nc] += flat_acc[idx]

    return flow_acc.reshape(h, w), flow_to, is_sink


def _draw_flow_network(draw, flow_acc: np.ndarray, flow_to: np.ndarray, arr: np.ndarray, ext: tuple,
                        resolution: float, center_x: float, center_y: float, extent: tuple,
                        texture_size: int, supersample: int) -> None:
    """Traces each channel's full path — from its head (a qualifying cell
    with no qualifying upstream neighbour of its own) downhill via
    `flow_to` to a sink or the edge of the array — and draws it as ONE
    connected polyline, not one independent segment per cell.

    The first version drew a separate 1-cell segment for every qualifying
    cell in isolation, which looked like converging arrowheads at every
    confluence (several short segments from different upstream directions
    all terminating at the same point, with no connecting line making
    them read as one channel) rather than a continuous stream — a real
    user report, with a screenshot. Tracing whole paths and drawing each
    as a single multi-point line fixes that directly: segments along one
    real flow path are now genuinely end-to-end connected. Stops tracing
    early if it reaches a cell another head's path already drew, to avoid
    needlessly re-stacking the same shared downstream tail many times.

    Width/opacity per segment is still log-scaled by that segment's own
    flow accumulation (real contributing-area values span orders of
    magnitude) — a single trickle stays thin and faint, a channel fed by
    many converging cells (per the user's own description — "a lot of
    these low point lines running into it") gets thicker and bolder.
    """
    h, w = arr.shape
    ext_left, ext_top = ext[0], ext[1]
    valid = arr > -9999
    max_acc = float(flow_acc[valid].max()) if valid.any() else 1.0
    min_contributing_cells = _channel_threshold(int(valid.sum()))
    log_max = np.log(max(max_acc, min_contributing_cells + 1))
    qualifies = valid & (flow_acc >= min_contributing_cells)

    # A channel head: qualifies, but no neighbour that flows INTO it also
    # qualifies (i.e. nothing upstream of it is itself part of a channel —
    # this is where a new channel starts, not a mid-stream point).
    has_qualifying_upstream = np.zeros((h, w), dtype=bool)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            points_here = qualifies & (flow_to[:, :, 0] == dr) & (flow_to[:, :, 1] == dc)
            has_qualifying_upstream |= np.roll(np.roll(points_here, dr, axis=0), dc, axis=1)
    channel_heads = qualifies & ~has_qualifying_upstream

    def to_pixel(r, c):
        x = ext_left + c * resolution
        y = ext_top - r * resolution
        return _local_to_pixel(x - center_x, y - center_y, extent, texture_size * supersample)

    drawn = np.zeros((h, w), dtype=bool)
    head_rows, head_cols = np.where(channel_heads)
    for r0, c0 in zip(head_rows.tolist(), head_cols.tolist()):
        r, c = r0, c0
        path = [(r, c)]
        while True:
            drawn[r, c] = True
            dr, dc = int(flow_to[r, c, 0]), int(flow_to[r, c, 1])
            if dr == -1 and dc == -1:
                break  # reached a sink
            nr, nc = r + dr, c + dc
            if not (0 <= nr < h and 0 <= nc < w) or not valid[nr, nc]:
                break  # flowed off the edge of this mosaic's own coverage
            path.append((nr, nc))
            if drawn[nr, nc]:
                break  # merges into a channel already traced from another head — no need to re-draw its tail again
            r, c = nr, nc

        if len(path) < 2:
            continue
        for i in range(len(path) - 1):
            acc = flow_acc[path[i][0], path[i][1]]
            norm = min(1.0, np.log(max(acc, 1)) / log_max) if log_max > 0 else 0.0
            width_px = max(1, int(round((1 + norm * 3) * supersample)))
            p0 = to_pixel(*path[i])
            p1 = to_pixel(*path[i + 1])
            draw.line([p0, p1], fill=(*FLOW_LINE_COLOR, min(255, 90 + int(norm * 165))), width=width_px, joint="curve")


MAX_REPORTED_SINKS = 20  # cap how many low points get their own marker — see get_flow_analysis()'s note on clustering/ranking


def _cluster_sinks(is_sink: np.ndarray, arr: np.ndarray, flow_acc: np.ndarray) -> list:
    """Groups 8-connected sink cells into single low points, each
    represented by its own LOWEST cell, ranked by that cluster's own
    catchment size (the sink cell's flow accumulation — how many upstream
    cells' water actually reaches it). Confirmed necessary, not
    theoretical: raw per-pixel D8 sink detection on a real (even
    median-smoothed) LIDAR array found 423 individual "sink" pixels
    across one ordinary parcel's padded mosaic — almost entirely flat
    micro-plateaus a few cm across (median filtering creates ties: many
    adjacent cells share the exact same value, and none of them has a
    STRICTLY lower neighbour, so the raw algorithm marks all of them as
    separate sinks). Clustering collapses each such plateau to one point;
    ranking by catchment size then means a genuine puddle-forming
    low point (fed by real upstream contributing area) sorts ahead of an
    isolated single-cell numerical blip with nothing draining into it.
    Returns `[(row, col, catchment_cells), ...]`, one tuple per cluster,
    sorted by catchment size descending.
    """
    h, w = is_sink.shape
    visited = np.zeros_like(is_sink)
    clusters = []
    rows, cols = np.where(is_sink)
    for r0, c0 in zip(rows.tolist(), cols.tolist()):
        if visited[r0, c0]:
            continue
        queue = deque([(r0, c0)])
        visited[r0, c0] = True
        cells = []
        while queue:
            r, c = queue.popleft()
            cells.append((r, c))
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < h and 0 <= nc < w and is_sink[nr, nc] and not visited[nr, nc]:
                        visited[nr, nc] = True
                        queue.append((nr, nc))
        lowest = min(cells, key=lambda rc: arr[rc[0], rc[1]])
        catchment = max(int(flow_acc[r, c]) for r, c in cells)
        clusters.append((lowest[0], lowest[1], catchment))
    clusters.sort(key=lambda t: -t[2])
    return clusters


POOL_ELEVATION_EPSILON_M = 1e-4  # cells within this of each other's filled elevation count as "the same pool"


def _label_pools(filled: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Connected-component labels (8-connected) for cells sharing
    (near-)identical FILLED elevation — i.e. the same real flat pool
    depression-filling created. -1 for a cell that isn't part of any
    shared pool (a lone cell whose own filled elevation doesn't match any
    neighbour — a channel cell, not the interior of a filled basin).

    Exists because of a real, confirmed bug in the naive version of
    interactive catchment selection (see get_flow_analysis()'s own note):
    two real sinks sharing the exact same pool (filled elevation
    21.586m, confirmed identical) showed wildly different, tiny "thin
    line" catchments (21 and 7 cells) when traced individually — each one
    only follows the ONE arbitrary branch of `_fill_and_route()`'s own
    spanning tree that happened to reach that specific pixel, not the
    pool's real total contributing area (confirmed correct once fixed:
    expanding to the whole 610-cell shared pool first and tracing from
    ALL of it gave 3106 cells — the real answer). Every cell in the same
    real pool needs to be treated as one unit for catchment tracing, not
    traced pixel-by-pixel.
    """
    h, w = filled.shape
    labels = np.full((h, w), -1, dtype=np.int32)
    visited = np.zeros((h, w), dtype=bool)
    next_label = 0
    for r0 in range(h):
        for c0 in range(w):
            if not valid[r0, c0] or visited[r0, c0]:
                continue
            visited[r0, c0] = True
            target = filled[r0, c0]
            queue = deque([(r0, c0)])
            component = [(r0, c0)]
            while queue:
                r, c = queue.popleft()
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        if dr == 0 and dc == 0:
                            continue
                        nr, nc = r + dr, c + dc
                        if (0 <= nr < h and 0 <= nc < w and valid[nr, nc] and not visited[nr, nc]
                                and abs(filled[nr, nc] - target) < POOL_ELEVATION_EPSILON_M):
                            visited[nr, nc] = True
                            queue.append((nr, nc))
                            component.append((nr, nc))
            if len(component) >= 2:
                for r, c in component:
                    labels[r, c] = next_label
                next_label += 1
    return labels


def _fill_and_route(arr: np.ndarray) -> tuple:
    """Priority-flood depression filling (Barnes et al. 2014 — the
    standard algorithm real hydrology tools use to prepare a DEM for
    basin-to-basin flow routing), which ALSO derives flow direction
    directly from its own fill order rather than re-deriving it from the
    filled elevations afterward.

    Asked for directly, after the first version (raw, unfilled D8 —
    see the module comment above) didn't answer the actual question: a
    real local minimum in reality fills with water until it spills over
    its own lowest rim, then that overflow continues downhill toward the
    next basin — not "flow just stops here forever", which is all a
    never-filled model can show. This fills every depression up to its
    own real spill (pour-point) elevation, so flow computed on the result
    correctly cascades from one former basin, over its rim, into whatever
    is downstream of it.

    Runs a standard priority-flood: starting from every border/NoData-
    adjacent cell (real, guaranteed exits) with its own elevation as
    priority, repeatedly pop the lowest still-open cell and "conquer" its
    unconquered neighbours — a neighbour lower than the conquering cell
    gets raised to its level (this raising IS the fill), and, critically,
    is also given a flow direction pointing straight back at whichever
    cell conquered it.

    That last part is why this does its OWN flow routing rather than
    reusing `_compute_flow_network()` on the filled result — confirmed
    live, not assumed: plain D8 on a filled array breaks on the flat
    plateaus filling deliberately creates (every cell in a pool shares the
    exact same elevation, so none has a strictly lower neighbour, and
    D8's steepest-descent test can't resolve a direction at all — the
    standard "flat resolution" problem in DEM hydrology). Recording each
    cell's flow direction AT THE MOMENT it's conquered sidesteps that
    entirely: a cell's conqueror is always its correct downhill neighbour
    by construction, whether or not their filled elevations happen to
    tie, so there's no flat-plateau ambiguity to resolve in the first
    place. Verified directly on a synthetic two-basin case (a shallow
    basin and a deeper one, separated by a saddle): the shallow basin's
    real bottom correctly gained a positive contributing-area count (28
    cells) instead of being an isolated dead end, and tracing its own
    flow direction chain led all the way out past the saddle, through the
    deeper basin, to the map's own edge — a real cascading path, not a
    basin that just stops.

    Returns `(filled, flow_to, flow_acc)` — `filled` has every depression
    raised to its own spill elevation; `flow_to`/`flow_acc` are shaped and
    used identically to `_compute_flow_network()`'s own (an (h, w, 2)
    direction array and an (h, w) contributing-cell-count array), so
    `_draw_flow_network()` needs no changes to consume either.
    """
    h, w = arr.shape
    valid = arr > -9999
    filled = arr.copy()
    closed = np.zeros((h, w), dtype=bool)
    flow_to = np.full((h, w, 2), -1, dtype=np.int32)
    pop_order = []
    heap = []
    counter = 0  # tie-breaker so heapq never has to compare (row, col) tuples when elevations tie
    for r in range(h):
        for c in range(w):
            if not valid[r, c]:
                closed[r, c] = True
                continue
            r0, r1 = max(0, r - 1), min(h, r + 2)
            c0, c1 = max(0, c - 1), min(w, c + 2)
            is_edge = r in (0, h - 1) or c in (0, w - 1) or not valid[r0:r1, c0:c1].all()
            if is_edge:
                heapq.heappush(heap, (float(filled[r, c]), counter, r, c))
                counter += 1
                closed[r, c] = True
    while heap:
        elev, _, r, c = heapq.heappop(heap)
        pop_order.append((r, c))
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if 0 <= nr < h and 0 <= nc < w and valid[nr, nc] and not closed[nr, nc]:
                    closed[nr, nc] = True
                    filled[nr, nc] = max(elev, arr[nr, nc])
                    flow_to[nr, nc] = [-dr, -dc]
                    heapq.heappush(heap, (float(filled[nr, nc]), counter, nr, nc))
                    counter += 1

    flow_acc = np.where(valid, 1, 0).astype(np.int64)
    for r, c in reversed(pop_order):  # children were always popped after their conqueror — reverse order finalizes each cell's own total before adding it to its parent's
        dr, dc = flow_to[r, c]
        if dr == -1 and dc == -1:
            continue
        flow_acc[r + dr, c + dc] += flow_acc[r, c]

    return filled, flow_to, flow_acc


FLOOD_EPSILON_M = 0.02  # a cell only counts as "would be flooded" if filling raised it by at least this much — keeps floating-point-noise-level non-differences from being flagged
FLOOD_POOL_COLOR = WATER_COLOR  # same colour as the flow lines — a line and the lake it feeds should read as the same substance
# Same RGB as the flow lines isn't enough on its own for them to actually
# LOOK the same colour — both layers get alpha-blended over the terrain's
# own varying colour underneath, so a translucent pool and a near-opaque
# line read as visibly different shades even with identical RGB. A real
# user report, confirmed by checking the numbers: the pool was fixed at
# 120/255 (~47% opaque) while a strong flow line reaches 255 (100%) — a
# big channel looked solid and saturated right next to a pale, washed-out
# lake. Raised to sit at the same end of the range a bold line reaches,
# not the line's own faint-trickle end.
FLOOD_POOL_ALPHA = 225

# code 1-8 = 1 + this list's own index; code 0 = a sink or NoData cell (no
# flow_to at all) — used to send the filled/routed flow direction grid to
# the frontend compactly (one small int per cell instead of two), so a
# click on a sink marker can reverse-trace that sink's own real catchment
# (every cell whose water eventually reaches it) entirely client-side —
# instant, no round trip per click, for an interactive multi-select tool.
FLOW_DIR_OFFSETS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _encode_flow_dir_codes(flow_to: np.ndarray) -> list:
    h, w, _ = flow_to.shape
    codes = np.zeros((h, w), dtype=np.int8)
    for i, (dr, dc) in enumerate(FLOW_DIR_OFFSETS):
        codes[(flow_to[:, :, 0] == dr) & (flow_to[:, :, 1] == dc)] = i + 1
    return codes.flatten().tolist()


def get_flow_analysis(polygon_ring_sets_wgs84: list) -> Optional[dict]:
    """Water flow analysis for a confirmed plot — a real flood-fill
    simulation, not just a static "flow stops here" snapshot: every real
    local minimum (sink) fills with water until it spills over its own
    lowest rim, and the overflow continues downhill toward whatever's
    next — asked for directly, after a first (unfilled) version only
    showed flow terminating at each basin with no way to see where an
    overflowing puddle would actually go next.

    Returns, for a confirmed plot's own padded terrain:
    - `sinks`: real local minima inside the plot boundary (clustered and
      ranked by catchment size — see `_cluster_sinks()`), each with its
      own `fill_depth_m` (how much water it holds before it spills over
      its own rim — a shallow depression overflows almost immediately, a
      real basin has real capacity) and `spill_elevation_m` (the level it
      fills to before that happens).
    - `flow_overlay_png_base64`: one transparent texture (same "paint it
      on the surface" approach as the boundary/roads overlay — see
      get_terrain_mesh()) with three layers composited together: the
      flooded-pool extent (every cell that would be underwater once
      every depression fills to its own spill point — literally what a
      flood event looks like on this terrain), the cascading overflow
      network on top (computed on the FILLED terrain via `_fill_and_route()`,
      so paths correctly continue past a former sink's rim into whatever
      basin is downstream of it, not stopping there), and sink markers.

    A real, disclosed simplification, same spirit as everywhere else in
    this app: D8-style flow routing on real (median-smoothed) LIDAR
    terrain is a genuine, standard hydrological technique, but it's still
    a model of where water WOULD go on this exact surface shape — not a
    substitute for an actual site drainage survey, and it says nothing
    about subsurface drainage, soil permeability, or engineered drainage
    already on site.

    Sinks are clustered (`_cluster_sinks()`, on the UNFILLED terrain —
    filling exists to route the overflow, not to decide where the real
    low points are) and ranked by real catchment size before being
    returned, not reported per raw pixel — confirmed necessary: naive
    per-pixel sink detection on one ordinary parcel found 423 individual
    "sink" pixels, almost all flat micro-plateaus a few cm across left
    over from median smoothing, not meaningfully distinct low points.
    `total_sinks_found` (the count actually inside the plot boundary,
    after clustering but before the `MAX_REPORTED_SINKS` cap) is returned
    alongside `sinks` (capped, ranked) so the frontend can be honest about
    how many exist even when only showing the top ones.
    """
    center = mesh_center_and_radius(polygon_ring_sets_wgs84)
    if not center:
        return None
    center_lon, center_lat, radius_m = center
    center_x, center_y = _to_itm.transform(center_lon, center_lat)

    mosaic = _mosaic_dtm(center_lat, center_lon, radius_m)
    if not mosaic:
        return None

    plot_geom = _ring_sets_to_itm_polygon(polygon_ring_sets_wgs84)
    if plot_geom is None or plot_geom.is_empty:
        return None

    arr = _median_smooth(mosaic["array"])
    ext = mosaic["ext"]
    ext_left, ext_top, ext_right, ext_bottom = ext
    resolution = mosaic["resolution"]
    valid = arr > -9999

    # Raw (unfilled) flow: decides where the real local minima are.
    _raw_flow_acc, _raw_flow_to, is_sink = _compute_flow_network(arr, resolution)
    clustered = _cluster_sinks(is_sink, arr, _raw_flow_acc)  # sorted by catchment size, descending

    # Filled + routed flow: decides where an overflowing sink's water
    # actually goes next — see _fill_and_route()'s own docstring for why
    # this needs its own flow-direction derivation, not plain D8 rerun on
    # the filled result.
    filled, flow_to, flow_acc = _fill_and_route(arr)
    flooded = valid & (filled > arr + FLOOD_EPSILON_M)

    sinks = []
    total_inside = 0
    for r, c, catchment in clustered:
        x = ext_left + c * resolution
        y = ext_top - r * resolution
        if not plot_geom.covers(Point(x, y)):
            continue
        total_inside += 1
        if len(sinks) >= MAX_REPORTED_SINKS:
            continue  # keep counting (for total_found below) but stop adding markers past the cap
        bottom_m = float(arr[r, c])
        spill_m = float(filled[r, c])
        sinks.append({
            "row": r,
            "col": c,
            "x": round(x - center_x, 2),
            "y": round(y - center_y, 2),
            "elevation_m": round(bottom_m, 2),
            "catchment_cells": catchment,
            "spill_elevation_m": round(spill_m, 2),
            "fill_depth_m": round(spill_m - bottom_m, 2),
            "is_exit": False,
        })

    # Exit points: every cell where a drawn channel just runs off the
    # edge of this mosaic's own analyzed area — asked for directly, so a
    # channel that visibly "stops" at the texture's own boundary can be
    # clicked to see its catchment too, same as a real basin sink.
    # Confirmed directly what these actually are before adding them: in
    # `_fill_and_route()`'s filled/routed graph, code 0 (`flow_to == (-1,-1)`)
    # occurs ONLY for the cells that seeded the fill in the first place —
    # the mosaic's own border, or cells next to a real internal NoData
    # gap in the LIDAR data (confirmed live: 7,572 such cells at Fermoy,
    # 698 on the literal border and the rest against internal NoData —
    # zero "leftover" unrouted local minima, since filling connects every
    # real basin bottom onward by construction). Only the ones actually
    # carrying a meaningful channel (the same `_channel_threshold()` used
    # to decide whether to draw a line there at all) are worth marking —
    # otherwise every one of the mosaic's ~700 border pixels would get its
    # own marker regardless of whether any real water reaches it.
    threshold = _channel_threshold(int(valid.sum()))
    already_marked = {(s["row"], s["col"]) for s in sinks}
    exit_candidates = []
    exit_rows, exit_cols = np.where(valid & (flow_to[:, :, 0] == -1) & (flow_to[:, :, 1] == -1) & (flow_acc >= threshold))
    for r, c in zip(exit_rows.tolist(), exit_cols.tolist()):
        if (r, c) in already_marked:
            continue
        exit_candidates.append((r, c, int(flow_acc[r, c])))
    exit_candidates.sort(key=lambda t: -t[2])
    for r, c, catchment in exit_candidates[:MAX_REPORTED_SINKS]:
        x = ext_left + c * resolution
        y = ext_top - r * resolution
        sinks.append({
            "row": r,
            "col": c,
            "x": round(x - center_x, 2),
            "y": round(y - center_y, 2),
            "elevation_m": round(float(filled[r, c]), 2),
            "catchment_cells": catchment,
            "spill_elevation_m": None,
            "fill_depth_m": None,
            "is_exit": True,
        })

    extent = (
        ext_left - center_x, ext_right - center_x,
        ext_bottom - center_y, ext_top - center_y,
    )
    # Drawn at FLOW_SUPERSAMPLE x the final texture size, then downsampled
    # with LANCZOS resampling — plain PIL line/ellipse drawing has no
    # anti-aliasing at all, which combined with the first version's
    # disconnected per-cell segments (see _draw_flow_network()'s own
    # comment) made the lines look visibly blocky/pixelated even after
    # fixing the connectivity — a real user report. Supersample-then-
    # downsample is the standard, simple way to get anti-aliased-looking
    # lines out of a drawing API with none built in.
    ss = FLOW_SUPERSAMPLE
    canvas_size = (TEXTURE_SIZE * ss, TEXTURE_SIZE * ss)
    overlay_img = Image.new("RGBA", canvas_size, (0, 0, 0, 0))

    if flooded.any():
        # A plain nearest-neighbour view of `flooded` scaled up would look
        # like a blocky mask — resizing through the SAME supersample ->
        # LANCZOS-downsample pipeline as the lines/markers below gives it
        # smooth, anti-aliased edges instead, and keeps every layer of
        # this texture consistent.
        mask_img = Image.fromarray((flooded.astype(np.uint8) * 255), mode="L").resize(canvas_size, Image.LANCZOS)
        pool_layer = Image.new("RGBA", canvas_size, (*FLOOD_POOL_COLOR, 0))
        pool_layer.putalpha(mask_img.point(lambda v: int(v * FLOOD_POOL_ALPHA / 255)))
        overlay_img = Image.alpha_composite(overlay_img, pool_layer)

    draw = ImageDraw.Draw(overlay_img, "RGBA")
    _draw_flow_network(draw, flow_acc, flow_to, arr, ext, resolution, center_x, center_y, extent, TEXTURE_SIZE, ss)
    marker_r = max(2, int(round(_meters_to_pixels(SINK_MARKER_RADIUS_M, extent, TEXTURE_SIZE) * ss)))
    for s in sinks:
        px, py = _local_to_pixel(s["x"], s["y"], extent, TEXTURE_SIZE * ss)
        color = EXIT_MARKER_COLOR if s["is_exit"] else SINK_MARKER_COLOR
        draw.ellipse([px - marker_r, py - marker_r, px + marker_r, py + marker_r], fill=(*color, 230))
    overlay_img = overlay_img.resize((TEXTURE_SIZE, TEXTURE_SIZE), Image.LANCZOS)

    buf = io.BytesIO()
    overlay_img.save(buf, format="PNG")

    log.info("-> Flow analysis: %d low point(s) inside the plot boundary + %d channel exit point(s) (showing %d of %d total markers), %d cell(s) would flood",
              total_inside, len(exit_candidates), len(sinks), total_inside + len(exit_candidates), int(flooded.sum()))

    rows, cols = arr.shape
    return {
        "found": True,
        "sinks": sinks,
        "total_sinks_found": total_inside,
        "total_exit_points_found": len(exit_candidates),
        "flooded_area_m2": round(float(flooded.sum()) * resolution * resolution, 1),
        "grid_extent": {"x_min": extent[0], "x_max": extent[1], "y_min": extent[2], "y_max": extent[3]},
        "flow_overlay_png_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
        # For client-side catchment tracing (click a sink -> highlight
        # everywhere its water comes from, no round trip per click — see
        # FLOW_DIR_OFFSETS): the SAME filled/routed direction grid the
        # drawn network itself uses, encoded compactly (see
        # _encode_flow_dir_codes()), plus the grid's own shape so the
        # frontend can map a flat index back to (row, col) and, via
        # `grid_extent`, to local (x, y).
        "rows": rows,
        "cols": cols,
        "cell_size_m": resolution,
        "flow_dir_codes": _encode_flow_dir_codes(flow_to),
        # Which cells share the same real flat pool (see _label_pools()'s
        # own note on why this is needed — without it, two sinks sharing
        # one pool trace back through two different arbitrary slivers of
        # the fill algorithm's own spanning tree instead of that pool's
        # real combined catchment). -1 = not part of any shared pool.
        "pool_labels": _label_pools(filled, valid).flatten().tolist(),
    }
