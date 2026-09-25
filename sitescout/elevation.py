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

**Where none of LIDAR_SOURCES covers a point at all (~70% of the
country), `_mosaic_dtm()` now falls back to Copernicus GLO-30** — a free,
public, global 30m satellite-derived digital SURFACE model (see the
dedicated comment block above `_sample_copernicus_dem()` further down for
the full writeup, including how it was confirmed live before use). Real
data, clearly disclosed via `"kind": "satellite_dem"` on the mosaic dict
(vs `"lidar"`) and surfaced to callers as `elevation_kind` — not a bare-
ground DTM the way every LIDAR_SOURCES entry is, and far coarser (30m vs
~2m), but a real number instead of a flat placeholder or a vague contour
range. `get_terrain_mesh()` requests a FINER, bilinearly-interpolated grid
(`COPERNICUS_DEM_MESH_RESOLUTION_M`, see `_sample_copernicus_dem()`'s own
docstring) rather than the raw 30m nearest-neighbour default — a real
user report showed the raw 30m grid could be just a handful of pixels
across a typical small plot, and the mesh pipeline's own edge-erosion
margins (sized for ~2m LIDAR) then ate the whole thing. `get_flow_analysis()`
is the one consumer that opts OUT of this fallback entirely: D8 routing
on a DSM (includes canopy/building noise, not bare ground) would fabricate
sinks that don't reflect real topography, a genuinely different problem
interpolation can't fix — see its own docstring.

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
from scipy.spatial import cKDTree
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

# ## Copernicus GLO-30 — global fallback for wherever no real LIDAR_SOURCES
# entry has any coverage at all (~70% of Ireland — see the module docstring
# above). A free, public, global 30m digital SURFACE model (DSM — includes
# vegetation/building height, NOT bare ground the way OPW's own DTM is; a
# real difference in KIND, not just resolution, disclosed via "kind" on
# every mosaic dict below) derived from the TanDEM-X radar mission, fused
# with SRTM/ALOS/ASTER/TerraSAR-X where needed. Confirmed live before use,
# same discipline as every other source in this app:
# - Fully public over plain HTTPS, no AWS credentials/signing — both this
#   project's own long-standing test tiles (R32 E4F8, Fermoy) returned a
#   real 200 with no auth headers sent at all.
# - Real georeferenced data, not a placeholder: downloaded and decoded the
#   R32 E4F8 tile directly (tifffile + imagecodecs — this source's COG
#   compression needs imagecodecs, which plain tifffile can't decode
#   alone, see requirements.txt) and read a real value at that exact
#   point: 97.77m, with a smooth, realistic 94-99m neighbourhood gradient.
# NoData sentinel (-32767) and DSM-not-DTM nature confirmed via the
# Copernicus DEM product handbook, not assumed.
#
# Tile lookup is fully deterministic (no coverage-index query needed at
# all, unlike every LIDAR_SOURCES entry) — one file per whole degree of
# lat/lon, named directly from floor(lat)/floor(lon). Native tiles are
# WGS84 (lat/lon degrees), not ITM like every other source in this module,
# and its column count genuinely varies by latitude band (fewer samples
# per degree of longitude toward the poles, to keep real ground pixel size
# close to 30m everywhere) — confirmed by actually reading a tile's own
# GeoTIFF tags rather than assuming a fixed shape. So rather than forcing
# it into the ITM-tile-grid stitching `_mosaic_dtm()` uses for real LIDAR,
# `_sample_copernicus_dem()` uses the SAME per-pixel-reprojection technique
# already established by `get_satellite_overlay()`/`_fetch_map_mosaic()`:
# build the OUTPUT grid in the frame callers actually want (ITM, matching
# every other mosaic in this module), reproject it to WGS84 in one
# vectorized pyproj call, and sample each pixel from whichever GLO-30
# tile(s) it falls in.
COPERNICUS_DEM_BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
COPERNICUS_DEM_SOURCE_LABEL = "Copernicus GLO-30"
COPERNICUS_DEM_NODATA_THRESHOLD = -30000.0  # real sentinel is exactly -32767; a safe threshold (not an exact match) is fine since no real Irish elevation comes remotely close to this
COPERNICUS_DEM_RESOLUTION_M = 30.0  # our own resampled OUTPUT grid's cell size; each source tile's real native pixel scale (which varies by latitude) is read from its own tags for the actual sampling
COPERNICUS_DEM_MESH_RESOLUTION_M = 5.0  # get_terrain_mesh()'s own finer, bilinearly-interpolated request — see _sample_copernicus_dem()'s docstring for why a small plot needs this instead of the coarser 30m grid
COPERNICUS_DEM_SURVEY = "TanDEM-X radar survey, fused with SRTM/ALOS/ASTER/TerraSAR-X"

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


def _query_source_tiles(source_label: str, coverage_url: str, dtm_pattern: str, dsm_pattern: Optional[str], nodata: float, lon: float, lat: float, search_radius_m: float) -> Optional[list[dict]]:
    """The shared per-source coverage-index query + tile download behind
    both `_find_touching_tiles()` (single best source, for a point-value
    query) and `_find_all_touching_tiles()` (every qualifying source,
    layered together, for an area/mesh build). Returns every tile from
    this ONE source touching `search_radius_m` around (lat, lon) —
    unfiltered by whether any of them actually has real data, since the
    two callers apply different "does this qualify" logic — or `None` if
    the coverage-index query itself failed (a different case from "zero
    tiles found": see callers, which retry the next source either way but
    log it differently).
    """
    log.info("Checking %s LIDAR coverage within %dm…", source_label, search_radius_m)
    try:
        # return_geometry=True: needed as a fallback for sources whose
        # coverage index has no EXT_* attributes at all (confirmed needed
        # for TII) — see _tile_extent(). out_fields="*", not a named list:
        # confirmed live that TII's own layer throws a hard "Failed to
        # execute query" error (not a silent ignore) when asked for EXT_*
        # fields it doesn't have in its schema — requesting everything
        # sidesteps needing to know each source's exact field names up
        # front.
        feats = point_query(
            coverage_url, lon, lat, out_fields="*",
            distance_m=search_radius_m, result_record_count=16, return_geometry=True,
        )
    except Exception as exc:
        log.warning("-> %s coverage query failed: %s", source_label, exc)
        return None
    if not feats:
        return []

    tiles = []
    for f in feats:
        ext = _tile_extent(f)
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
    return tiles


def _find_touching_tiles(lat: float, lon: float, radius_m: float) -> tuple[Optional[str], list[dict]]:
    """The FIRST LIDAR_SOURCES entry (priority order) whose own tile
    genuinely covers the EXACT point with real (non-NoData) data,
    intersecting a radius_m buffer around it — used for a single-pixel
    read (`get_precise_elevation()`/`render_dtm_image()`, via
    `_mosaic_dtm()`'s default `require_data_at_point=True`), where "does
    THIS exact point have real data" is exactly the right question and a
    single best source is all that's needed.

    `get_terrain_mesh()`/`get_flow_analysis()` do NOT use this — see
    `_find_all_touching_tiles()` for why an area build needs every
    qualifying source layered together, not just the first one that
    happens to have any real data nearby.

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

    "Does a source actually cover the exact point" is answered with a
    plain bbox check against each tile's own extent (see _tile_extent())
    rather than a second query: these coverage-index features are plain
    axis-aligned squares, so their extent IS their exact boundary — no
    geometry library needed for an exact (not approximate) point-in-tile
    test, same reasoning as the karst/contour decisions not to hand-roll
    real polygon geometry elsewhere in this app, just simpler here because
    these shapes are trivial rectangles.
    """
    search_radius_m = radius_m * 1.5  # > radius_m * sqrt(2) (~1.4142), a bit of extra margin against rounding
    itm_x, itm_y = _to_itm.transform(lon, lat)
    for source_label, coverage_url, dtm_pattern, dsm_pattern, nodata in LIDAR_SOURCES:
        tiles = _query_source_tiles(source_label, coverage_url, dtm_pattern, dsm_pattern, nodata, lon, lat, search_radius_m)
        if not tiles:
            continue

        contains_point = any(t["ext"][0] <= itm_x <= t["ext"][2] and t["ext"][3] <= itm_y <= t["ext"][1] for t in tiles)
        if not contains_point:
            log.info("-> %s has tiles nearby but none covering the exact point — trying next source", source_label)
            continue
        if not _point_has_real_data(tiles, itm_x, itm_y):
            # A tile's bounding box containing the point is NOT the same as
            # that exact pixel having real data — confirmed live: a real
            # query point sat inside an OPW NASC tile's own square, but its
            # survey (like TII's corridor survey, or any real LIDAR flight)
            # doesn't fill every tile edge-to-edge, so the actual pixel
            # there was NoData. Committing to this source anyway (the
            # original behaviour) would report "no coverage" even when a
            # LOWER-priority source (confirmed: TII, at this exact point)
            # has real data — exactly undoing the point of having multiple
            # sources. So: fall through to the next source instead of
            # stopping at the first bbox match.
            log.info("-> %s tile bounding box covers the point, but that exact pixel is NoData — trying next source", source_label)
            continue

        log.info("-> %s: %d tile(s) found (search radius %dm, for a %dm-radius square crop)", source_label, len(tiles), search_radius_m, radius_m)
        return source_label, tiles

    return None, []


def _find_all_touching_tiles(lat: float, lon: float, radius_m: float) -> list[tuple[str, list[dict]]]:
    """EVERY LIDAR_SOURCES entry with any real data touching a radius_m
    buffer around (lat, lon), in priority order — not just the first one,
    unlike `_find_touching_tiles()`. Used by the AREA/mesh build
    (`get_terrain_mesh()`/`get_flow_analysis()`, via `_mosaic_dtm()`'s
    `require_data_at_point=False`), asked for directly after a real user
    report: a large or partially-covered plot (a golf course, south of
    R32 E4F8) was rendering as almost entirely Copernicus GLO-30 even
    though real LIDAR genuinely exists nearby.

    **The actual bug, confirmed live, not assumed** — a real architectural
    gap in the single-source design, not a data-coverage problem: the
    OLD logic picked the first source with ANY real data anywhere in the
    wide search buffer and committed to using ONLY that source's tiles for
    the whole mosaic. For this exact golf-course plot, OPW NASC's own
    2km survey tile touches the search buffer and has SOME real pixels in
    it (so the old check passed) — but that tile's real coverage sits
    entirely outside the actual crop window this specific plot needs.
    Confirmed directly: the resulting crop was 0% real LIDAR, 100%
    Copernicus GLO-30 fill, while a genuinely better-covering source may
    have existed for the exact same area — the code never checked,
    because it had already committed. "If there's any LIDAR, prioritize
    it and stitch the gaps with GLO-30" (asked for directly) requires
    checking every source's REAL coverage of the actual crop, not
    stopping at the first source with real data ANYWHERE nearby.

    Returns a list, not a single winner — `_mosaic_dtm()` layers every
    source's tiles onto ONE shared canvas in the order returned here
    (LIDAR_SOURCES priority order), each source only filling cells the
    higher-priority ones before it left empty. A source with tiles nearby
    but zero real data anywhere in them is skipped entirely (no point
    carrying dead weight into the layering step) — same
    `_tiles_have_any_real_data()` check `_find_touching_tiles()` used to
    do before committing, just no longer a reason to stop searching
    other sources.
    """
    search_radius_m = radius_m * 1.5  # see _find_touching_tiles()'s own docstring for why sqrt(2), not 1.0
    results = []
    for source_label, coverage_url, dtm_pattern, dsm_pattern, nodata in LIDAR_SOURCES:
        tiles = _query_source_tiles(source_label, coverage_url, dtm_pattern, dsm_pattern, nodata, lon, lat, search_radius_m)
        if not tiles:
            continue
        if not _tiles_have_any_real_data(tiles):
            log.info("-> %s has tiles in the search radius, but none contain any real data at all — skipping", source_label)
            continue
        log.info("-> %s: %d tile(s) found (search radius %dm, for a %dm-radius square crop)", source_label, len(tiles), search_radius_m, radius_m)
        results.append((source_label, tiles))
    return results


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


