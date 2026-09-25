"""Shared helper for checking whether a candidate zone/designation polygon
genuinely OVERLAPS the user's confirmed plot boundary — not just whether a
single representative point (the search pin, or the boundary's own
centroid) happens to fall inside it.

Real user report, with a screenshot: an SMR Zone visibly overlapped the
confirmed plot's own boundary on the map, but the card said "not within an
SMR Zone" — because the underlying check (heritage.get_smr_zone(), same
pattern in several other modules) only ever tested a single point, not the
real shape the user actually selected. Asked for directly: for any
"is this site within/does this overlap a mapped zone" category, check the
CONFIRMED BOUNDARY against the zone's real polygon, not a point — and keep
the point-radius approach only for genuine distance/proximity searches
("nearby monuments within 2km"), which the user explicitly distinguished
from overlap checks in the same message.

Every module that supports this takes an optional `boundary_ring_sets_wgs84`
parameter (cadastral.py's own `polygon_ring_sets_wgs84` convention — a list
of ring-sets, one per confirmed/merged parcel, each ring-set itself
`[outer_ring, hole_ring, ...]`, each ring a list of `[lon, lat]` pairs) and
falls back to its EXISTING point-based behaviour when it's None or empty —
which is the case for the CLI (no plot picker, no confirmed boundary at
all) and for the web UI's own initial speculative section fetch (fired the
moment the point resolves, before the user has picked/confirmed a parcel —
see webapp.py's docstring and CLAUDE.md's item on the pin/parcel
correction). The web UI re-fetches every relevant section with the real
confirmed boundary once it's known (see index.html's confirmPlotSelection/
refetchAllSectionsAt).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

log = logging.getLogger("sitescout.boundary")


def ring_set_to_polygon(ring_set: Optional[list]) -> Optional[Polygon]:
    """A single feature's own rings (`[outer, hole, ...]`, Esri's own
    `rings` convention — also what a GeoJSON Polygon's `coordinates`
    already is) -> a real shapely Polygon, or None for a degenerate/empty
    ring set. Self-intersecting real-world source data (rare, but not
    unheard of in public datasets) is repaired with shapely's own standard
    `buffer(0)` trick rather than silently failing the whole overlap check.
    """
    if not ring_set or not ring_set[0] or len(ring_set[0]) < 3:
        return None
    try:
        poly = Polygon(ring_set[0], ring_set[1:])
    except Exception:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if poly and not poly.is_empty else None


def ring_sets_to_shape(ring_sets: Optional[list]):
    """A LIST of ring-sets (cadastral.py's own `polygon_ring_sets_wgs84`
    convention: one ring-set per parcel for the confirmed boundary, or one
    per part of a MultiPolygon for a single candidate feature) -> one
    combined shapely geometry (a union), or None if nothing usable."""
    polys = [p for p in (ring_set_to_polygon(rs) for rs in (ring_sets or [])) if p]
    if not polys:
        return None
    return unary_union(polys)


def radius_covering_m(lat: float, lon: float, ring_sets: Optional[list], minimum_m: float) -> float:
    """Smallest radius (metres) around (lat, lon) that comfortably covers
    every point in `ring_sets`, or `minimum_m` if that's already bigger —
    so passing this into an existing fixed-radius search never SHRINKS it.
    Used to widen a zone-search buffer to reliably cover a whole confirmed
    (possibly multi-parcel, merged) boundary rather than just the app's
    existing per-source default radius, which was sized for a single
    representative point, not an arbitrary real plot shape. Flat lat/lon
    degrees-to-metres approximation, consistent with the same convention
    already used elsewhere in this app (see wfs.py's own `_bbox_around`).
    """
    if not ring_sets:
        return minimum_m
    max_dist = 0.0
    for ring_set in ring_sets:
        for ring in ring_set:
            for point_lon, point_lat in ring:
                dlat_m = (point_lat - lat) * 111_000
                dlon_m = (point_lon - lon) * 111_000 * math.cos(math.radians(lat))
                dist = math.hypot(dlat_m, dlon_m)
                if dist > max_dist:
                    max_dist = dist
    return max(minimum_m, max_dist + 200)  # +200m margin — a candidate search radius, not the overlap test itself


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Plain two-point distance in metres — the same flat lat/lon
    degrees-to-metres approximation as `radius_covering_m()` above (see
    its own note on why this is an accepted convention at this scale,
    already used elsewhere in this app). Good enough for ranking/weighting
    nearby real records by distance (wells.py's depth estimate); NOT
    precise enough to trust for survey-grade measurement.
    """
    dlat_m = (lat2 - lat1) * 111_000
    dlon_m = (lon2 - lon1) * 111_000 * math.cos(math.radians(lat1))
    return math.hypot(dlat_m, dlon_m)


def geojson_geometry_to_ring_sets(geometry: Optional[dict]) -> list:
    """A GeoJSON Polygon/MultiPolygon geometry (as returned by a WFS
    GetFeature response, `srsName=EPSG:4326` — see wfs.py) -> this app's
    usual ring-sets convention (cadastral.py's own
    `polygon_ring_sets_wgs84`). A MultiPolygon's own `coordinates` already
    IS a list of ring-sets; a Polygon's is wrapped in a list of one, so
    callers always get the same shape either way.
    """
    if not geometry:
        return []
    if geometry.get("type") == "MultiPolygon":
        return geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        return [geometry.get("coordinates", [])]
    return []


def points_within(boundary_ring_sets: Optional[list], points: list) -> Optional[set]:
    """Like find_overlapping() but for POINT candidates rather than
    polygons — `points` is a list of `(key, lon, lat)` triples (a planning
    application's own site coordinate, say). Returns the set of keys whose
    point genuinely falls within the boundary, or None if no usable
    boundary was given at all (same "unknown, not confirmed-empty"
    convention as find_overlapping — lets a caller tell "no boundary known
    yet" apart from "boundary known, nothing's actually on it").
    """
    if not boundary_ring_sets:
        return None
    boundary_shape = ring_sets_to_shape(boundary_ring_sets)
    if boundary_shape is None:
        return None
    within = set()
    for key, lon, lat in points:
        if lon is None or lat is None:
            continue
        if boundary_shape.intersects(Point(lon, lat)):
            within.add(key)
    return within


def find_overlapping(boundary_ring_sets: Optional[list], candidates: list) -> Optional[set]:
    """`candidates` is a list of `(key, ring_sets)` pairs — each `key`
    whatever the caller wants back (an id, an index, ...), `ring_sets` in
    the same list-of-ring-sets convention as the boundary itself. Returns
    the set of keys whose geometry genuinely intersects the boundary, or
    None if no usable boundary was given at all — deliberately NOT an
    empty set in that case, so callers can tell "no boundary known yet,
    fall back to point-based behaviour" apart from "boundary known, really
    nothing overlaps."
    """
    if not boundary_ring_sets:
        return None
    boundary_shape = ring_sets_to_shape(boundary_ring_sets)
    if boundary_shape is None:
        return None
    overlapping = set()
    for key, ring_sets in candidates:
        shape = ring_sets_to_shape(ring_sets)
        if shape is not None and boundary_shape.intersects(shape):
            overlapping.add(key)
    return overlapping
