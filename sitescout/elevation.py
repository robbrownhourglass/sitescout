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

Two sources tried in order (NASC first — newer, more tiles):
- OPW NASC (National Aerial Survey Contract): confirmed genuinely 2m,
  ~4MB per tile, DTM + DSM both present, uppercase filenames.
- OPW (older, pre-NASC): also confirmed genuinely 2m, ~4MB per tile,
  DTM + DSM both present, lowercase filenames.

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
import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Optional

import numpy as np
import requests
import shapely
import tifffile
from PIL import Image, ImageDraw
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.ops import unary_union

from . import config
from .arcgis import point_query, point_query_full

log = logging.getLogger("sitescout.elevation")

# (source label, coverage-index query URL, DTM filename pattern, DSM filename pattern)
LIDAR_SOURCES = [
    (
        "OPW NASC",
        "https://gsi.geodata.gov.ie/server/rest/services/Lidar/IE_GSI_LiDAR_Coverage_OPW_NASC_IE26_ITM/MapServer/3/query",
        "{name}_DTM.tif", "{name}_DSM.tif",
    ),
    (
        "OPW (pre-NASC)",
        "https://gsi.geodata.gov.ie/server/rest/services/Lidar/IE_GSI_LiDAR_Coverage_OPW_IE26_ITM/MapServer/3/query",
        "{name}_dtm.tif", "{name}_dsm.tif",
    ),
]
COVERAGE_OUT_FIELDS = "DATA_URL,DATA_NAME,RESOLUTION,DATECAPTUR,EXT_LEFT,EXT_TOP,EXT_RIGHT,EXT_BOTTOM"

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


def _download_and_extract(data_url: str, source_label: str, dtm_name: str, dsm_name: str) -> tuple[Optional[Path], Optional[Path]]:
    dtm_path = _cache_path(source_label, dtm_name)
    dsm_path = _cache_path(source_label, dsm_name)
    if dtm_path.exists():
        return dtm_path, (dsm_path if dsm_path.exists() else None)

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
        if dsm_name.lower() in names:
            _atomic_write(dsm_path, zf.read(names[dsm_name.lower()]))

    return dtm_path, (dsm_path if dsm_path.exists() else None)


