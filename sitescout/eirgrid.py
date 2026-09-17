"""EirGrid transmission grid infrastructure — existing and committed
(planned) substations, overhead lines, and underground cables.

Distinct from ESB Networks (utilities.py) — ESB Networks operates the
local *distribution* network (low/medium voltage lines feeding individual
buildings), confirmed genuinely closed/security-sensitive data. EirGrid
operates the national *transmission* network (110kV+, the backbone
between substations) — public because new transmission infrastructure
requires statutory public consultation, and EirGrid itself publishes a
public Transmission Development Plan (TDP) web map for exactly that
purpose.

Found via ArcGIS Online's own public content search
(`GET https://www.arcgis.com/sharing/rest/search?q=eirgrid&f=json`) rather
than reverse-engineering a specific viewer — turned up "TDP 2024 Web Map
PUBLIC", a Feature Service owned by EirGrid's own ArcGIS org
(`Cristobal.Albistur_eirgrid2`), confirmed live and current (modified
2024). Six relevant layers on one FeatureServer: existing stations
(substations), existing overhead lines, existing underground cables, and
a "Committed Projects" counterpart of each (approved/in-progress future
works) — plus a few named-project layers (Celtic/Greenlink/East-West
interconnectors) not used here.

Confirmed live: Knockumber substation (110kV) near Navan, Co. Meath is
literally the connection point feeding Boliden Tara Mines (found while
building epa.py's PRTR addition) — a nice cross-check that this dataset
reflects real, current grid topology.
"""
from __future__ import annotations

import logging
import math

from .arcgis import point_query

log = logging.getLogger("sitescout.eirgrid")

BASE_URL = (
    "https://services-eu1.arcgis.com/1VGs4Se8lewgdzfE/arcgis/rest/services/"
    "TDP_2024_Web_Map_PUBLIC/FeatureServer"
)
STATIONS_LAYER = f"{BASE_URL}/38/query"
OVERHEAD_LINES_LAYER = f"{BASE_URL}/40/query"
UNDERGROUND_CABLES_LAYER = f"{BASE_URL}/39/query"
COMMITTED_STATIONS_LAYER = f"{BASE_URL}/42/query"
COMMITTED_OVERHEAD_LAYER = f"{BASE_URL}/41/query"
COMMITTED_UNDERGROUND_LAYER = f"{BASE_URL}/43/query"

# Substations matter at a much wider radius than lines/cables — "is there
# a connection point within a few km" is the relevant grid-feasibility
# question, unlike a line/cable, which only really matters for a
# wayleave/easement/visual-impact assessment if it's genuinely close by.
STATION_SEARCH_RADIUS_M = 5000
LINE_SEARCH_RADIUS_M = 1000
# Within this distance, an overhead line or underground cable likely
# crosses or closely borders the site itself — a real wayleave/easement
# flag, not just "grid infrastructure exists in the area".
LINE_CROSSING_THRESHOLD_M = 100


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _stations(layer_url: str, lat: float, lon: float, radius_m: float) -> list[dict]:
    feats = point_query(layer_url, lon, lat, distance_m=radius_m, result_record_count=10, return_geometry=True)
    stations = []
    for f in feats:
        a = f["attributes"]
        g = f.get("geometry") or {}
        s_lat, s_lon = g.get("y"), g.get("x")
        distance_m = round(_haversine_m(lat, lon, s_lat, s_lon)) if s_lat is not None else None
        stations.append({
            "name": a.get("ST_NAME"),
            "voltage": a.get("VOLTAGE"),
            "lat": s_lat,
            "lon": s_lon,
            "distance_m": distance_m,
        })
    stations.sort(key=lambda s: s["distance_m"] if s["distance_m"] is not None else float("inf"))
    return stations


def _lines(layer_url: str, lat: float, lon: float, radius_m: float) -> list[dict]:
    feats = point_query(layer_url, lon, lat, distance_m=radius_m, result_record_count=25, return_geometry=True)
    lines = []
    for f in feats:
        a = f["attributes"]
        g = f.get("geometry") or {}
        lines.append({
            "name": a.get("CSECT_NAME") or a.get("CIRCUIT"),
            "voltage": a.get("VOLTAGE"),
            "category": a.get("CATEGORY"),
            "state": a.get("STATE"),
            # ArcGIS's own polyline shape — a list of paths, each a list of
            # [lon, lat] pairs, already WGS84 (outSR=4326 via return_geometry).
            "line_paths_wgs84": g.get("paths") or [],
        })
    return lines


def get_transmission_grid(lat: float, lon: float) -> dict:
    log.info("Querying EirGrid transmission grid infrastructure…")
    stations = _stations(STATIONS_LAYER, lat, lon, STATION_SEARCH_RADIUS_M)
    lines = _lines(OVERHEAD_LINES_LAYER, lat, lon, LINE_SEARCH_RADIUS_M)
    cables = _lines(UNDERGROUND_CABLES_LAYER, lat, lon, LINE_SEARCH_RADIUS_M)
    committed_stations = _stations(COMMITTED_STATIONS_LAYER, lat, lon, STATION_SEARCH_RADIUS_M)
    committed_lines = _lines(COMMITTED_OVERHEAD_LAYER, lat, lon, LINE_SEARCH_RADIUS_M) + \
        _lines(COMMITTED_UNDERGROUND_LAYER, lat, lon, LINE_SEARCH_RADIUS_M)

    nearest_station = stations[0] if stations else None
    # A separate, tighter-radius query (rather than reusing `lines`/`cables`
    # above) — ArcGIS's own distance filter already does the real
    # geometry-to-point test, so re-querying at LINE_CROSSING_THRESHOLD_M
    # is the simplest way to ask "is one within crossing distance" without
    # a real point-to-polyline distance calculation of our own.
    likely_crossing = bool(
        point_query(OVERHEAD_LINES_LAYER, lon, lat, distance_m=LINE_CROSSING_THRESHOLD_M, result_record_count=1)
        or point_query(UNDERGROUND_CABLES_LAYER, lon, lat, distance_m=LINE_CROSSING_THRESHOLD_M, result_record_count=1)
    )

    severity = "warn" if likely_crossing else "ok"

    log.info(
        "-> Nearest station: %s (%s, %sm); %d line(s), %d cable(s) within %dm; likely crossing: %s",
        nearest_station["name"] if nearest_station else "none within radius",
        nearest_station["voltage"] if nearest_station else "",
        nearest_station["distance_m"] if nearest_station else "",
        len(lines), len(cables), LINE_SEARCH_RADIUS_M, likely_crossing,
    )

    return {
        "stations": stations,
        "nearest_station": nearest_station,
        "station_search_radius_m": STATION_SEARCH_RADIUS_M,
        "lines": lines,
        "cables": cables,
        "line_search_radius_m": LINE_SEARCH_RADIUS_M,
        "likely_crossing": likely_crossing,
        "committed_stations": committed_stations,
        "committed_lines": committed_lines,
        "severity": severity,
        "source": "EirGrid Transmission Development Plan (TDP) public web map",
        "caveat": (
            "This is the national transmission network (110kV+) only — not the local low/medium "
            "voltage distribution network that actually connects most buildings, which ESB Networks "
            "doesn't publish as open data (see the Utilities section above). A nearby substation is "
            "an indicative grid-connection feasibility signal, not a guarantee of available capacity — "
            "confirm directly with EirGrid/ESB Networks for an actual connection application. A line "
            "or cable within a site is a wayleave/easement matter for a site-specific survey."
        ),
    }
