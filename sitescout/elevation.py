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

import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Optional

import numpy as np
import requests
import tifffile
from PIL import Image
from pyproj import Transformer

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
    itm_x, itm_y = _to_itm.transform(lon, lat)
    for source_label, coverage_url, dtm_pattern, dsm_pattern in LIDAR_SOURCES:
        log.info("Checking %s LIDAR coverage within %dm…", source_label, radius_m)
        try:
            feats = point_query(
                coverage_url, lon, lat, out_fields=COVERAGE_OUT_FIELDS,
                distance_m=radius_m, result_record_count=10,
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
        log.info("-> %s: %d tile(s) touching %dm radius", source_label, len(tiles), radius_m)
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