def _read_pixel(tif_path: Path, itm_x: float, itm_y: float, ext_left: float, ext_top: float, ext_right: float, ext_bottom: float) -> Optional[float]:
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
    return None if value <= -9999 else value


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
    against the returned tiles' own EXT_* attributes rather than a second
    query: these coverage-index features are plain axis-aligned squares,
    so their extent IS their exact boundary — no geometry library needed
    for an exact (not approximate) point-in-tile test, same reasoning as
    the karst/contour decisions not to hand-roll real polygon geometry
    elsewhere in this app, just simpler here because these shapes are
    trivial rectangles.

    Deliberately sticks to ONE source per mosaic (whichever the point
    itself falls within, in LIDAR_SOURCES priority order) rather than
    mixing tiles from different surveys/years into one image.
    """
    search_radius_m = radius_m * 1.5  # > radius_m * sqrt(2) (~1.4142), a bit of extra margin against rounding
    itm_x, itm_y = _to_itm.transform(lon, lat)
    for source_label, coverage_url, dtm_pattern, dsm_pattern in LIDAR_SOURCES:
        log.info("Checking %s LIDAR coverage within %dm (for a %dm-radius square crop)…", source_label, search_radius_m, radius_m)
        try:
            feats = point_query(
                coverage_url, lon, lat, out_fields=COVERAGE_OUT_FIELDS,
                distance_m=search_radius_m, result_record_count=16,
            )
        except Exception as exc:
            log.warning("-> %s coverage query failed: %s", source_label, exc)
            continue
        if not feats:
            continue

        contains_point = any(
            f["attributes"]["EXT_LEFT"] <= itm_x <= f["attributes"]["EXT_RIGHT"]
            and f["attributes"]["EXT_BOTTOM"] <= itm_y <= f["attributes"]["EXT_TOP"]
            for f in feats
        )
        if not contains_point:
            log.info("-> %s has tiles nearby but none covering the exact point — trying next source", source_label)
            continue

        tiles = []
        for f in feats:
            a = f["attributes"]
            data_name = a["DATA_NAME"]
            dtm_name = dtm_pattern.format(name=data_name)
            dsm_name = dsm_pattern.format(name=data_name)
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
                "ext": (a["EXT_LEFT"], a["EXT_TOP"], a["EXT_RIGHT"], a["EXT_BOTTOM"]),
                "resolution": a.get("RESOLUTION"),
                "survey_date": a.get("DATECAPTUR"),
            })
        log.info("-> %s: %d tile(s) found (search radius %dm, for a %dm-radius square crop)", source_label, len(tiles), search_radius_m, radius_m)
        return source_label, tiles

    return None, []


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
            surface_m = _read_pixel(t["dsm_path"], itm_x, itm_y, tl, tt, tr, tb)
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
        "source": f"OPW LIDAR ({mosaic['source_label']})",
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
        "source": "OPW LIDAR (precise, where surveyed) + EPA Hydrological DTM contours (national, coarser)",
        "caveat": (
            "Precise elevation (OPW LIDAR, ~2m grid) only exists where OPW surveyed for flood-risk "
            "mapping — mainly rivers, floodplains, and coasts, not the whole country. Where it's not "
            "available, the contour lines (10m vertical interval, 20m grid) are the fallback — real "
            "elevation values, but far coarser, and shown as a range rather than a single figure since "
            "this app has no way to compute true point-to-contour distance."
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

    # A plain, uncipped rectangular grid — every cell whose 4 real corner
    # values are valid (not NoData) becomes 2 triangles, full stop. No
    # polygon test, no per-cell shapely call: dropping the exact-clip
    # approach (see the module comment above) means this loop is now both
    # simpler AND faster than the v2 version.
    for ri in range(nrows - 1):
        r0, r1 = row_indices[ri], row_indices[ri + 1]
        y0, y1 = grid_ys[ri], grid_ys[ri + 1]
        for ci in range(ncols - 1):
            c0, c1 = col_indices[ci], col_indices[ci + 1]
            x0, x1 = grid_xs[ci], grid_xs[ci + 1]
            h00, h10, h01, h11 = arr[r0, c0], arr[r0, c1], arr[r1, c0], arr[r1, c1]
            if h00 <= -9999 or h10 <= -9999 or h01 <= -9999 or h11 <= -9999:
                continue  # a real LIDAR data gap in this cell — never guess across NoData
            i00, i10 = add_vertex(x0, y0, h00), add_vertex(x1, y0, h10)
            i01, i11 = add_vertex(x0, y1, h01), add_vertex(x1, y1, h11)
            faces.append([i00, i10, i01])
            faces.append([i10, i11, i01])

    if not vertices:
        log.info("-> LIDAR coverage exists nearby but no cell in this padded area had valid data")
        return None

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
        "-> Terrain mesh: %d vertices, %d triangles (%.1fm grid, %dm padded context around the plot), %.1f-%.1fm",
        len(vertices), len(faces), resolution * step, int(radius_m), min(valid_heights), max(valid_heights),
    )
    result = {
        "found": True,
        "vertices": vertices,
        "faces": faces,
        "min_elevation_m": round(min(valid_heights), 2),
        "max_elevation_m": round(max(valid_heights), 2),
        "cell_size_m": resolution * step,
        "resolution_m": resolution,
        "source": f"OPW LIDAR ({mosaic['source_label']})",
        "origin_lon": center_lon,
        "origin_lat": center_lat,
        "grid_extent": {"x_min": extent[0], "x_max": extent[1], "y_min": extent[2], "y_max": extent[3]},
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
