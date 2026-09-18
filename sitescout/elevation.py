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


def _find_covering_tiles(lat: float, lon: float):
    """Generator, not a list — LIDAR_SOURCES whose coverage index
    geometrically contains this point, downloaded+cached, yielded lazily
    in source priority order (NASC first) so a caller that only wants the
    first hit (render_dtm_image()) doesn't pay for querying every
    remaining source once it already has what it needs. get_precise_
    elevation() instead consumes the whole generator, since it needs to
    fall through to the next source on a NoData pixel.

    Split out from get_precise_elevation() so render_dtm_image() can reuse
    the same tile-resolution logic without duplicating it — rendering
    doesn't care whether the exact query pixel is NoData (a whole tile can
    still be worth drawing even if one specific point in it has a gap),
    so it can't just reuse get_precise_elevation()'s own NoData-driven
    fallback loop directly.
    """
    for source_label, coverage_url, dtm_pattern, dsm_pattern in LIDAR_SOURCES:
        log.info("Checking %s LIDAR coverage…", source_label)
        try:
            feats = point_query(coverage_url, lon, lat, out_fields=COVERAGE_OUT_FIELDS)
        except Exception as exc:
            log.warning("-> %s coverage query failed: %s", source_label, exc)
            continue
        if not feats:
            continue

        a = feats[0]["attributes"]
        data_name = a["DATA_NAME"]
        dtm_name = dtm_pattern.format(name=data_name)
        dsm_name = dsm_pattern.format(name=data_name)
        try:
            dtm_path, dsm_path = _download_and_extract(a["DATA_URL"], source_label, dtm_name, dsm_name)
        except Exception as exc:
            log.warning("-> %s tile download/extract failed: %s", source_label, exc)
            continue
        if not dtm_path:
            continue

        yield {
            "source_label": source_label,
            "dtm_path": dtm_path,
            "dsm_path": dsm_path,
            "ext": (a["EXT_LEFT"], a["EXT_TOP"], a["EXT_RIGHT"], a["EXT_BOTTOM"]),
            "resolution": a.get("RESOLUTION"),
            "survey_date": a.get("DATECAPTUR"),
        }


def _itm_bounds_to_wgs84(ext: tuple) -> list:
    ext_left, ext_top, ext_right, ext_bottom = ext
    lon_sw, lat_sw = _from_itm.transform(ext_left, ext_bottom)
    lon_ne, lat_ne = _from_itm.transform(ext_right, ext_top)
    return [[lat_sw, lon_sw], [lat_ne, lon_ne]]


def _dtm_range(tif_path: Path) -> Optional[tuple[float, float]]:
    """Full-array min/max, ignoring NoData. Only used by
    get_precise_elevation() below, purely to caption the map image with a
    real legend ("this tile ranges from X to Ym") — render_dtm_image()
    needs this exact same min/max anyway for its own color scale, but
    computes it from the array it's already reading for pixel colors
    rather than calling this and reading the file a second time.
    """
    arr = tifffile.imread(str(tif_path)).astype(np.float32)
    valid = arr > -9999
    if not valid.any():
        return None
    return float(arr[valid].min()), float(arr[valid].max())


def get_precise_elevation(lat: float, lon: float) -> dict:
    itm_x, itm_y = _to_itm.transform(lon, lat)

    for tile in _find_covering_tiles(lat, lon):
        ground_m = _read_pixel(tile["dtm_path"], itm_x, itm_y, *tile["ext"])
        surface_m = _read_pixel(tile["dsm_path"], itm_x, itm_y, *tile["ext"]) if tile["dsm_path"] else None
        if ground_m is None:
            log.info("-> %s tile found but point is NoData (edge/water gap) — trying next source", tile["source_label"])
            continue

        log.info(
            "-> %s: ground %.2fm%s", tile["source_label"], ground_m,
            f", surface {surface_m:.2f}m" if surface_m is not None else "",
        )
        image_range = _dtm_range(tile["dtm_path"])
        return {
            "found": True,
            "ground_elevation_m": round(ground_m, 2),
            "surface_elevation_m": round(surface_m, 2) if surface_m is not None else None,
            "canopy_or_building_height_m": round(surface_m - ground_m, 2) if surface_m is not None else None,
            "resolution_m": tile["resolution"],
            "survey_date": tile["survey_date"],
            "source": f"OPW LIDAR ({tile['source_label']})",
            "bounds_wgs84": _itm_bounds_to_wgs84(tile["ext"]),
            "image_min_elevation_m": round(image_range[0], 1) if image_range else None,
            "image_max_elevation_m": round(image_range[1], 1) if image_range else None,
            # Not rendered here — the image is only generated on demand,
            # when the map layer is actually toggled on (see webapp.py's
            # /api/terrain-image route and render_dtm_image() below), same
            # lazy-fetch pattern as the contour tile layer. bounds_wgs84 and
            # the min/max above are cheap enough (extent is plain metadata;
            # the range read is one more small array read) to include
            # eagerly rather than round-tripping again just for these.
            "image_url": f"/api/terrain-image?lat={lat}&lon={lon}",
        }

    log.info("-> No precise LIDAR coverage at this point")
    return {"found": False}


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
    """Renders the DTM tile covering (lat, lon) as a real per-pixel
    elevation image — every pixel's color directly encodes that pixel's
    own genuine LIDAR elevation value (min/max-normalised per tile, since
    a fixed national scale would wash out contrast in any one 2km tile),
    not a hillshade and not a derived/coarser product. This is the actual
    2m data requested — the "Terrain & elevation" card's single point
    value only ever showed one pixel out of the ~1000x1000 in the tile;
    this shows all of them.

    Called on demand (see webapp.py's /api/terrain-image route), not
    during the main site lookup — same lazy-fetch pattern as the contour
    tile layer, so a site that never gets this layer toggled on never
    pays the rendering cost.
    """
    tile = next(_find_covering_tiles(lat, lon), None)  # first hit only — stops the generator there, no wasted queries against remaining sources
    if not tile:
        return None

    arr = tifffile.imread(str(tile["dtm_path"])).astype(np.float32)
    valid = arr > -9999
    if not valid.any():
        return None

    vmin, vmax = float(arr[valid].min()), float(arr[valid].max())
    span = max(vmax - vmin, 0.01)  # guard against a div-by-zero on a perfectly flat tile
    normalized = np.clip((arr - vmin) / span, 0, 1)
    rgb = _elevation_to_rgb(normalized)
    alpha = np.where(valid, 200, 0).astype(np.uint8)  # NoData pixels fully transparent
    rgba = np.dstack([rgb, alpha])

    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")

    return {
        "png_bytes": buf.getvalue(),
        "bounds_wgs84": _itm_bounds_to_wgs84(tile["ext"]),
        "min_elevation_m": round(vmin, 1),
        "max_elevation_m": round(vmax, 1),
        "source_label": tile["source_label"],
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