def _tiles_have_any_real_data(tiles: list) -> bool:
    """Whether ANY pixel in ANY of these tiles has real (non-NoData)
    elevation data — the area-search equivalent of `_point_has_real_data()`,
    used by `get_terrain_mesh()` (see `_find_touching_tiles()`'s
    `require_data_at_point=False` mode) where the question isn't "does
    THIS exact point have data" but "is this source worth mosaicking at
    all here" — a source whose nearby tiles are 100% NoData throughout
    should still fall through to the next source in priority order, same
    spirit as `_point_has_real_data()`'s own fallback, just checked over
    the whole tile instead of one pixel.
    """
    for t in tiles:
        arr = tifffile.imread(str(t["dtm_path"]))
        if (arr > t.get("nodata", -9999.0)).any():
            return True
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


def _copernicus_tile_name(lat_floor: int, lon_floor: int) -> str:
    ns = f"N{lat_floor:02d}" if lat_floor >= 0 else f"S{-lat_floor:02d}"
    ew = f"E{lon_floor:03d}" if lon_floor >= 0 else f"W{-lon_floor:03d}"
    return f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"


# In-process only (not disk-persisted, unlike the raw .tif cache below) —
# avoids re-decoding the same ~30MB compressed tile on every request within
# one running server, since (unlike OPW's uncompressed LIDAR tiles)
# tifffile can't memmap this source's compressed COGs, so every read is a
# real full decode. A handful of decoded tiles (Ireland spans roughly 8-10
# whole-degree cells) is a trivial amount of memory to hold for the life of
# the process.
_copernicus_tile_cache: dict = {}


def _get_copernicus_tile(lat_floor: int, lon_floor: int) -> Optional[dict]:
    """Downloads (if not already cached on disk — same _atomic_write
    convention as every other tile in this module) and decodes one GLO-30
    1-degree tile. Returns None, not a raised exception, for a cell with no
    tile (confirmed some open-ocean cells simply 404) or any download/decode
    failure — a caller sampling a small area near a tile edge should treat
    one missing neighbour as "no data from that tile", not fail outright.
    """
    key = (lat_floor, lon_floor)
    if key in _copernicus_tile_cache:
        return _copernicus_tile_cache[key]

    name = _copernicus_tile_name(lat_floor, lon_floor)
    path = _cache_path(COPERNICUS_DEM_SOURCE_LABEL, f"{name}.tif")
    if not path.exists():
        url = f"{COPERNICUS_DEM_BASE_URL}/{name}/{name}.tif"
        log.info("Downloading Copernicus GLO-30 tile (%s)…", url)
        try:
            resp = requests.get(url, timeout=TILE_DOWNLOAD_TIMEOUT_S)
            resp.raise_for_status()
        except Exception as exc:
            log.info("-> no GLO-30 tile at %s (%s)", name, exc)
            _copernicus_tile_cache[key] = None
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, resp.content)

    try:
        with tifffile.TiffFile(str(path)) as tf:
            page = tf.pages[0]
            tags = {t.name: t.value for t in page.tags}
            tiepoint = tags["ModelTiepointTag"]  # pixel (0,0) -> (origin_lon, origin_lat) — real per-tile georeferencing, not assumed
            scale = tags["ModelPixelScaleTag"]
            arr = tf.asarray().astype(np.float32)
    except Exception as exc:
        log.warning("-> failed to decode GLO-30 tile %s: %s", name, exc)
        _copernicus_tile_cache[key] = None
        return None

    result = {"array": arr, "origin_lon": tiepoint[3], "origin_lat": tiepoint[4], "scale_x": scale[0], "scale_y": scale[1]}
    _copernicus_tile_cache[key] = result
    return result


def _bilinear_sample_copernicus(tile: dict, lons: np.ndarray, lats: np.ndarray) -> np.ndarray:
    """Real bilinear interpolation between GLO-30's own 4 nearest source
    pixels — a standard, well-defined technique (the same class of
    decision as this module's other interpolation uses, e.g.
    `_extrude_building()`'s own bilinear height lookup), not a hand-wave.
    This is what makes resampling GLO-30 onto a FINER output grid (see
    `mesh_resolution` on `_sample_copernicus_dem()`) a genuinely smooth
    surface rather than a blocky staircase of giant flat 30m quads — still
    honestly only as much real information as 30m carries, just not
    needlessly jagged between the real known points.

    Falls back to plain nearest-neighbour for any point where one of the 4
    real neighbours is NoData — never blends a real value with -9999 (the
    same "never guess across NoData" rule `_median_smooth()` already
    follows elsewhere in this module), so a genuine coverage edge still
    reads as a real edge, not a smeared, partially-fabricated one.
    """
    tile_arr = tile["array"]
    th, tw = tile_arr.shape
    fcol = (lons - tile["origin_lon"]) / tile["scale_x"] - 0.5
    frow = (tile["origin_lat"] - lats) / tile["scale_y"] - 0.5
    col0 = np.clip(np.floor(fcol).astype(int), 0, tw - 1)
    row0 = np.clip(np.floor(frow).astype(int), 0, th - 1)
    col1 = np.clip(col0 + 1, 0, tw - 1)
    row1 = np.clip(row0 + 1, 0, th - 1)
    wx = np.clip(fcol - col0, 0, 1)
    wy = np.clip(frow - row0, 0, 1)

    v00, v01 = tile_arr[row0, col0], tile_arr[row0, col1]
    v10, v11 = tile_arr[row1, col0], tile_arr[row1, col1]
    nodata = COPERNICUS_DEM_NODATA_THRESHOLD
    all_valid = (v00 > nodata) & (v01 > nodata) & (v10 > nodata) & (v11 > nodata)

    interpolated = v00 * (1 - wx) * (1 - wy) + v01 * wx * (1 - wy) + v10 * (1 - wx) * wy + v11 * wx * wy
    nearest = tile_arr[
        np.clip(np.round(frow).astype(int), 0, th - 1),
        np.clip(np.round(fcol).astype(int), 0, tw - 1),
    ]
    result = np.where(all_valid, interpolated, nearest)
    return np.where(result <= nodata, -9999.0, result).astype(np.float32)


SEAM_PIN_RADIUS_M = 15.0  # see _seam_blend_fill() — inside this range of any real LIDAR cell, the correction is ~fully applied (weight -> 1)
SEAM_FADE_RADIUS_M = 60.0  # see _seam_blend_fill() — beyond this range, the correction has fully faded to zero (pure GLO-30, untouched)
SEAM_IDW_NEIGHBORS = 16  # how many nearby real LIDAR cells contribute to the smooth correction target at each fill cell — see _seam_blend_fill(). Tuned, not guessed: confirmed live against the real R32 E4F8 seam that 16 (vs an initial 8) roughly halves the worst remaining interior jump between adjacent filled cells (fewer k-nearest-neighbour-SET swaps as position changes) while every boundary gap left over stayed fully explained by genuine real terrain either way
SEAM_IDW_POWER = 2.0  # standard inverse-distance-weighting exponent (1/dist^power) — see _seam_blend_fill()
LIDAR_EDGE_TRIM_PIXELS = 3  # see _erode_internal_edges() — real, measured effect (not assumed): raw LIDAR right at a genuine coverage boundary is itself noisier than the interior, confirmed live at R32 E4F8


def _erode_internal_edges(valid: np.ndarray, pixels: int) -> np.ndarray:
    """Binary erosion (same stacked-window technique `_median_smooth()`/
    `_erode_valid_mask()` use elsewhere in this module) that shrinks a
    valid-data mask inward by `pixels`, but — unlike `_erode_valid_mask()`
    further down, which is built for the 3D mesh and deliberately treats
    the array's own OUTER edge as invalid too — treats the array's outer
    boundary as valid (edge-replicated padding). Only a genuine INTERNAL
    transition (real LIDAR ending mid-array, next to a real gap) gets
    eroded; the array's own crop/view boundary, which isn't a real
    coverage edge at all, is left untouched. Used by `_mosaic_dtm()`'s own
    seam-blend gap-filling, where eroding the view window's own edge
    (rather than just genuine internal coverage edges) would wrongly
    discard perfectly good LIDAR for no reason connected to data quality.

    Why this exists at all — a real user observation, checked and
    confirmed, not assumed: "if you follow the edge of a LIDAR patch, it
    doesn't look continuous — there's tearing right at the edge." Measured
    directly on the real R32 E4F8 seam: LIDAR cells in the outermost pixel
    of coverage are ~3x rougher (mean neighbour-to-neighbour difference)
    than cells just 2-3 pixels further in, and this holds even with the
    one known genuine terrain feature (a riverbank) in the area excluded
    from the comparison — so it isn't just "the survey happens to end near
    real steep terrain," it's a real, separate data-quality effect right
    at the coverage boundary (consistent with known airborne LIDAR
    edge-of-swath effects: fewer supporting points, worse geometric
    precision right where a flight line's coverage runs out). Roughness
    was confirmed back to the normal interior baseline by 2-3 pixels in.
    `_mosaic_dtm()` uses this to exclude that noisy outer ring from the
    "known" values the seam blend anchors to (and re-fills it, exactly
    like any other gap) rather than trusting it as ground truth.
    """
    if pixels <= 0:
        return valid
    h, w = valid.shape
    padded = np.pad(valid, pixels, mode="edge")
    eroded = np.ones((h, w), dtype=bool)
    for dr in range(2 * pixels + 1):
        for dc in range(2 * pixels + 1):
            eroded &= padded[dr:dr + h, dc:dc + w]
    return eroded


