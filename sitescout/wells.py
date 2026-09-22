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
"""
from __future__ import annotations

import logging
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


def get_nearby_wells(lat: float, lon: float, radius_m: int = SEARCH_RADIUS_M) -> dict:
    log.info("Querying GSI wells, springs & boreholes within %dm…", radius_m)
    feats = point_query(
        WELLS_URL, lon, lat,
        out_fields=OUT_FIELDS,
        distance_m=radius_m,
        return_geometry=True,
        result_record_count=RESULT_CAP,
    )

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
