"""Environmental hazards — Environmental Protection Agency (EPA) data:
radon risk, historic/closed landfills, and licensed IPPC/IED industrial
facilities. All three come off EPA's own public GeoServer (gis.epa.ie),
found the same way as floodinfo.ie's flood maps — pulling apart the "EPA
Maps" viewer's own JS bundle (app-bundle.js) for its GeoServer base URL,
then confirmed live via its WMS `GetCapabilities` (which lists every layer
this GeoServer serves — a huge national environmental dataset, most of it
out of scope here; see CLAUDE.md for what else is in there).

Uses WFS (not WMS `GetFeatureInfo` like wms.py/floodinfo.ie) — GeoServer's
WFS `GetFeature` returns real vector features directly in WGS84
(`srsName=EPSG:4326`), no Web Mercator reprojection needed.

Gotcha, confirmed by testing (don't re-derive): the `bbox` filter
parameter MUST carry an explicit CRS suffix
(`bbox=minx,miny,maxx,maxy,EPSG:4326`). Without it, GeoServer silently
interprets the bbox numbers in the layer's native storage CRS (Irish
Transverse Mercator, EPSG:2157) instead of WGS84 degrees — a bbox sized in
degrees is then a nonsensically tiny sliver in ITM metres, so the query
just returns zero features. No error, no warning — reads exactly like
"nothing nearby," confirmed by querying a real landfill's own centroid and
still getting zero results until the CRS suffix was added.

Radon specifically closes an item that had been link-out only since this
project started (see CLAUDE.md's "Not yet wired in" list).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import requests

from . import config

log = logging.getLogger("sitescout.epa")

WFS_URL = "https://gis.epa.ie/geoserver/wfs"

RADON_LAYER = "EPA:RadonRiskMapofIreland"
LANDFILLS_LAYER = "EPA:ClosedLandfills_2023"
IPPC_LAYER = "EPA:IPPC_LicFacilities"

RADON_SEARCH_HALF_M = 100         # one classification per area; a small buffer reliably hits the polygon at the point
LANDFILLS_SEARCH_RADIUS_M = 1000  # landfills can have a wider zone of influence than their mapped boundary
IPPC_SEARCH_RADIUS_M = 1000


def _bbox_around(lat: float, lon: float, half_m: float) -> str:
    dlat = half_m / 111_000
    dlon = half_m / (111_000 * math.cos(math.radians(lat)))
    return f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat},EPSG:4326"


def _wfs_query(type_name: str, lat: float, lon: float, half_m: float, max_features: int = 25) -> dict:
    """Returns the raw parsed WFS response (not just `features`) so callers
    can check `numberMatched` vs `numberReturned` — same "exactly N found"
    vs "N found, capped, more exist" distinction as arcgis.point_query_full().
    """
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeNames": type_name,
        "bbox": _bbox_around(lat, lon, half_m),
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": max_features,
    }
    resp = requests.get(WFS_URL, params=params, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "features" not in data:
        raise RuntimeError(f"EPA WFS error for {type_name}: {data}")
    return data


def _polygon_ring_sets(geometry: Optional[dict]) -> list:
    """A GeoJSON Polygon/MultiPolygon's `coordinates` is already a list of
    ring-sets for MultiPolygon (one per part); wrap a plain Polygon's
    single ring-set in a list too, so callers always get the same shape —
    matching cadastral.py's `polygon_ring_sets_wgs84` convention.
    """
    if not geometry:
        return []
    if geometry.get("type") == "MultiPolygon":
        return geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        return [geometry.get("coordinates", [])]
    return []


def _first_point(geometry: Optional[dict]) -> tuple:
    if not geometry:
        return None, None
    coords = geometry.get("coordinates") or []
    if geometry.get("type") == "MultiPoint" and coords:
        lon, lat = coords[0]
        return lat, lon
    if geometry.get("type") == "Point" and coords:
        lon, lat = coords
        return lat, lon
    return None, None


def get_radon_risk(lat: float, lon: float) -> dict:
    log.info("Querying EPA Radon Risk Map…")
    feats = _wfs_query(RADON_LAYER, lat, lon, RADON_SEARCH_HALF_M, max_features=1)["features"]
    if not feats:
        log.info("-> No radon classification returned at this exact point")
        return {
            "found": False,
            "source": "EPA Radon Risk Map",
            "note": "No radon classification returned at this exact point.",
        }
    props = feats[0]["properties"]
    log.info("-> %s", props.get("Risk"))
    return {
        "found": True,
        "risk_description": props.get("Risk"),
        "more_info_url": props.get("URL"),
        "source": "EPA Radon Risk Map",
    }


def get_closed_landfills(lat: float, lon: float) -> dict:
    log.info("Querying EPA closed/historic landfills within %dm…", LANDFILLS_SEARCH_RADIUS_M)
    data = _wfs_query(LANDFILLS_LAYER, lat, lon, LANDFILLS_SEARCH_RADIUS_M)
    landfills = [
        {
            "name": f["properties"].get("Name and Location of Facility"),
            "registration_code": f["properties"].get("Registration Code"),
            "status": f["properties"].get("Certificate of Authorisation Status"),
            "operated": f["properties"].get("Estimated Dates of Operation"),
            "tonnage": f["properties"].get("Estimated Landfilled Tonnage (T)"),
            "polygon_ring_sets_wgs84": _polygon_ring_sets(f.get("geometry")),
        }
        for f in data["features"]
    ]
    more_exist = data.get("numberMatched", 0) > data.get("numberReturned", 0)
    log.info(
        "-> %d closed landfill(s) within %dm%s",
        len(landfills), LANDFILLS_SEARCH_RADIUS_M, " (capped, more exist)" if more_exist else "",
    )
    for lf in landfills[:6]:
        log.info("   - %s (%s)", lf["name"], lf["operated"])
    return {
        "landfill_count": len(landfills),
        "more_exist": more_exist,
        "landfills": landfills,
        "source": "EPA Historic (Closed) Landfills Register",
        "caveat": "Proximity to a former landfill is a contamination/ground-stability due-diligence "
                  "flag, not a determination in itself — a site-specific ground investigation would "
                  "confirm actual risk.",
    }


def get_ippc_facilities(lat: float, lon: float) -> dict:
    log.info("Querying EPA licensed IPPC/IED facilities within %dm…", IPPC_SEARCH_RADIUS_M)
    data = _wfs_query(IPPC_LAYER, lat, lon, IPPC_SEARCH_RADIUS_M)
    facilities = []
    for f in data["features"]:
        p = f["properties"]
        lat_, lon_ = _first_point(f.get("geometry"))
        facilities.append({
            "name": p.get("Name"),
            "address": p.get("Address"),
            "category": p.get("Category"),
            "licence_status": p.get("LicenceStatusType"),
            "licence_number": p.get("ActiveLicenceNumber"),
            "lat": lat_,
            "lon": lon_,
        })
    more_exist = data.get("numberMatched", 0) > data.get("numberReturned", 0)
    log.info(
        "-> %d licensed facility(ies) within %dm%s",
        len(facilities), IPPC_SEARCH_RADIUS_M, " (capped, more exist)" if more_exist else "",
    )
    return {
        "facility_count": len(facilities),
        "more_exist": more_exist,
        "facilities": facilities,
        "source": "EPA Licensed IPPC/IED Facilities Register",
    }


def get_environmental_hazards(lat: float, lon: float) -> dict:
    """One combined section — radon + closed landfills + licensed
    industrial facilities — mirroring ecology.py's pattern of bundling
    several related EPA/NPWS layers behind one report section/sidebar
    tile rather than three separate ones.
    """
    return {
        "radon": get_radon_risk(lat, lon),
        "landfills": get_closed_landfills(lat, lon),
        "ippc": get_ippc_facilities(lat, lon),
        "source": "Environmental Protection Agency (EPA), gis.epa.ie",
    }