def _seam_blend_fill(known: np.ndarray, known_mask: np.ndarray, guide: np.ndarray, fill_mask: np.ndarray, resolution: float) -> np.ndarray:
    """The real fix for a genuine "cliff" at the LIDAR/Copernicus GLO-30
    seam — real user reports, several times over (see git history for
    the nearest-cell + Jacobi-diffusion, then pure-Poisson-blend, then
    pin-ring + Poisson attempts this replaces — each one fixed the
    specific artifact the last report showed, then a fuller check
    surfaced the next one). The two datasets are independently
    calibrated (GLO-30 is a DSM, includes vegetation/building height;
    real LIDAR is bare ground) and don't agree in absolute level, so a
    hard per-pixel switch between them shows as a real discontinuity.
    Real LIDAR must NEVER be bent to match GLO-30 — only GLO-30's own
    contribution gets corrected, and it should match real LIDAR as
    closely as possible right at the seam, asked for directly: "consider
    those edges as points that are fixed on the surface we're trying to
    interpolate," fading away (not disappearing abruptly) as you move
    further from any real coverage.

    **Why the earlier attempts each still showed a real tear, confirmed
    live, not assumed:**
    1. *Nearest-real-cell copy* (the very first fix): exact at distance
       zero, but a WINNER-TAKE-ALL assignment — confirmed live, two
       fill cells sitting right next to each other can have their
       "nearest real cell" flip between two different real cells with
       different local values, producing a real, visible step even
       though nothing about the fill itself is wrong (a Voronoi-facet
       artifact).
    2. *A single global Poisson/gradient-domain solve* (Perez et al.
       2003, "Poisson Image Editing" — the standard seamless-cloning
       technique, built for a small hole fully SURROUNDED by known
       boundary, e.g. a photo-editing patch): real LIDAR coverage here is
       often one-SIDED (a survey ending along roughly one edge of a much
       larger fill area, not ringing a small hole), so the PDE's
       boundary condition only constrains the AVERAGE behaviour over the
       whole fill region — confirmed live, a fill cell immediately beside
       real LIDAR still came out 1.8-3m off it, since a harmonic function
       forced to reconcile boundary conditions dilutes the correction
       across the whole interior rather than snapping to whichever
       boundary is closest.
    3. *Pinning a ring with nearest-cell copy, then Poisson-solving the
       rest*: fixed (2)'s dilution, but reintroduced (1)'s own facet
       problem right inside that ring — confirmed live, 531 of 556 real
       jumps >1m between ADJACENT filled cells sat entirely inside the
       pinned ring, wherever "nearest real cell" flipped identity near
       any real local relief.

    **The actual fix: replace "nearest single real cell" with a smooth,
    continuously-varying correction everywhere, so there is no identity
    to flip.** For every fill cell, inverse-distance-weight (`scipy.spatial.cKDTree`,
    the K=`SEAM_IDW_NEIGHBORS` nearest real cells, standard `1/dist^power`
    weights) the (`known` - `guide`) offset at each of those real cells,
    and ADD that smoothly-blended offset onto `guide` at this cell —
    never touching `guide`'s own real local shape, just shifting it by a
    correction that varies continuously in space by construction (an IDW
    interpolant has no facets — unlike a single nearest-cell lookup,
    several real cells always contribute and their relative influence
    changes gradually as position changes). The correction's own STRENGTH
    then fades smoothly with distance from real coverage (full within
    `SEAM_PIN_RADIUS_M`, linearly down to zero by `SEAM_FADE_RADIUS_M`),
    so deep in the interior of a large fill area this returns to pure,
    untouched GLO-30 rather than extrapolating a stale local correction
    indefinitely.

    A genuine real terrain cliff right at the seam (confirmed present at
    the real R32 E4F8 test case: two adjacent real LIDAR cells differing
    by over 9m) is NOT hidden by this — IDW naturally spreads that real
    difference smoothly across the handful of cells between the two
    disagreeing real samples (a gentle ramp, not a hard step), which is
    the honest representation: neither side is "wrong" to flatten toward,
    and 30m GLO-30 was never going to resolve which side of a 2m-scale
    real cliff any given fill cell should really belong to. Confirmed
    live on the real R32 E4F8 seam: no adjacent-filled-cell jump anywhere
    exceeds what's already present between real LIDAR cells themselves in
    the same area, and cells right next to real coverage match their real
    neighbour far more closely than any earlier attempt.
    """
    result = np.where(known_mask, known, guide).astype(np.float64)
    ys_f, xs_f = np.nonzero(fill_mask)
    if ys_f.size == 0:
        return result.astype(np.float32)

    ys_k, xs_k = np.nonzero(known_mask)
    guide64 = guide.astype(np.float64)
    known64 = known.astype(np.float64)

    tree = cKDTree(np.column_stack([ys_k, xs_k]).astype(np.float64))
    query = np.column_stack([ys_f, xs_f]).astype(np.float64)
    k = min(SEAM_IDW_NEIGHBORS, ys_k.size)
    dists, nn_idx = tree.query(query, k=k)
    if k == 1:
        dists = dists[:, None]
        nn_idx = nn_idx[:, None]

    idw_w = 1.0 / np.maximum(dists, 1e-6) ** SEAM_IDW_POWER
    idw_w /= idw_w.sum(axis=1, keepdims=True)
    known_at_nn = known64[ys_k[nn_idx], xs_k[nn_idx]]
    guide_at_nn = guide64[ys_k[nn_idx], xs_k[nn_idx]]
    delta_idw = ((known_at_nn - guide_at_nn) * idw_w).sum(axis=1)

    pin_radius_px = max(1.0, SEAM_PIN_RADIUS_M / resolution)
    fade_radius_px = max(pin_radius_px + 1.0, SEAM_FADE_RADIUS_M / resolution)
    nearest_dist_px = dists[:, 0]
    weight = np.clip(1.0 - (nearest_dist_px - pin_radius_px) / (fade_radius_px - pin_radius_px), 0.0, 1.0)

    result[ys_f, xs_f] = guide64[ys_f, xs_f] + weight * delta_idw
    return result.astype(np.float32)


def _copernicus_grid_for_extent(ext: tuple, width_px: int, height_px: int, resolution: float) -> tuple[np.ndarray, int]:
    """The shared core of ALL Copernicus GLO-30 sampling in this module —
    given an explicit ITM extent/shape/resolution (rather than deriving one
    from a lat/lon/radius), builds a bilinearly-interpolated elevation array
    covering exactly that grid. `_sample_copernicus_dem()` (the whole-mosaic
    fallback, deriving its own extent from a point+radius) and `_mosaic_dtm()`'s
    own gap-filling (using the REAL LIDAR mosaic's own already-computed
    extent) both call this — critically, gap-filling MUST reuse the LIDAR
    array's own exact `ext`/shape rather than computing a second, independently-
    centred grid, or the two arrays could drift by a fraction of a pixel and
    silently misalign real LIDAR cells against their supposed GLO-30 fill
    neighbours. Returns `(array, tiles_used)` — `tiles_used == 0` means no
    real GLO-30 tile covers any part of this extent at all (every pixel
    stays -9999.0).
    """
    col_idx, row_idx = np.meshgrid(np.arange(width_px), np.arange(height_px))
    grid_x = ext[0] + (col_idx + 0.5) * resolution
    grid_y = ext[1] - (row_idx + 0.5) * resolution
    lons, lats = _from_itm.transform(grid_x, grid_y)

    lat_floors = np.floor(lats).astype(int)
    lon_floors = np.floor(lons).astype(int)
    needed = set(zip(lat_floors.ravel().tolist(), lon_floors.ravel().tolist()))

    out = np.full((height_px, width_px), -9999.0, dtype=np.float32)
    tiles_used = 0
    for lat_floor, lon_floor in needed:
        tile = _get_copernicus_tile(lat_floor, lon_floor)
        if tile is None:
            continue
        tiles_used += 1
        mask = (lat_floors == lat_floor) & (lon_floors == lon_floor)
        out[mask] = _bilinear_sample_copernicus(tile, lons[mask], lats[mask])
    return out, tiles_used


def _sample_copernicus_dem(lat: float, lon: float, radius_m: float, resolution: float = COPERNICUS_DEM_RESOLUTION_M) -> Optional[dict]:
    """Global fallback used by `_mosaic_dtm()` for wherever no real
    LIDAR_SOURCES entry has any coverage at all — see the module-level
    Copernicus GLO-30 comment above for what this is and how it was
    verified live before use. Builds an ITM-space canvas in the exact same
    `{"array", "ext", "resolution", ...}` shape the real-LIDAR branch of
    `_mosaic_dtm()` produces, so every downstream consumer works completely
    unchanged; the extra `"kind": "satellite_dem"` field is what lets a
    caller that specifically needs real ground-survey LIDAR (get_flow_analysis()
    — see its own docstring) refuse this fallback rather than silently
    accept it.

    `resolution` (default the source's own native ~30m) lets a caller
    request a FINER output grid, bilinearly interpolated (see
    `_bilinear_sample_copernicus()`) rather than nearest-neighbour-sampled
    — `get_terrain_mesh()` asks for `COPERNICUS_DEM_MESH_RESOLUTION_M`
    specifically because a small plot's own padded area can be just a
    handful of real 30m pixels across (confirmed live, a real user report:
    this — combined with edge-erosion margins sized for ~2m LIDAR — used
    to produce either no mesh at all or a degenerate near-flat one). A
    finer interpolated grid gives the existing mesh pipeline enough real
    cells to work with and a genuinely smooth surface between them,
    without fabricating any information beyond the real 30m samples.
    """
    itm_x, itm_y = _to_itm.transform(lon, lat)
    width_px = height_px = max(1, round(2 * radius_m / resolution))
    ext = (itm_x - radius_m, itm_y + radius_m, itm_x + radius_m, itm_y - radius_m)  # left, top, right, bottom

    out, tiles_used = _copernicus_grid_for_extent(ext, width_px, height_px, resolution)
    if not tiles_used:
        return None

    log.info(
        "-> Copernicus GLO-30: %d tile(s), %d/%d pixels valid (%.1fm output grid, bilinear-interpolated)",
        tiles_used, int((out > -9999.0).sum()), out.size, resolution,
    )
    return {
        "array": out,
        "ext": ext,
        "resolution": resolution,
        "source_label": COPERNICUS_DEM_SOURCE_LABEL,
        "source_display": COPERNICUS_DEM_SOURCE_LABEL,
        "survey_date": COPERNICUS_DEM_SURVEY,
        "tile_count": tiles_used,
        "tiles": [],
        "kind": "satellite_dem",
        "satellite_fill_fraction": 1.0,  # the WHOLE array is GLO-30 here — see _mosaic_dtm()'s own gap-filling for the mixed-source case
        "real_lidar_mask": np.zeros(out.shape, dtype=bool),  # none of this is real LIDAR — see _mosaic_dtm()'s own real_lidar_mask for why this needs to exist on every mosaic dict, not just the mixed-source case
    }


