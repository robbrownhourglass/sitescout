"""Nearby wells, springs & boreholes — GSI's own drilling records, the
closest thing to a real, on-the-ground water-table depth this app has
access to.

Directly answers a real user question ("does any of the hydrology tell us
about the aquifer and how deep the water table is?") — investigated and
confirmed live before building anything: `gsi.py`'s groundwater
vulnerability and `geohazards.py`'s aquifer classification are both real,
but both are categorical (a vulnerability class, an aquifer type/
productivity band) — neither gives an actual depth-to-water figure at any
point. GSI's own live groundwater-level monitoring site (gwlevel.ie) does
have real depth data, but is a custom Leaflet app whose data API couldn't
be found through static analysis in reasonable time, and even if found
would likely cover far fewer locations nationally than the source below.

Found on the SAME `gsi.geodata.gov.ie` ArcGIS server `gsi.py`/
`geohazards.py` already use — its `Groundwater` folder (already the source
of aquifer/vulnerability/karst/source-protection) also has
`IE_GSI_Groundwater_Wells_Springs_100K_IE26_ITM`, a real per-well drilling
record dataset. Its `H2OSTRIKE1`-`H2OSTRIKE4` fields ("First/Second/Third/
Fourth reported waterstrike met when drilling (m)") are a genuine,
directly-measured depth-to-water at that exact well — not a modeled
estimate, unlike every other groundwater figure elsewhere in this app.

Two honest caveats, matching the layer's own description text exactly
("It is NOT a comprehensive database and many wells and springs are not
included... You should not rely only on this database"): (1) this is a
NEARBY-WELL radius search, not a value at the query point itself — same
"list what's actually mapped nearby" pattern as heritage.py's SMR
monuments or geohazards.py's karst features, not a zone-overlap category
(a well's own water strike says nothing about a DIFFERENT point 200m
away); (2) confirmed live across 5 real test sites that plenty of nearby
wells have every H2OSTRIKE field null (no water-strike depth was
recorded for that well) — real, disclosed sparseness, not a bug. Also
confirmed live: `DRILL_DATE` uses a real sentinel value for "no date
recorded" (`-2209161600000`ms, i.e. 1899-12-30 — appeared in nearly half
of a 43-well sample), not just missing data; dates before 1950 are
treated as unrecorded rather than shown as a real drilling year.

The layer's own geometry type is `esriGeometryPolygon` (a small circle
sized by the record's own location-accuracy, per the layer's
description), not a point — `boundary.ring_set_to_polygon()` (already a
verified dependency, added for real boundary-overlap checks elsewhere —
see CLAUDE.md) gives a real polygon centroid for the map marker rather
than a hand-rolled average.

**Depth estimate — asked for directly** ("could we model the aquifer
depth... based on the samples we do have?"), only built after checking it
was actually justified, not assumed: (1) real published hydrogeology
(the Hydrogeology Journal's typology of Irish hard-rock aquifers) says
Irish bedrock groundwater flow is fracture-controlled and concentrated
near the surface, declining with depth — confirmed directly in THIS
dataset too: boreholes with a "Poor" yield outcome struck water deeper
on average (36.6m) than "Excellent"/"Good" ones (29-31m) in a national
sample, the opposite of "drill deeper for more yield." (2) a real
semivariogram-style check (pairs of nearby boreholes, water-strike depth
difference vs. distance apart) confirmed genuine spatial correlation up
to ~2-5km (median difference 2.3m at <500m, growing to ~9m by 5km, then
PLATEAUING — a well past ~5-10km carries little more predictive value
than the national spread). That's real justification for a distance-
weighted estimate, not just "nearby data must be relevant."

`get_water_table_estimate()` runs a separate, ADAPTIVE-radius search per
hole type (Borehole vs. Dug well — genuinely different constructions,
never pooled) — starting at the same 5km as the plain nearby-wells list,
widening only if too few real records are found, since the semivariogram
above showed correlation decays past that point anyway (widening
unconditionally would just dilute the estimate with less-relevant
records). Weights each real record by inverse distance, using
`boundary.distance_m()`'s flat lat/lon approximation.

**A real bug, caught before shipping, worth documenting**: this
originally used the layer's own `X_ING`/`Y_ING` attribute fields,
assuming they were the same ITM (EPSG:2157) CRS the layer's geometry is
stored in — reasonable-looking, since the values are a similar order of
magnitude. Wrong: their own field alias says "X/Y Easting/Northing
(ING)" — Irish National Grid (EPSG:29903), a DIFFERENT, older Irish CRS
with a different origin, confirmed by the actual computed distances
coming out absurdly large (600km+ for records the spatial query had
already confirmed were within 5-40km). Fixed by using each record's own
`return_geometry=True` centroid (already computed via
`boundary.ring_set_to_polygon()`, WGS84) and the same flat-degree
distance approximation already established elsewhere in this app,
instead of trusting an ambiguous attribute field's CRS.

Reports the estimate ALONGSIDE its own real spread (sample count,
min/max, the single closest real record) — never a lone confident
number, since even the tightest (<500m) bucket in the semivariogram
check still had a real ~2-6m spread.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from . import boundary
from .arcgis import point_query

log = logging.getLogger("sitescout.wells")

WELLS_URL = (
    "https://gsi.geodata.gov.ie/server/rest/services/Groundwater/"
    "IE_GSI_Groundwater_Wells_Springs_100K_IE26_ITM/MapServer/0/query"
)

SEARCH_RADIUS_M = 5000
RESULT_CAP = 50

OUT_FIELDS = (
    "GSI_NAME,SOURCENAM,SOURCETYP,HOLEDEPTHM,DTB_M,DRILL_DATE,COUNTY,TOWNLAND,"
    "YIELDCLASS,PROD_CLASS,YIELD_M3D,DRAWDOWN_M,"
    "H2OSTRIKE1,H2OSTRIKE2,H2OSTRIKE3,H2OSTRIKE4"
)

# A shallow water table is a real, practical foundation/septic-percolation
# concern, not just a curiosity — banded the same way radon/WFD status get
# a plain-language severity elsewhere in this app, rather than leaving the
# user to interpret a raw metres figure themselves.
SHALLOW_WATER_STRIKE_M = 2.0

# Confirmed live (see module docstring): a real, recurring "no date
# recorded" sentinel, not a genuine drilling year.
_MIN_PLAUSIBLE_DRILL_YEAR = 1950

# Only these two — genuinely different construction methods, never pooled
# into one estimate (a spring/infiltration gallery has no comparable
# "depth you'd drill/dig" concept, and both are rare — see CLAUDE.md's
# national SOURCETYP breakdown).
ESTIMATE_SOURCE_TYPES = ["Borehole", "Dug well"]
# Starts at the same radius as the plain nearby-wells list, widening only
# if too few real records are found there — the semivariogram check (see
# module docstring) showed real records past ~5-10km add little more
# predictive value, so widening unconditionally would just dilute the
# estimate rather than improve it.
ESTIMATE_RADII_M = [5000, 10000, 20000, 40000]
ESTIMATE_MIN_SAMPLES = 5
ESTIMATE_MAX_CANDIDATES = 200
IDW_POWER = 2
# A record essentially at the query point shouldn't get a near-infinite
# weight from a near-zero distance — floors the weight at what a real
# 10m-away record would get.
IDW_DISTANCE_FLOOR_M = 10.0


def _shallowest_strike(attrs: dict) -> Optional[float]:
    strikes = [attrs.get(f"H2OSTRIKE{i}") for i in range(1, 5)]
    real = [s for s in strikes if s is not None]
    return min(real) if real else None


def _drill_year(epoch_ms: Optional[int]) -> Optional[int]:
    if epoch_ms is None:
        return None
    try:
        year = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).year
    except (OverflowError, OSError, ValueError):
        return None
    return year if year >= _MIN_PLAUSIBLE_DRILL_YEAR else None


def _idw_estimate(candidates: list) -> Optional[dict]:
    """`candidates`: list of (dist_m, depth_m), distance already computed
    by the caller. Inverse-distance-weighted estimate plus its own honest
    spread — never just the weighted number alone (see module docstring
    on why)."""
    if not candidates:
        return None
    weighted_sum = 0.0
    weight_total = 0.0
    depths = []
    closest = None
    for dist_m, depth in candidates:
        weight = 1.0 / max(dist_m, IDW_DISTANCE_FLOOR_M) ** IDW_POWER
        weighted_sum += weight * depth
        weight_total += weight
        depths.append(depth)
        if closest is None or dist_m < closest[0]:
            closest = (dist_m, depth)
    return {
        "estimated_depth_m": round(weighted_sum / weight_total, 1),
        "sample_count": len(candidates),
        "min_depth_m": round(min(depths), 1),
        "max_depth_m": round(max(depths), 1),
        "closest_record_distance_m": round(closest[0]),
        "closest_record_depth_m": closest[1],
    }


def _estimate_for_type(lat: float, lon: float, source_type: str) -> dict:
    feats: list = []
    radius_m = ESTIMATE_RADII_M[-1]
    for radius_m in ESTIMATE_RADII_M:
        feats = point_query(
            WELLS_URL, lon, lat,
            out_fields="H2OSTRIKE1,H2OSTRIKE2,H2OSTRIKE3,H2OSTRIKE4",
            distance_m=radius_m,
            return_geometry=True,
            where=f"SOURCETYP='{source_type}' AND H2OSTRIKE1 IS NOT NULL",
            result_record_count=ESTIMATE_MAX_CANDIDATES,
        )
        if len(feats) >= ESTIMATE_MIN_SAMPLES:
            break

    candidates = []
    for f in feats:
        a = f["attributes"]
        depth = _shallowest_strike(a)
        shape = boundary.ring_set_to_polygon((f.get("geometry") or {}).get("rings"))
        centroid = shape.centroid if shape else None
        if depth is not None and centroid is not None:
            dist_m = boundary.distance_m(lat, lon, centroid.y, centroid.x)
            candidates.append((dist_m, depth))

    result = _idw_estimate(candidates)
    if result is None:
        return {
            "source_type": source_type,
            "estimated_depth_m": None,
            "sample_count": 0,
            "search_radius_m": radius_m,
            "note": f"No {source_type.lower()} within {radius_m / 1000:.0f}km has a recorded water-strike depth.",
        }
    result["source_type"] = source_type
    result["search_radius_m"] = radius_m
    return result


def get_water_table_estimate(lat: float, lon: float) -> dict:
    """A distance-weighted estimate of likely water-strike depth here, one
    per hole type — see module docstring for why this is empirically
    justified (a real semivariogram check on this exact dataset) rather
    than just "nearby data must be relevant." NOT a substitute for a
    hydrogeological site investigation — every result carries its own
    real sample count and spread alongside the single weighted figure.
    """
    estimates = {}
    for source_type in ESTIMATE_SOURCE_TYPES:
        log.info("Estimating %s water-strike depth from nearby real records…", source_type.lower())
        est = _estimate_for_type(lat, lon, source_type)
        estimates[source_type] = est
        if est.get("estimated_depth_m") is not None:
            log.info(
                "-> %s: ~%.1fm (%d real record(s) within %dm, range %.1f-%.1fm)",
                source_type, est["estimated_depth_m"], est["sample_count"], est["search_radius_m"],
                est["min_depth_m"], est["max_depth_m"],
            )
        else:
            log.info("-> %s: %s", source_type, est.get("note"))
    return estimates


def _fetch_raw_nearby(lat: float, lon: float, radius_m: int) -> list:
    log.info("Querying GSI wells, springs & boreholes within %dm…", radius_m)
    return point_query(
        WELLS_URL, lon, lat,
        out_fields=OUT_FIELDS,
        distance_m=radius_m,
        return_geometry=True,
        result_record_count=RESULT_CAP,
    )


def get_nearby_wells(lat: float, lon: float, radius_m: int = SEARCH_RADIUS_M) -> dict:
    # The plain nearby-wells list and the depth estimate (its own, wider,
    # per-hole-type radius search — see get_water_table_estimate()) are
    # independent live queries against the same server; run them
    # concurrently rather than adding their latencies together.
    with ThreadPoolExecutor(max_workers=2) as executor:
        raw_future = executor.submit(_fetch_raw_nearby, lat, lon, radius_m)
        estimate_future = executor.submit(get_water_table_estimate, lat, lon)
        feats = raw_future.result()
        depth_estimates = estimate_future.result()

    wells = []
    for f in feats:
        a = f["attributes"]
        shape = boundary.ring_set_to_polygon((f.get("geometry") or {}).get("rings"))
        centroid = shape.centroid if shape else None
        strike_m = _shallowest_strike(a)
        wells.append({
            "name": a.get("SOURCENAM") or a.get("GSI_NAME"),
            "gsi_ref": a.get("GSI_NAME"),
            "source_type": a.get("SOURCETYP"),
            "water_strike_m": strike_m,
            "hole_depth_m": a.get("HOLEDEPTHM"),
            "depth_to_bedrock_m": a.get("DTB_M"),
            "yield_class": a.get("YIELDCLASS"),
            "yield_m3d": a.get("YIELD_M3D"),
            "drawdown_m": a.get("DRAWDOWN_M"),
            "county": a.get("COUNTY"),
            "townland": a.get("TOWNLAND"),
            "drill_year": _drill_year(a.get("DRILL_DATE")),
            "lat": centroid.y if centroid else None,
            "lon": centroid.x if centroid else None,
        })

    with_strike = [w for w in wells if w["water_strike_m"] is not None]
    with_strike.sort(key=lambda w: w["water_strike_m"])
    shallowest = with_strike[0] if with_strike else None

    if shallowest:
        severity = "warn" if shallowest["water_strike_m"] <= SHALLOW_WATER_STRIKE_M else "ok"
    else:
        severity = "warn"  # no depth-to-water data at all nearby — unknown, not "clean" (same convention as water_quality.py)

    log.info(
        "-> %d well(s)/spring(s)/borehole(s) within %dm, %d with a recorded water-strike depth%s",
        len(wells), radius_m, len(with_strike),
        f" (shallowest: {shallowest['water_strike_m']}m)" if shallowest else "",
    )

    return {
        "well_count": len(wells),
        "wells_with_depth_count": len(with_strike),
        "wells": wells,
        "shallowest_water_strike_m": shallowest["water_strike_m"] if shallowest else None,
        "search_radius_m": radius_m,
        "depth_estimates": depth_estimates,
        "severity": severity,
        "source": "Geological Survey Ireland (GSI) Wells, Springs & Boreholes",
        "caveat": (
            "This is a radius search of GSI's own drilling records, not a value at the exact site — "
            "the nearest well may still be some distance away, and water level naturally varies "
            "seasonally and between locations. GSI's own database is NOT comprehensive; many real "
            "wells in any given area are not included. A shallow water strike nearby is a real "
            "foundation/septic-percolation due-diligence flag, not a site-specific determination — "
            "a professional ground investigation would confirm actual conditions at this exact site."
        ),
    }