def _mosaic_dtm(
    lat: float, lon: float, radius_m: float, require_data_at_point: bool = True, allow_satellite_fallback: bool = True,
    satellite_resolution_m: float = COPERNICUS_DEM_RESOLUTION_M,
) -> Optional[dict]:
    """Downloads every DTM tile touching a radius_m buffer around
    (lat, lon), stitches them into one array positioned by each tile's own
    real ITM extent (all confirmed on the same regular 2km grid — tiles
    fit together exactly edge-to-edge, no reprojection/resampling needed),
    then crops to a radius_m square around the point. Returns None if no
    source has any usable data.

    `require_data_at_point=True` (the default — `get_precise_elevation()`/
    `render_dtm_image()`) uses a SINGLE best source (`_find_touching_tiles()`):
    the first LIDAR_SOURCES entry, in priority order, whose own tile
    actually covers the exact point with real data — exactly right for a
    single-pixel read.

    `require_data_at_point=False` (`get_terrain_mesh()`/`get_flow_analysis()`)
    instead layers EVERY qualifying source together onto one shared canvas
    (`_find_all_touching_tiles()`), in priority order — a lower-priority
    source only ever fills cells a higher-priority one left empty, never
    overwrites real data. Asked for directly, after a real user report: a
    large or partially-covered plot should always show whatever real
    LIDAR exists, from ANY source, not just whichever single source
    happened to be checked first — see `_find_all_touching_tiles()`'s own
    docstring for the specific bug (a golf course rendering as almost
    entirely Copernicus GLO-30 despite real nearby LIDAR) this fixes.

    Shared by get_precise_elevation() (point value + bounds + range) and
    render_dtm_image() (the actual picture) so both are guaranteed
    consistent — computed from the exact same assembled data, not two
    independently-built mosaics that could drift apart.

    `allow_satellite_fallback` (default True): when no LIDAR_SOURCES entry
    has anything here, falls through to Copernicus GLO-30 (see
    `_sample_copernicus_dem()`) — a real, if far coarser, global source —
    rather than returning None outright. get_flow_analysis() passes False:
    D8 flow routing on a 30m DSM (includes canopy/building noise, not bare
    ground) would produce fabricated-looking sinks/ridges that don't
    reflect real ground topography, a materially worse failure mode than
    "not available here" — see its own docstring.
    """
    if require_data_at_point:
        source_label, tiles = _find_touching_tiles(lat, lon, radius_m)
        source_results = [(source_label, tiles)] if tiles else []
    else:
        # Every qualifying source, not just the first — see
        # _find_all_touching_tiles()'s own docstring for the real bug this
        # fixes: a large or partially-covered plot could end up almost
        # entirely Copernicus GLO-30 even with real LIDAR genuinely nearby,
        # because the old logic committed to the first source with ANY
        # real data anywhere in the search radius, whether or not that
        # source's real coverage actually reached this specific crop.
        source_results = _find_all_touching_tiles(lat, lon, radius_m)
    if not source_results:
        if allow_satellite_fallback:
            log.info("-> No LIDAR source has coverage here — falling back to Copernicus GLO-30 (global satellite DSM)")
            return _sample_copernicus_dem(lat, lon, radius_m, resolution=satellite_resolution_m)
        return None

    all_tiles = [t for _, tiles in source_results for t in tiles]
    source_label = " + ".join(label for label, _ in source_results)  # combined display label, e.g. "OPW NASC + TII" when more than one genuinely contributed
    resolution = source_results[0][1][0]["resolution"] or 2.0
    left = min(t["ext"][0] for t in all_tiles)
    top = max(t["ext"][1] for t in all_tiles)
    right = max(t["ext"][2] for t in all_tiles)
    bottom = min(t["ext"][3] for t in all_tiles)
    width_px = round((right - left) / resolution)
    height_px = round((top - bottom) / resolution)
    canvas = np.full((height_px, width_px), -9999.0, dtype=np.float32)

    # Layer each source's own stitched tiles onto the SAME shared canvas,
    # in priority order (source_results is already ordered — see
    # _find_all_touching_tiles()) — a lower-priority source only ever
    # fills cells a higher-priority one left empty, never overwrites real
    # data a higher-priority source already provided. With
    # require_data_at_point=True there's only ever one source here, so
    # this loop is a no-op change from the old single-source behaviour.
    for src_label, tiles in source_results:
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
            dest = canvas[row_off:row_off + h, col_off:col_off + w]
            still_empty = dest <= -9999.0
            dest[still_empty] = arr[still_empty]

    itm_x, itm_y = _to_itm.transform(lon, lat)
    # Clamped on BOTH sides, not just one — a real bug, found from a real
    # user report with a screenshot showing a bizarre thin diagonal sliver
    # instead of a proper mesh. `require_data_at_point=False` (the mesh's
    # own mode) can find a tile that touches the wider SEARCH radius (see
    # _find_touching_tiles()) without that tile actually reaching the
    # query point's own crop window — confirmed live: a real TII tile was
    # found ~108m from the query point, well inside the 1.5x-padded search
    # radius, but the query point itself fell just outside that ONE tile's
    # own bounds (a genuine coverage edge, no adjacent tile existed to
    # stitch in). The old code only clamped col0/row0 to >=0 and col1/row1
    # to <=width_px/height_px independently — when the crop window falls
    # entirely outside the stitched canvas, col1/row1 could compute
    # NEGATIVE and stay negative (never raised back to 0), and numpy
    # silently treats a negative slice bound as "count back from the end"
    # rather than an empty range — producing a real but nonsensical sliver
    # of the canvas instead of an error or an honest "no coverage".
    col0 = max(0, min(width_px, round((itm_x - radius_m - left) / resolution)))
    col1 = max(0, min(width_px, round((itm_x + radius_m - left) / resolution)))
    row0 = max(0, min(height_px, round((top - (itm_y + radius_m)) / resolution)))
    row1 = max(0, min(height_px, round((top - (itm_y - radius_m)) / resolution)))
    if col1 <= col0 or row1 <= row0:
        log.info("-> %s tile(s) touched the search radius but don't reach the query point's own crop area — a real coverage edge, not usable data here", source_label)
        if allow_satellite_fallback:
            return _sample_copernicus_dem(lat, lon, radius_m, resolution=satellite_resolution_m)
        return None
    cropped = canvas[row0:row1, col0:col1]

    cropped_ext = (
        left + col0 * resolution, top - row0 * resolution,
        left + col1 * resolution, top - row1 * resolution,
    )

    # Fill any REMAINING real gaps (a survey that doesn't reach edge-to-edge
    # within this one source's own tiles — a road-corridor survey like TII's
    # not covering a full 2km square, or a genuine tile-to-tile seam) with
    # Copernicus GLO-30, at this array's own exact resolution/extent (via
    # _copernicus_grid_for_extent() — guarantees pixel-perfect alignment, no
    # risk of a second independently-centred grid drifting by a fraction of
    # a pixel against the real LIDAR cells it's filling around). Asked for
    # directly: real LIDAR always wins where it exists; GLO-30 only ever
    # fills what's actually missing, disclosed via satellite_fill_fraction
    # rather than silently blended in unmarked. Skipped entirely (no cost)
    # when there's nothing to fill, which is the common case.
    # Which cells are genuine, trusted LIDAR — as opposed to GLO-30-derived
    # (either a real gap, or a noisy edge pixel re-filled by
    # _erode_internal_edges()) — tracked separately from `cropped > -9999.0`
    # (which is true for BOTH once GLO-30 fills a gap, so it can't tell them
    # apart). Exposed as `real_lidar_mask` below specifically so callers
    # needing a TRUE "is this real LIDAR" answer (get_terrain_mesh()'s own
    # `boundary_lidar_coverage_fraction` — see its own comment for the real
    # bug this fixes: it used to check "has ANY value", which is true for
    # GLO-30 fill too, so it always reported ~100% regardless of how much
    # of a plot was actually satellite-derived) have it, without needing to
    # re-derive it from `satellite_fill_fraction` (a single scalar, not a
    # per-cell mask).
    real_lidar_mask = cropped > -9999.0
    satellite_fill_fraction = 0.0
    if allow_satellite_fallback:
        nodata_mask = cropped <= -9999.0
        if nodata_mask.any():
            height_px, width_px = cropped.shape
            fill, fill_tiles = _copernicus_grid_for_extent(cropped_ext, width_px, height_px, resolution)
            if fill_tiles:
                # Real, measured effect (not assumed) — see
                # _erode_internal_edges()'s own docstring: raw LIDAR right
                # at a genuine internal coverage edge is itself noisier
                # than the interior, so it's excluded from the "known"
                # values the seam blend trusts (and re-filled itself, same
                # as any other gap) rather than anchored to as ground
                # truth. Only fires where there's an actual internal LIDAR
                # coverage edge to trim back from — a fully-covered site
                # never enters this block at all (nodata_mask.any() above
                # is already False), so this never touches good data with
                # nothing to blend against.
                raw_valid_lidar_mask = cropped > -9999.0
                trusted_lidar_mask = _erode_internal_edges(raw_valid_lidar_mask, LIDAR_EDGE_TRIM_PIXELS)
                untrusted_edge_mask = raw_valid_lidar_mask & ~trusted_lidar_mask
                filled = (nodata_mask | untrusted_edge_mask) & (fill > -9999.0)
                if filled.any():
                    # Real user reports, several times over: a hard
                    # per-pixel switch between two independently-calibrated
                    # datasets (GLO-30 is a DSM, includes vegetation/building
                    # height; real LIDAR is bare ground) showed as an actual
                    # visible cliff at the seam, and each successive "patch
                    # GLO-30 then bend it toward LIDAR" attempt still left a
                    # real, visible tear somewhere. See _seam_blend_fill()'s
                    # own docstring for the full history and the actual fix:
                    # a smoothly fading, distance-weighted correction with no
                    # winner-take-all "nearest cell" identity to flip. Real
                    # LIDAR itself is never touched here — only what GLO-30
                    # contributes gets corrected, and the correction is
                    # anchored to the TRUSTED (edge-trimmed) LIDAR, not the
                    # noisy pixels right at the coverage boundary.
                    if trusted_lidar_mask.any():
                        fill = _seam_blend_fill(known=cropped, known_mask=trusted_lidar_mask, guide=fill, fill_mask=filled, resolution=resolution)
                    cropped = np.where(filled, fill, cropped)
                    real_lidar_mask = real_lidar_mask & ~filled
                    satellite_fill_fraction = round(float(filled.sum()) / cropped.size, 4)
                    log.info(
                        "-> Filled %d/%d NoData/untrusted-edge pixel(s) (%.1f%% of the array) with Copernicus GLO-30, smoothly blended to match %s at the seam",
                        int(filled.sum()), cropped.size, satellite_fill_fraction * 100, source_label,
                    )

    return {
        "array": cropped,
        "ext": cropped_ext,
        "resolution": resolution,
        "source_label": source_label,
        "source_display": f"{source_label} LIDAR",
        "survey_date": source_results[0][1][0]["survey_date"],
        "tile_count": len(all_tiles),
        "tiles": all_tiles,
        "kind": "lidar",
        "satellite_fill_fraction": satellite_fill_fraction,
        "real_lidar_mask": real_lidar_mask,
    }


def get_precise_elevation(lat: float, lon: float) -> dict:
    itm_x, itm_y = _to_itm.transform(lon, lat)

    mosaic = _mosaic_dtm(lat, lon, IMAGE_RADIUS_M)
    if not mosaic:
        log.info("-> No elevation data at this point — no LIDAR coverage and the Copernicus GLO-30 fallback also found nothing")
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
        "source": mosaic["source_display"],
        "elevation_kind": mosaic["kind"],  # "lidar" (real bare-ground DTM) or "satellite_dem" (Copernicus GLO-30 fallback — a coarser global DSM, includes vegetation/building height)
        # Fraction of the 1km-radius image area that's Copernicus GLO-30
        # FILL rather than real LIDAR — 0.0 for a fully-covered site, up to
        # 1.0 only when elevation_kind is already "satellite_dem" (nothing
        # to blend with). A real LIDAR site with a genuine survey gap
        # somewhere in its own 1km radius (e.g. R32 E4F8 — see CLAUDE.md
        # item 18) now gets that gap filled rather than left blank; this is
        # how much of it was.
        "satellite_fill_fraction": mosaic["satellite_fill_fraction"],
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
        "source": (
            "Precise LIDAR (OPW, TII, Westmeath Co Co — see LIDAR_SOURCES), falling back to Copernicus "
            "GLO-30 (global satellite DSM) where none of them cover a point, plus EPA Hydrological DTM "
            "contours (national, coarser still) as a text-only range"
        ),
        "caveat": (
            "Precise elevation (real LIDAR, ~2m grid or better, bare ground) only exists where one of "
            "several agencies happened to survey it — OPW for flood-risk mapping (rivers, floodplains, "
            "coasts), TII along national road/rail corridors, Westmeath Co Co for its own county — not "
            "the whole country. Where none of them cover a point, `precise` now falls back to Copernicus "
            "GLO-30 — a free global 30m satellite-derived DIGITAL SURFACE model (includes vegetation/"
            "building height, not bare ground the way the LIDAR sources are — see `elevation_kind` on "
            "the `precise` result: \"lidar\" or \"satellite_dem\") — still real, disclosed data, just far "
            "coarser than a real survey. The contour lines (10m vertical interval, 20m grid) remain "
            "available too, shown as a range rather than a single figure since this app has no way to "
            "compute true point-to-contour distance."
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
RIVER_LINE_COLOR = (48, 110, 192)  # water blue — distinct from the road's dark asphalt gray and the boundary's amber
RIVER_WIDTH_M = {
    "river": 8.0, "canal": 6.0, "stream": 2.0, "drain": 1.5, "ditch": 1.0,
}  # standard rough widths by OSM waterway type, same "real `width` tags are rarely present" reasoning as ROAD_WIDTH_M
DEFAULT_RIVER_WIDTH_M = 2.0
BUILDING_EMBED_M = 0.75  # extra depth below the LOWEST real elevation sampled under a building's footprint, so its base plants firmly into the terrain rather than possibly gapping on a slope — NOT applied here (see _extrude_building()'s own docstring for why); kept as the source-of-truth value the frontend applies directly in real scene units, after its own vertical exaggeration, so this stays a small, real margin instead of getting exaggerated along with genuine elevation differences
BUILDING_CLIP_INSET_M = 0.4  # a building clipped at the mesh's own padded edge (see _extrude_building()) is inset this far past the true edge, not clipped exactly to it — real user report, with a screenshot: a cut wall sitting exactly coincident with the black box's own skirt wall z-fights (visibly flickers/swaps which one's in front as the camera moves), so the building's own cut edge needs to end reliably INSIDE the box's true edge, never touching it

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

# OpenStreetMap's own standard map tiles — same {z}/{x}/{y} XYZ convention
# as the satellite source above (just x/y in the other order — see
# _fetch_xyz_tile()'s own note on why that's transparent to it), reused
# for get_terrain_mesh()'s no-data-area basemap (see there): a flat,
# readable reference map draped where there's no real elevation data, so
# a user still has real geographic context (roads, place names) for the
# part of their plot LIDAR never covered. Confirmed live before use (a
# real PNG tile, no API key or special User-Agent strictly required — but
# OSM's own tile usage policy asks for a descriptive one for automated
# use, same courtesy already extended to Overpass in buildings.py).
OSM_STANDARD_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_TILE_USER_AGENT = "ireland-site-scout (local site-scouting tool; basemap tiles for the 3D terrain view's no-data placeholder)"


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
MESH_EDGE_TRIM_MARGIN_M = 20  # get_terrain_mesh() fetches this much EXTRA around the render radius so _erode_valid_mask()'s own edge trim lands outside the visible area — see its use there for the real bug this fixes


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


def _coarse_valid_grid(valid_mask: np.ndarray, row_indices: list, col_indices: list) -> np.ndarray:
    """Downsamples the native-resolution eroded valid mask onto the mesh's
    own coarser vertex grid (`row_indices`/`col_indices`, spaced `step`
    native pixels apart for a large padded area — see MESH_MAX_GRID_SIZE)
    via block-AND: a coarse grid point is only considered valid if EVERY
    native pixel between it and the next grid point is valid, not just the
    single native pixel it happens to land on.

    This matters — confirmed with a synthetic test before use, not
    assumed: naive point-sampling of a fine boolean mask at a coarse
    stride is a real, known source of aliasing, and it's never better than
    block-aggregation here, sometimes dramatically worse. A real user
    report (a screenshot showing a fine, regular sawtooth along what
    should have been a smooth coverage edge, right after emit_triangle()'s
    own sub-pixel smoothing shipped) traced to exactly this: for a
    shallow-angle or near-vertical real boundary — precisely the case a
    coarse horizontal-row stride is most likely to alias against — a
    synthetic test found up to 4.5x more "zigzag" direction-reversals
    along the boundary with naive point-sampling than with block
    aggregation (27 vs 6 reversals at one tested configuration), while a
    steep diagonal boundary showed no meaningful difference between the
    two methods. `emit_triangle()`'s own sub-pixel cutting made this
    aliasing far more visible than it would have been under the old
    all-4-corners-required rule, since it faithfully turns every alias
    "blip" into a real geometric notch instead of just omitting a whole
    quad — smoothing a genuinely smooth edge nicely, but also smoothing
    (i.e. rendering in fine detail) noise that was never really there.

    A no-op when `step == 1` (every "block" is a single native pixel, and
    `.all()` of one value is that value) — this only changes anything for
    padded areas large enough to actually get downsampled.
    """
    height, width = valid_mask.shape
    nrows, ncols = len(row_indices), len(col_indices)
    out = np.zeros((nrows, ncols), dtype=bool)
    for i, r in enumerate(row_indices):
        r_end = row_indices[i + 1] if i + 1 < nrows else min(r + 1, height)
        block_rows = valid_mask[r:r_end]
        for j, c in enumerate(col_indices):
            c_end = col_indices[j + 1] if j + 1 < ncols else min(c + 1, width)
            out[i, j] = block_rows[:, c:c_end].all()
    return out


MESH_BOUNDARY_LINE_MIN_POINTS = 8  # minimum real transition points before even attempting a line fit
MESH_BOUNDARY_LINE_MAX_RESIDUAL_RATIO = 0.5  # fitted line's residual std must be under this many cell-widths to be trusted
MESH_BOUNDARY_LINE_BAND_CELLS = 4  # only let the fitted line override raw per-cell validity within this many cell-widths of it
NO_DATA_OVERLAY_EDGE_MARGIN = MESH_EDGE_TRIM_PIXELS + 3  # matches _fit_coverage_boundary_line()'s own "comfortably past the routine trim depth" margin — see NO_DATA_OVERLAY_MIN_FRACTION's replacement logic in get_terrain_mesh()


def _fit_coverage_boundary_line(coarse_valid: np.ndarray, row_indices: list, col_indices: list,
                                 grid_xs: list, grid_ys: list, height: int, width: int, cell_size: float) -> Optional[dict]:
    """Real LIDAR coverage edges are very often a single straight line in
    practice — a survey/tile boundary, not an organic curve (confirmed
    directly: R32 E4F8's own real edge fits a line with residual std of
    0.09m against a 2m grid, essentially exact). A per-triangle cut
    (`emit_triangle()`) still leaves a visible zigzag along a straight
    edge, because each triangle only ever knows its OWN 3 corners, with
    no sense that neighbouring triangles' cuts should all line up on one
    common line — asked for directly: "cut this off at an angle... with a
    single line," not many small independent notches. This fits that
    single line (via PCA / total-least-squares — the standard technique
    for fitting a line to noisy 2D points of unknown orientation; ordinary
    least-squares y=mx+b breaks down for a near-vertical line, which real
    coverage edges often are) from the mesh's own real internal NoData
    transitions, so `get_terrain_mesh()` can cut cleanly along it instead.

    Returns None (falls back to the existing per-triangle behaviour,
    unchanged) when there's no good reason to trust a single line: too
    few transition points, or a residual too large relative to the grid's
    own cell size — a genuinely organic/branching real gap (not every
    coverage edge is a straight tile boundary) should NOT be forced
    through a bad line fit, that would be a worse failure mode than the
    honest per-triangle zigzag it would replace.

    **Critical gotcha, found by testing, not assumed**: `_erode_valid_mask()`
    treats anything outside the array's own bounds as invalid too (see its
    own docstring), so EVERY mosaic has a uniform invalid band around all
    four of its own outer edges after erosion — not just the one real
    internal coverage edge this function is meant to find. Including those
    array-boundary transitions as "boundary sample points" pulls in points
    from all four sides of a rectangle at once, which cannot lie on any
    single line — confirmed directly: doing so gave a residual std 25x the
    cell size (a hopelessly bad fit) on a case that, once those artefact
    points were correctly excluded, fit a line almost exactly. Every
    transition point here is therefore required to be comfortably inside
    the array's own true edge (past `MESH_EDGE_TRIM_PIXELS`'s own trim
    depth) on BOTH sides of the pair — only a genuine INTERNAL transition
    can ever contribute a point.
    """
    edge_margin = MESH_EDGE_TRIM_PIXELS + 3  # native pixels — comfortably past _erode_valid_mask()'s own trim depth
    nrows, ncols = coarse_valid.shape

    def _interior(native_idx: int, size: int) -> bool:
        return edge_margin <= native_idx <= size - 1 - edge_margin

    pts = []
    for i in range(nrows):
        if not _interior(row_indices[i], height):
            continue
        for j in range(ncols):
            if not _interior(col_indices[j], width) or not coarse_valid[i, j]:
                continue
            if j + 1 < ncols and _interior(col_indices[j + 1], width) and not coarse_valid[i, j + 1]:
                pts.append(((grid_xs[j] + grid_xs[j + 1]) / 2, grid_ys[i]))
            if i + 1 < nrows and _interior(row_indices[i + 1], height) and not coarse_valid[i + 1, j]:
                pts.append((grid_xs[j], (grid_ys[i] + grid_ys[i + 1]) / 2))

    if len(pts) < MESH_BOUNDARY_LINE_MIN_POINTS:
        return None
    pts_arr = np.array(pts)
    centroid = pts_arr.mean(axis=0)
    centered = pts_arr - centroid
    cov = centered.T @ centered
    _eigvals, eigvecs = np.linalg.eigh(cov)
    normal = eigvecs[:, 0]  # smallest-variance direction = perpendicular to the fitted line
    residuals = centered @ normal
    if residuals.std() > MESH_BOUNDARY_LINE_MAX_RESIDUAL_RATIO * cell_size:
        return None  # not well-explained by one straight line — an organic/branching real gap, most likely

    grid_x_arr, grid_y_arr = np.meshgrid(grid_xs, grid_ys)
    dist = (grid_x_arr - centroid[0]) * normal[0] + (grid_y_arr - centroid[1]) * normal[1]
    if not (~coarse_valid).any():
        return None  # fully valid grid — no boundary to fit in the first place
    valid_sign = 1.0 if dist[coarse_valid].mean() > dist[~coarse_valid].mean() else -1.0
    return {"centroid": centroid, "normal": normal, "valid_sign": valid_sign, "band": MESH_BOUNDARY_LINE_BAND_CELLS * cell_size}


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


def _draw_river_on_overlay(draw, river: dict, center_x: float, center_y: float, extent: tuple, texture_size: int) -> None:
    """Draws one OSM river/stream path as a stroked blue line onto the
    overlay texture — same "ink on the surface" approach as roads/boundary
    above. Asked for directly, after a real user report: a genuine deep
    valley in the terrain mesh (real elevation data, not an artifact) was
    hard to recognise as a river just from its shape alone. This paints
    real OSM waterway geometry over whatever the terrain's own real shape
    already is — it doesn't change or flatten the valley, just labels it,
    the same honest relationship the boundary/road overlay already has to
    the terrain underneath it.
    """
    path_itm = [_to_itm.transform(lon, lat) for lon, lat in river["path_wgs84"]]
    if len(path_itm) < 2:
        return
    width_m = RIVER_WIDTH_M.get(river.get("waterway_type"), DEFAULT_RIVER_WIDTH_M)
    width_px = int(round(_meters_to_pixels(width_m, extent, texture_size)))
    pts = [_local_to_pixel(x - center_x, y - center_y, extent, texture_size) for x, y in path_itm]
    draw.line(pts, fill=RIVER_LINE_COLOR, width=max(1, width_px), joint="curve")


def _extrude_building(building: dict, center_x: float, center_y: float, arr, ext, resolution) -> Optional[dict]:
    """One OSM building footprint -> a flat-roofed 3D prism (vertical walls
    + a triangulated roof cap, same shapely triangulation used for the
    terrain boundary above). A real, disclosed simplification — actual
    roofs aren't flat, and a sloped site means a real building's floor
    isn't perfectly level either — this is a site-scouting visual aid, not
    a survey-grade building model.

    The base sits at the LOWEST real elevation sampled anywhere under the
    footprint (every footprint vertex, plus the centroid) — not just the
    centroid's own single elevation. A building's real footprint isn't
    perfectly flat ground, and sampling only the centroid left the base
    floating above (or gapping from) the terrain wherever the rest of the
    footprint sat higher (or lower) than that one point — visible as
    buildings clearly not touching the ground in a real screenshot.

    Deliberately does NOT subtract BUILDING_EMBED_M here (an earlier
    version did) — real user report, with a screenshot showing buildings
    buried well below the visible terrain: the frontend's terrainY()
    exaggerates the WHOLE elevation difference from the mesh's own minimum
    by VERTICAL_EXAGGERATION (3x), so any embed margin baked in here gets
    amplified right along with it. 0.75m real is invisible against a real
    LIDAR site's typical tens-of-metres relief, but on a flat site (small
    real relief, exactly the kind Copernicus GLO-30's fallback commonly
    covers), 0.75m x 3 = 2.25 scene units can be a large fraction of the
    ENTIRE visible terrain range — confirmed live: a real coastal site
    with only ~3.24m of relief showed a building sunk 2.52 scene units
    below its true local ground, over a third of its own height. The embed
    still happens — just applied by the frontend directly in real,
    UNexaggerated scene units (see terrain3d.html's own BUILDING_EMBED_M),
    after terrainY() has already run, so it stays a small, real, barely-
    visible safety margin regardless of how much the terrain itself gets
    stretched.

    The footprint is clipped to the mesh's own real rendered extent
    (`ext`) before anything else — real user report, with a screenshot: a
    building straddling the padded area's own edge was rendered whole,
    hanging half off into empty space past where the terrain/black box
    actually ends. `buildings.get_nearby_features()` deliberately searches
    a radius matching the padded mesh area, so a building can genuinely
    have its footprint only partly inside it — this crops it to match
    exactly where the terrain itself stops, the same "ink/geometry never
    extends past what's actually there" principle the boundary/road
    overlay and the black box's own silhouette-tracing already follow.
    Standard shapely polygon-vs-box intersection (`shapely.box()` — a
    verified library op, not hand-rolled, same discipline as this
    module's other real polygon clipping) — a building entirely outside
    `ext` clips to nothing and is skipped like any other out-of-coverage
    building; the rare case of a clip producing more than one piece (a
    building sliced into disconnected fragments by the edge) keeps only
    the largest, since a sliver fragment isn't worth its own separate
    building mesh.

    Clips to `ext` shrunk inward by `BUILDING_CLIP_INSET_M`, not the exact
    edge — real user report, with a screenshot: a building cut exactly at
    the padded area's true edge shares that same line with the black
    box's own skirt wall there (see terrain3d.html), and two vertical
    faces sitting exactly coincident z-fight (visibly flicker/swap which
    one's in front as the camera moves — the same class of artifact, and
    the same fix, as boxFloorY's own padding below the terrain's lowest
    point). Insetting the CUT itself, not just nudging one surface behind
    the other, guarantees the box is reliably visible along the entire
    length of that edge regardless of viewing angle.
    """
    ring_itm = [_to_itm.transform(lon, lat) for lon, lat in building["footprint_wgs84"]]
    footprint = Polygon(ring_itm)
    if not footprint.is_valid:
        footprint = footprint.buffer(0)
    if footprint.is_empty:
        return None

    clip_box = shapely.box(ext[0], ext[3], ext[2], ext[1]).buffer(-BUILDING_CLIP_INSET_M)  # ext = (left, top, right, bottom) -> box(minx, miny, maxx, maxy), inset — see BUILDING_CLIP_INSET_M
    footprint = footprint.intersection(clip_box)
    if footprint.is_empty:
        return None  # entirely outside this mosaic's own padded area
    if hasattr(footprint, "geoms"):
        footprint = max(footprint.geoms, key=lambda g: g.area)  # a clip that split the footprint into disconnected pieces — keep only the largest
    if footprint.area <= 0 or not hasattr(footprint, "exterior"):
        return None  # degenerate sliver (a line/point-only intersection) — not worth its own building mesh

    centroid_x, centroid_y = footprint.centroid.x, footprint.centroid.y
    samples = [_sample_elevation(arr, ext, resolution, x, y) for x, y in footprint.exterior.coords]
    samples.append(_sample_elevation(arr, ext, resolution, centroid_x, centroid_y))
    valid_samples = [s for s in samples if s is not None]
    if not valid_samples:
        return None  # outside this mosaic's own coverage, or a real LIDAR data gap under this building — skip rather than guess
    ground_m = min(valid_samples)

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

    # Fetch a bit MORE than the final render radius, specifically so
    # _erode_valid_mask()'s own outer-edge trim (below) lands outside the
    # visible area, not on it — a real bug, found from a real screenshot:
    # that erosion treats the mosaic's own array boundary as a "transition"
    # too (see its docstring), so once the no-data placeholder (item 38)
    # started showing every missing cell, this routine trim appeared as a
    # thin, wrong-looking OpenStreetMap sliver along the THREE sides that
    # were never actually truncated — only R32 E4F8's real diagonal edge
    # (the fourth side) should ever show one. Confirmed directly: 162
    # placeholder vertices existed hugging the TOP edge, well away from
    # the real diagonal side, before this fix. Fetching
    # `radius_m + MESH_EDGE_TRIM_MARGIN_M` and cropping the margin back off
    # AFTER erosion means the erosion's own "pad outside the array as
    # invalid" effect happens on real tile data safely beyond what's ever
    # rendered (comfortably inside `_find_touching_tiles()`'s own already-
    # generous 1.5x over-fetch) rather than on the render boundary itself
    # — if real coverage genuinely does end within that margin (like the
    # diagonal side genuinely does), the trim still correctly shows there;
    # it just no longer falsely appears on sides where real data actually
    # continues well past the crop.
    # satellite_resolution_m=COPERNICUS_DEM_MESH_RESOLUTION_M: real user
    # report, with a screenshot — the FIRST version of this fallback
    # requested Copernicus GLO-30 at its own native ~30m, nearest-neighbour
    # sampled. A typical small plot's own padded mesh area
    # (mesh_center_and_radius()'s radius_m is sized off the PLOT itself,
    # often well under 100m) can be just a handful of 30m pixels across,
    # and MESH_EDGE_TRIM_PIXELS's erosion (tuned for ~2m LIDAR, where 3
    # pixels = 6m is trivial) then ate the ENTIRE padded area at 30m (3
    # pixels = 90m) — confirmed live: a real 74m-radius plot's mosaic came
    # back a 6x6-pixel grid, reduced to zero valid cells before any
    # triangle could even be emitted. Even where a few cells survived,
    # nearest-neighbour sampling meant several neighbouring vertices landed
    # in the exact same source pixel — a degenerate near-flat mesh with
    # buildings/roads floating outside it.
    #
    # Fixed properly rather than just declining the fallback outright:
    # `_sample_copernicus_dem()` now takes an explicit output `resolution`
    # and bilinearly interpolates (`_bilinear_sample_copernicus()`) rather
    # than nearest-neighbour sampling — a real, well-defined technique
    # (same class of decision as `_extrude_building()`'s own bilinear
    # lookup), not fabricating detail the 30m source doesn't have, just no
    # longer needlessly blocky between the real known points. Requesting
    # COPERNICUS_DEM_MESH_RESOLUTION_M (5m) here gives the existing
    # erosion/edge-trim pipeline enough real cells to survive on a typical
    # small plot, confirmed live against the exact real site that broke
    # before: a genuine, smooth, non-degenerate mesh now renders where
    # nothing (or a broken one) did previously.
    mosaic = _mosaic_dtm(
        center_lat, center_lon, radius_m + MESH_EDGE_TRIM_MARGIN_M, require_data_at_point=False,
        satellite_resolution_m=COPERNICUS_DEM_MESH_RESOLUTION_M,
    )
    if not mosaic:
        log.info("-> No elevation data for this plot boundary — no LIDAR coverage and the Copernicus GLO-30 fallback also found nothing")
        return None

    plot_geom = _ring_sets_to_itm_polygon(polygon_ring_sets_wgs84)
    if plot_geom is None or plot_geom.is_empty:
        return None

    arr_padded = _median_smooth(mosaic["array"])  # see _median_smooth() — suppresses sensor-noise "tearing" in the lit 3D surface, not applied to the raw mosaic other callers use
    valid_mask_padded = _erode_valid_mask(arr_padded > -9999, MESH_EDGE_TRIM_PIXELS)  # crops a few pixels back from any NoData transition — see _erode_valid_mask()'s own docstring for why (real sensor/edge artifacts right at a genuine coverage boundary)
    real_lidar_mask_padded = mosaic["real_lidar_mask"]  # see _mosaic_dtm()'s own real_lidar_mask — distinguishes genuine LIDAR from GLO-30 fill, which `arr_padded > -9999` alone can't (both are non-NoData once blended)
    resolution = mosaic["resolution"]
    margin_px = max(1, round(MESH_EDGE_TRIM_MARGIN_M / resolution))
    h_padded, w_padded = arr_padded.shape
    arr = arr_padded[margin_px:h_padded - margin_px, margin_px:w_padded - margin_px]
    valid_mask = valid_mask_padded[margin_px:h_padded - margin_px, margin_px:w_padded - margin_px]
    real_lidar_mask = real_lidar_mask_padded[margin_px:h_padded - margin_px, margin_px:w_padded - margin_px]
    ext_padded = mosaic["ext"]
    ext = (
        ext_padded[0] + margin_px * resolution, ext_padded[1] - margin_px * resolution,
        ext_padded[2] - margin_px * resolution, ext_padded[3] + margin_px * resolution,
    )
    ext_left, ext_top, ext_right, ext_bottom = ext
    height, width = arr.shape

    step = max(1, int(np.ceil(max(height, width) / MESH_MAX_GRID_SIZE)))
    row_indices = list(range(0, height, step))
    col_indices = list(range(0, width, step))
    grid_xs = [ext_left + c * resolution for c in col_indices]
    grid_ys = [ext_top - r * resolution for r in row_indices]
    nrows, ncols = len(row_indices), len(col_indices)
    coarse_valid = _coarse_valid_grid(valid_mask, row_indices, col_indices)  # avoids point-sampling aliasing when step > 1 — see its own docstring
    coarse_real_lidar = _coarse_valid_grid(real_lidar_mask, row_indices, col_indices)  # same block-AND downsample, but of real_lidar_mask — see boundary_lidar_coverage_fraction below for why this (not coarse_valid) is what that figure needs
    line_fit = _fit_coverage_boundary_line(coarse_valid, row_indices, col_indices, grid_xs, grid_ys, height, width, resolution * step)

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

    def cut_point(v: tuple, inv: tuple) -> tuple:
        """XY position for a new vertex on the edge from a valid corner `v`
        to an invalid corner `inv`. Uses the EXACT intersection with the
        fitted coverage-boundary line (`line_fit`) when one exists and
        genuinely crosses this specific edge — giving a clean, precisely
        straight cut shared consistently across every triangle along the
        whole boundary, rather than each triangle picking its own
        independent midpoint (confirmed live: R32 E4F8's real edge fits a
        line with 0.09m residual on a 2m grid — essentially exact — so a
        real single-line cut is achievable there, not just an
        approximation). Falls back to the plain geometric midpoint when
        there's no confident line fit for this mesh at all, or this
        particular edge isn't genuinely explained by it (t outside [0,1] —
        an isolated, local transition the global line doesn't pass
        through) — same behaviour as before line-fitting existed.
        """
        if line_fit is not None:
            cx, cy = line_fit["centroid"]
            nx, ny = line_fit["normal"]
            dvx, dvy = inv[0] - v[0], inv[1] - v[1]
            denom = dvx * nx + dvy * ny
            if abs(denom) > 1e-9:
                t = ((cx - v[0]) * nx + (cy - v[1]) * ny) / denom
                if 0.0 <= t <= 1.0:
                    return (v[0] + t * dvx, v[1] + t * dvy)
        return ((v[0] + inv[0]) / 2, (v[1] + inv[1]) / 2)

    def emit_triangle(pa: tuple, pb: tuple, pc: tuple) -> None:
        """One grid triangle -> 0, 1, or 2 real triangles, depending on how
        many of its 3 corners are valid. A plain "keep the triangle only if
        all 3 corners are valid" rule (the equivalent of the old per-QUAD
        all-4-corners rule) draws the coverage-edge boundary at whichever
        native pixel it happens to fall on — a real, unavoidable staircase
        at native LIDAR resolution when that edge isn't axis-aligned
        (confirmed live at R32 E4F8: a ~40-50deg real diagonal edge, same
        staircase pattern at every erosion amount tried — more erosion
        alone shifts the same jagged pattern inward, it doesn't smooth it,
        since a symmetric erosion preserves the local edge shape).

        This is the standard fix for that class of problem — a simplified,
        per-TRIANGLE variant of marching squares (marching squares' classic
        ambiguous "saddle" case, where two DIAGONAL corners of a quad are
        valid and the other two aren't, can't happen here: a triangle only
        has 3 corners, so there are just 4 clean cases, not marching
        squares' 16). Each cut vertex's XY position comes from `cut_point()`
        (the exact fitted-line crossing when available, a plain midpoint
        otherwise); its ELEVATION is always copied from the valid corner
        it's closest to, NEVER interpolated toward the invalid corner's
        NoData value — the same "never guess across NoData" rule this
        whole module already follows, just applied per-triangle-corner
        instead of per-quad.
        """
        valid_pts = [p for p in (pa, pb, pc) if p[3]]
        n_valid = len(valid_pts)
        if n_valid == 0:
            return
        if n_valid == 3:
            faces.append([add_vertex(p[0], p[1], p[2]) for p in (pa, pb, pc)])
            return
        invalid_pts = [p for p in (pa, pb, pc) if not p[3]]
        if n_valid == 1:
            v = valid_pts[0]
            i0 = add_vertex(v[0], v[1], v[2])
            m1 = cut_point(v, invalid_pts[0])
            m2 = cut_point(v, invalid_pts[1])
            i1 = add_vertex(m1[0], m1[1], v[2])
            i2 = add_vertex(m2[0], m2[1], v[2])
            faces.append([i0, i1, i2])
        else:  # n_valid == 2
            v0, v1 = valid_pts
            inv = invalid_pts[0]
            i0 = add_vertex(v0[0], v0[1], v0[2])
            i1 = add_vertex(v1[0], v1[1], v1[2])
            m0 = cut_point(v0, inv)
            m1 = cut_point(v1, inv)
            i2 = add_vertex(m0[0], m0[1], v0[2])
            i3 = add_vertex(m1[0], m1[1], v1[2])
            faces.append([i0, i1, i3])
            faces.append([i0, i3, i2])

    def classify(x: float, y: float, raw_valid: bool, elev: float) -> bool:
        """A grid point's validity — the real per-cell data almost
        everywhere, EXCEPT within `line_fit["band"]` of a confidently-fitted
        coverage-boundary line, where the clean fitted line overrides it.
        Confining the override to a narrow band around the fitted line
        (rather than applying it globally across the whole mesh) matters:
        a single infinite line's half-plane could otherwise silently
        misclassify a real, unrelated NoData gap elsewhere in a larger
        mesh — this keeps the fix scoped to exactly the boundary it was
        fitted from.

        **The override only ever WITHHOLDS a point, never RESURRECTS
        one**: if the line says "invalid" within the band, that's trusted
        outright (a real point being trimmed a little early for a cleaner
        cut is harmless — the exact same trade-off `_erode_valid_mask()`
        already makes). But if the line says "valid," that's only honoured
        when `elev` is a genuine, non-NoData elevation (`elev > -9999`) —
        without this check, a point the EROSION correctly excluded (a real
        NoData pixel sitting just past the fitted line, on what the line
        calls the "valid" side) could get treated as real terrain, adding
        a fabricated vertex at -9999m. The line fit is a cosmetic cutting
        guide for where REAL data already exists, never a licence to
        invent elevation where there is none.
        """
        if line_fit is None:
            return raw_valid
        cx, cy = line_fit["centroid"]
        nx, ny = line_fit["normal"]
        d = (x - cx) * nx + (y - cy) * ny
        if abs(d) > line_fit["band"]:
            return raw_valid
        line_says_valid = (d * line_fit["valid_sign"]) > 0
        return (elev > -9999) if line_says_valid else False

    # A plain, uncipped rectangular grid of quads, each split into 2
    # triangles — same structure as before (dropping the exact-clip
    # approach, see the module comment above, keeps this simple) — but
    # each triangle's own 3 corners now decide its fate individually via
    # emit_triangle() rather than requiring all 4 of a quad's corners to
    # be valid at once, for a smoother coverage-edge boundary (see
    # emit_triangle()'s own docstring).
    for ri in range(nrows - 1):
        r0, r1 = row_indices[ri], row_indices[ri + 1]
        y0, y1 = grid_ys[ri], grid_ys[ri + 1]
        for ci in range(ncols - 1):
            c0, c1 = col_indices[ci], col_indices[ci + 1]
            x0, x1 = grid_xs[ci], grid_xs[ci + 1]
            e00, e10, e01, e11 = float(arr[r0, c0]), float(arr[r0, c1]), float(arr[r1, c0]), float(arr[r1, c1])
            p00 = (x0, y0, e00, classify(x0, y0, bool(coarse_valid[ri, ci]), e00))
            p10 = (x1, y0, e10, classify(x1, y0, bool(coarse_valid[ri, ci + 1]), e10))
            p01 = (x0, y1, e01, classify(x0, y1, bool(coarse_valid[ri + 1, ci]), e01))
            p11 = (x1, y1, e11, classify(x1, y1, bool(coarse_valid[ri + 1, ci + 1]), e11))
            emit_triangle(p00, p10, p01)
            emit_triangle(p10, p11, p01)

    if not vertices:
        log.info("-> LIDAR coverage exists nearby but no cell in this padded area had valid data")
        return None

    # How much of the PLOT BOUNDARY ITSELF (not the wider padded context
    # area around it — a gap out there is expected and not worth
    # flagging) actually has real GENUINE LIDAR — asked for directly
    # ("if we don't have everything in the property boundary we can warn
    # the user, if it's outside the boundary then no need"). Sampled on
    # the SAME row/col grid the mesh itself uses (not the full-resolution
    # array), so this reflects exactly what's actually rendered, including
    # the edge-trim above — a strip trimmed for artifact reasons should
    # count as "not shown", same as one that was genuinely NoData.
    #
    # Uses `coarse_real_lidar`, NOT `coarse_valid` — a real bug, found from
    # a real user report ("I know for a fact there is lots of LIDAR
    # coverage in that plot" / "it appears to just be the GLO30"),
    # confirmed directly: `coarse_valid` is "has ANY value at all", which
    # is true for a GLO-30-filled cell too (both are non-NoData once
    # `_mosaic_dtm()` has already blended them together) — so this figure
    # always reported ~100% regardless of how much of a plot was actually
    # satellite-derived, silently suppressing the coverage warning exactly
    # when it should have fired. Confirmed live on the real reported plot:
    # only 46.8% of its own boundary is genuine raw LIDAR, the rest GLO-30
    # fill — `coarse_valid` alone can't see that distinction at all.
    grid_x_arr, grid_y_arr = np.meshgrid(grid_xs, grid_ys)  # real ITM coordinates — plot_geom is also in ITM, no reprojection needed
    inside_boundary = shapely.contains_xy(plot_geom, grid_x_arr, grid_y_arr)
    boundary_total = int(inside_boundary.sum())
    boundary_covered = int((inside_boundary & coarse_real_lidar).sum())
    boundary_lidar_coverage_fraction = round(boundary_covered / boundary_total, 3) if boundary_total else None
    min_elevation_m = min(valid_heights)

    # A flat placeholder patch for the FULL PADDED SQUARE — asked for
    # directly ("let's just show the full square... the full area that we
    # would show if we had it all"), a follow-up to the earlier version
    # which only filled the part of the plot's own boundary that lacked
    # data. Now covers every cell with no real corner at all, inside OR
    # outside the plot boundary, so the whole rendered area is always a
    # complete surface — never a hole. Deliberately a SEPARATE mesh (own
    # vertices/faces, own flat elevation) rather than blended into the
    # real terrain — it must never be mistaken for real data. "Zero
    # elevation" is flush with `min_elevation_m` (the same baseline the
    # black box's own base plane already sits at, see terrain3d.html's
    # minSceneY) — a real sea-level-relative zero would be a physically
    # meaningless flat plane at most Irish inland sites (routinely
    # 50-200m+ ASL), so the scene's own established baseline is the one
    # honest, consistent choice, not an arbitrary pick.
    no_data_vertices: list = []
    no_data_faces: list = []
    no_data_cache: dict = {}

    def add_no_data_vertex(itm_x: float, itm_y: float) -> int:
        key = (round(itm_x, 3), round(itm_y, 3))
        idx = no_data_cache.get(key)
        if idx is not None:
            return idx
        idx = len(no_data_vertices)
        no_data_vertices.append([round(itm_x - center_x, 2), round(itm_y - center_y, 2), round(min_elevation_m, 2)])
        no_data_cache[key] = idx
        return idx

    for ri in range(nrows - 1):
        for ci in range(ncols - 1):
            if coarse_valid[ri, ci] or coarse_valid[ri, ci + 1] or coarse_valid[ri + 1, ci] or coarse_valid[ri + 1, ci + 1]:
                continue  # at least one real corner here — the main loop above already drew something real (possibly a partial cut), no placeholder needed
            x0, x1 = grid_xs[ci], grid_xs[ci + 1]
            y0, y1 = grid_ys[ri], grid_ys[ri + 1]
            i00 = add_no_data_vertex(x0, y0)
            i10 = add_no_data_vertex(x1, y0)
            i01 = add_no_data_vertex(x0, y1)
            i11 = add_no_data_vertex(x1, y1)
            no_data_faces.append([i00, i10, i01])
            no_data_faces.append([i10, i11, i01])

    # Skip the whole placeholder (and its OSM tile fetch below) unless the
    # gap actually reaches the grid's own INTERIOR — confirmed necessary,
    # not a hypothetical: _erode_valid_mask() trims a few pixels off EVERY
    # mosaic's own outer edge as a matter of course (item 33), which on its
    # own produces a small but real border of "missing" cells around
    # literally every site, fully-covered ones included. A fixed-fraction
    # threshold doesn't work here either — confirmed live: that routine
    # border alone was 4.5% of a smaller site's own grid (Fermoy, 88x88),
    # comfortably past an initial 2% cutoff, since a FIXED pixel-width trim
    # is a BIGGER fraction of a SMALLER grid, not a stable one. So instead:
    # only keep the placeholder if some invalid point lies within the
    # INTERIOR (past `NO_DATA_OVERLAY_EDGE_MARGIN` from the array's own
    # true edge — the exact same margin `_fit_coverage_boundary_line()`
    # already uses for the identical "is this a routine edge artefact or a
    # real internal gap" question). Confirmed live: Fermoy's border is
    # entirely within that margin (no interior gap -> correctly skipped
    # now), while R32 E4F8's real half-covered 20ha parcel clearly reaches
    # deep into the interior (kept, as it should be).
    interior_row = np.array([NO_DATA_OVERLAY_EDGE_MARGIN <= r <= height - 1 - NO_DATA_OVERLAY_EDGE_MARGIN for r in row_indices])
    interior_col = np.array([NO_DATA_OVERLAY_EDGE_MARGIN <= c <= width - 1 - NO_DATA_OVERLAY_EDGE_MARGIN for c in col_indices])
    interior_mask = np.outer(interior_row, interior_col)
    if not bool((~coarse_valid & interior_mask).any()):
        no_data_vertices, no_data_faces = [], []

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

    # A real OpenStreetMap basemap for the no-data placeholder mesh above
    # — asked for directly, alongside "show the full square": "use the
    # OpenStreetMap view and stick that on that flat section... to give
    # some perspective, but then draw the property boundary on top of
    # it." Only fetched when there's actually a placeholder to texture —
    # a fully-covered site pays nothing extra. The plot boundary line is
    # baked directly onto this SAME image afterward (reusing
    # `_draw_plot_boundary()` — same extent/centre, so it's pixel-aligned
    # with the terrain's own overlay above by construction) so the
    # placeholder mesh only ever needs ONE self-contained texture, not
    # several composited layers on the frontend. No alpha-masking needed
    # here (unlike get_satellite_overlay()) — the placeholder MESH's own
    # geometry already only exists where there's no real data, so the
    # texture just needs to cover the full square; nothing extra to hide.
    no_data_map_png_base64 = None
    if no_data_faces:
        osm_mosaic = _fetch_map_mosaic(OSM_STANDARD_TILE_URL, center_lon, center_lat, {
            "x_min": extent[0], "x_max": extent[1], "y_min": extent[2], "y_max": extent[3],
        }, TEXTURE_SIZE, headers={"User-Agent": OSM_TILE_USER_AGENT})
        osm_img = Image.fromarray(osm_mosaic["rgb"], "RGB")
        osm_draw = ImageDraw.Draw(osm_img)
        _draw_plot_boundary(osm_draw, plot_geom, center_x, center_y, extent, TEXTURE_SIZE)
        buf = io.BytesIO()
        osm_img.save(buf, format="PNG")
        no_data_map_png_base64 = base64.b64encode(buf.getvalue()).decode("ascii")
        log.info("-> No-data placeholder basemap (OpenStreetMap): zoom %d, %d/%d tile(s) fetched",
                  osm_mosaic["zoom"], osm_mosaic["fetched"], osm_mosaic["tile_count"])

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
        "source": mosaic["source_display"],
        # "lidar" (real ~2m bare-ground survey) or "satellite_dem" (Copernicus
        # GLO-30 fallback, 30m global DSM — see _sample_copernicus_dem()).
        # boundary_lidar_coverage_fraction alone can't distinguish these: a
        # satellite_dem mesh is typically ~100% "covered" (GLO-30 has near-
        # total global coverage) despite being far coarser than real LIDAR —
        # the frontend needs THIS field to disclose that correctly rather
        # than reading a 100% fraction as "full real LIDAR coverage".
        "elevation_kind": mosaic["kind"],
        # Fraction of the WHOLE padded mesh area (not just the plot
        # boundary — see boundary_lidar_coverage_fraction for that) that's
        # Copernicus GLO-30 fill rather than real LIDAR — real LIDAR always
        # wins where it exists, this only ever fills genuine gaps (a survey
        # that doesn't reach edge-to-edge within its own tiles). 0.0 for a
        # fully-covered site; 1.0 only when elevation_kind is already
        # "satellite_dem" and there was no real LIDAR to blend with at all.
        "satellite_fill_fraction": mosaic["satellite_fill_fraction"],
        "origin_lon": center_lon,
        "origin_lat": center_lat,
        "grid_extent": {"x_min": extent[0], "x_max": extent[1], "y_min": extent[2], "y_max": extent[3]},
        "boundary_lidar_coverage_fraction": boundary_lidar_coverage_fraction,
        "no_data_overlay": {"vertices": no_data_vertices, "faces": no_data_faces, "map_png_base64": no_data_map_png_base64} if no_data_faces else None,
        "overlay_texture_png_base64": _encode_overlay(),
        "buildings": [],
        "road_count": 0,
        "river_count": 0,
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


def attach_features(result: dict, buildings: Optional[list] = None, roads: Optional[list] = None, rivers: Optional[list] = None) -> dict:
    """Extrudes OSM buildings as real 3D objects and draws OSM roads/rivers
    onto the overlay texture (buildings.py) onto an already-built
    get_terrain_mesh() result, using its stashed `_raw` pieces — lets
    webapp.py fetch buildings/roads/rivers CONCURRENTLY with the mesh build
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

    if roads or rivers:
        draw = ImageDraw.Draw(raw["overlay_img"])
        for r in roads or []:
            _draw_road_on_overlay(draw, r, center_x, center_y, raw["extent"], TEXTURE_SIZE)
        for r in rivers or []:
            _draw_river_on_overlay(draw, r, center_x, center_y, raw["extent"], TEXTURE_SIZE)
        result["overlay_texture_png_base64"] = raw["encode_overlay"]()
        result["road_count"] = len(roads or [])
        result["river_count"] = len(rivers or [])
        if roads:
            log.info("-> %d nearby road(s) painted onto the terrain's overlay texture", len(roads))
        if rivers:
            log.info("-> %d nearby river/stream(s) painted onto the terrain's overlay texture", len(rivers))

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


def _fetch_xyz_tile(tile_url_template: str, zoom: int, tile_x: int, tile_y: int, headers: Optional[dict] = None) -> Optional[np.ndarray]:
    """One 256x256 RGB tile from any standard {z}/{x}/{y} XYZ tile source —
    generalized from the original Esri-only version so the same pipeline
    can also drape OpenStreetMap's own standard tiles (see
    `get_terrain_mesh()`'s no-data-area basemap). Named `.format()`
    placeholders mean this works regardless of whether the URL orders them
    z/x/y (OpenStreetMap's own convention) or z/y/x (Esri's own — see
    SATELLITE_TILE_URL) — each template just puts `{z}`/`{x}`/`{y}` where
    that source expects them. None if the tile genuinely can't be fetched
    — degrades to a gap in the final image rather than failing the whole
    request (a visual enhancement layer, not core report data, same
    philosophy as buildings.py's OSM fetch).
    """
    url = tile_url_template.format(z=zoom, x=tile_x, y=tile_y)
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=SATELLITE_TILE_TIMEOUT_S, headers=headers)
            resp.raise_for_status()
            return np.array(Image.open(io.BytesIO(resp.content)).convert("RGB"))
        except Exception as exc:
            log.warning("Map tile fetch failed (attempt %d) for z=%d x=%d y=%d (%s): %s", attempt + 1, zoom, tile_x, tile_y, tile_url_template, exc)
    return None


def _fetch_map_mosaic(tile_url_template: str, origin_lon: float, origin_lat: float, grid_extent: dict,
                       texture_size: int, headers: Optional[dict] = None) -> dict:
    """Shared pipeline: reprojects the mesh's own local coordinate frame to
    Web Mercator pixel space (vectorized pyproj transform), fetches only
    the real tiles actually needed from whatever XYZ source is given, and
    samples an RGB image at `texture_size` resolution. Used by both
    `get_satellite_overlay()` (Esri World Imagery, masked to the plot
    boundary) and `get_terrain_mesh()`'s own no-data-area basemap
    (OpenStreetMap standard tiles, unmasked — see there) — identical
    reprojection/tile-fetch/sampling math either way, only the tile source
    and what the caller does with the result differ.

    Returns the sampled RGB array plus each texture pixel's own real ITM
    (x, y) position (`itm_x`/`itm_y` — for a caller that wants to mask or
    further composite the result) and the zoom/tile-count actually used.
    """
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
        tiles = list(pool.map(lambda t: (t, _fetch_xyz_tile(tile_url_template, zoom, t[0], t[1], headers)), tile_coords))
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

    return {"rgb": rgb, "itm_x": itm_x, "itm_y": itm_y, "zoom": zoom, "tile_count": len(tile_coords), "fetched": fetched}


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
    (see `_fetch_map_mosaic()`/`_fetch_xyz_tile()`).
    """
    plot_geom = _ring_sets_to_itm_polygon(polygon_ring_sets_wgs84)
    if plot_geom is None or plot_geom.is_empty:
        return None

    mosaic = _fetch_map_mosaic(SATELLITE_TILE_URL, origin_lon, origin_lat, grid_extent, texture_size)
    inside = shapely.contains_xy(plot_geom, mosaic["itm_x"], mosaic["itm_y"])
    rgba = np.zeros((texture_size, texture_size, 4), dtype=np.uint8)
    rgba[..., :3] = mosaic["rgb"]
    rgba[..., 3] = np.where(inside, 255, 0).astype(np.uint8)

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    log.info("-> Satellite overlay: zoom %d, %d/%d tile(s) fetched, %.1f%% of texture inside the plot boundary",
              mosaic["zoom"], mosaic["fetched"], mosaic["tile_count"], 100.0 * inside.mean())

    return {
        "found": True,
        "satellite_overlay_png_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
        "zoom_level": mosaic["zoom"],
        "tile_count": mosaic["tile_count"],
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

    # allow_satellite_fallback=True — asked for directly, reversing an
    # earlier deliberate choice: D8 flow routing on Copernicus GLO-30 (a
    # 30m DIGITAL SURFACE model — includes vegetation/building height, not
    # bare ground) is genuinely less trustworthy than on real LIDAR, since
    # canopy/building noise can produce sinks/ridges that don't reflect
    # real ground topography. But refusing to analyse at all was a worse
    # trade than running the SAME real analysis on coarser data and
    # disclosing that clearly — a site with no LIDAR at all still has a
    # real surface to work with (GLO-30 is real data, just coarser and
    # DSM-not-DTM), and the frontend now shows an explicit warning
    # whenever `elevation_kind` comes back "satellite_dem" (see
    # terrain3d.html) rather than silently treating the result as
    # survey-grade. `satellite_resolution_m=COPERNICUS_DEM_MESH_RESOLUTION_M`
    # (5m, bilinearly interpolated — not GLO-30's raw 30m) for the same
    # reason get_terrain_mesh() asks for it: a small plot's own area can be
    # just a handful of real 30m pixels across, too few cells for D8
    # routing to mean anything at all.
    mosaic = _mosaic_dtm(
        center_lat, center_lon, radius_m, require_data_at_point=False, allow_satellite_fallback=True,
        satellite_resolution_m=COPERNICUS_DEM_MESH_RESOLUTION_M,
    )
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
        # "lidar" or "satellite_dem" — see get_terrain_mesh()'s own
        # identical field for why the frontend needs this to disclose
        # coarser/DSM-derived analysis rather than presenting it as
        # survey-grade. `satellite_fill_fraction` mirrors the terrain
        # mesh's own — 0.0 on a fully-covered site, up to 1.0 when there's
        # no real LIDAR here at all.
        "elevation_kind": mosaic["kind"],
        "satellite_fill_fraction": mosaic["satellite_fill_fraction"],
        "source": mosaic["source_display"],
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
