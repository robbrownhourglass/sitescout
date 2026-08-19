"""Record of Protected Structures (RPS) & Architectural Conservation Areas
(ACA) — Ireland's actual statutory built-heritage protections.

Important, and easy to get wrong: **NIAH (heritage.py) is not the same as
RPS.** NIAH is the National Inventory of Architectural Heritage — a survey/
rating, not a legal protection in itself. RPS is the real thing: each of
Ireland's 31 local authorities maintains its own statutory Record of
Protected Structures as part of its Development Plan, and a structure can
be on NIAH, RPS, both, or neither.

That "31 local authorities, 31 datasets" part is exactly why this only
covers a handful of councils so far, unlike everything else in this app
(one national dataset each). `Irish_Master_Data_Source_Register_Site_Scout_v2.xlsx`
(repo root) surveyed all 31 — most publish RPS/ACA only as PDF/plan text,
a few as a static downloadable file (GeoJSON, or worse, a zip/GeoPackage
needing a geospatial library this app doesn't otherwise depend on), and
only four had a genuine live, directly-queryable ArcGIS endpoint at
research time (7 Aug 2026) — see SOURCES below. Everywhere else, this
reports "not yet available for this local authority" honestly rather than
guessing or silently omitting the section.

Routing: `local_authority.py` resolves the point to a council name first;
SOURCES is keyed by that exact (all-caps) name.
"""
from __future__ import annotations

import logging

from .arcgis import point_query, point_query_full
from .local_authority import get_local_authority_raw

log = logging.getLogger("sitescout.rps")

RPS_SEARCH_RADIUS_M = 500   # matches heritage.NIAH_SEARCH_RADIUS_M — same kind of thing
ACA_SEARCH_RADIUS_M = 2000  # matches heritage.SEARCH_RADIUS_M / ecology.SEARCH_RADIUS_M

# Per-source field names are genuinely inconsistent (each of the 31
# councils published its own dataset independently) — each `extract`
# function normalises one source's raw attributes into a common shape.
# RPS -> {ref, address, description}. ACA -> {name, description}.

SOURCES = {
    "SOUTH DUBLIN COUNTY COUNCIL": {
        "rps": {
            "url": "https://services1.arcgis.com/PxbTDTskGHCe4sv6/arcgis/rest/services/"
                   "Record_of_Protected_Structures_South_Dublin_County_Development_Plan_2022_to_2028/"
                   "FeatureServer/0/query",
            "out_fields": "RPS_NUMBER,AddressLoc,Descriptio",
            "extract": lambda a: {
                "ref": a.get("RPS_NUMBER"), "address": a.get("AddressLoc"), "description": a.get("Descriptio"),
            },
        },
        "aca": {
            "url": "https://services1.arcgis.com/PxbTDTskGHCe4sv6/arcgis/rest/services/"
                   "Architectural_Conservation_Areas_South_Dublin_County_Development_Plan_2022_to_2028/"
                   "FeatureServer/0/query",
            "out_fields": "FEATURE,LOCATION,REF",
            "extract": lambda a: {
                "name": a.get("FEATURE") or a.get("LOCATION"), "description": a.get("LOCATION"),
            },
        },
    },
    "WICKLOW COUNTY COUNCIL": {
        "rps": {
            "url": "https://services.arcgis.com/hQOfkHGHCu8mgDpG/arcgis/rest/services/"
                   "Protected_Structures_Wicklow_County_CDP_2022_2028/FeatureServer/0/query",
            "out_fields": "RPS_Number,Building_Address,Structure,Description,Plan_Description",
            "extract": lambda a: {
                "ref": a.get("RPS_Number"), "address": a.get("Building_Address") or a.get("Structure"),
                "description": a.get("Description") or a.get("Plan_Description"),
            },
        },
        "aca": {
            "url": "https://services.arcgis.com/hQOfkHGHCu8mgDpG/arcgis/rest/services/"
                   "Architectural_Conservation_Areas_CDP_2022_2028/FeatureServer/0/query",
            "out_fields": "Name_of_Se,Settlement,Descriptio,Plan_Descr",
            "extract": lambda a: {
                "name": a.get("Name_of_Se") or a.get("Settlement"),
                "description": a.get("Descriptio") or a.get("Plan_Descr"),
            },
        },
    },
    "CORK CITY COUNCIL": {
        "rps": {
            "url": "https://services-eu1.arcgis.com/f0ZQOHXBIeLonX0V/arcgis/rest/services/"
                   "ProtectedStructures/FeatureServer/0/query",
            "out_fields": "Street,BuildingName,No,Ref_no,Notes",
            "extract": lambda a: {
                "ref": a.get("Ref_no"),
                "address": ", ".join(p for p in [a.get("No"), a.get("Street")] if p),
                "description": a.get("BuildingName") or a.get("Notes"),
            },
        },
        "aca": None,  # published as a static GeoJSON download, not a live query endpoint — not wired in yet
    },
    "FINGAL COUNTY COUNCIL": {
        "rps": None,  # no live endpoint found — RPS page is web/plan text only
        "aca": {
            "url": "https://services5.arcgis.com/CI1e5PKQXvJgmJK8/arcgis/rest/services/"
                   "FCC_Development_Plan_2023_2029_ACA_Architectural_Conservation_Areas/FeatureServer/0/query",
            "out_fields": "Location,Labels,Obj_Desc",
            "extract": lambda a: {
                "name": a.get("Location") or a.get("Labels"), "description": a.get("Obj_Desc"),
            },
        },
    },
}


def _query_rps(cfg: dict, lat: float, lon: float) -> dict:
    data = point_query_full(
        cfg["url"], lon, lat,
        out_fields=cfg["out_fields"],
        distance_m=RPS_SEARCH_RADIUS_M,
        return_geometry=True,
        result_record_count=25,
    )
    structures = []
    for f in data.get("features", []):
        item = cfg["extract"](f["attributes"])
        geom = f.get("geometry") or {}
        item["lat"] = geom.get("y")
        item["lon"] = geom.get("x")
        structures.append(item)
    return {
        "available": True,
        "structure_count": len(structures),
        "more_exist": bool(data.get("exceededTransferLimit")),
        "structures": structures,
    }


def _query_aca(cfg: dict, lat: float, lon: float) -> dict:
    exact_feats = point_query(cfg["url"], lon, lat, out_fields=cfg["out_fields"])
    in_aca = bool(exact_feats)
    current = cfg["extract"](exact_feats[0]["attributes"]) if exact_feats else None

    data = point_query_full(
        cfg["url"], lon, lat,
        out_fields=cfg["out_fields"],
        distance_m=ACA_SEARCH_RADIUS_M,
        return_geometry=True,
        result_record_count=25,
    )
    areas = []
    for f in data.get("features", []):
        item = cfg["extract"](f["attributes"])
        item["polygon_rings_wgs84"] = f.get("geometry", {}).get("rings")
        item["contains_site"] = in_aca and item.get("name") == (current or {}).get("name")
        areas.append(item)

    return {
        "available": True,
        "in_aca": in_aca,
        "current": current,
        "area_count": len(areas),
        "more_exist": bool(data.get("exceededTransferLimit")),
        "areas": areas,
    }


def get_protected_structures(lat: float, lon: float) -> dict:
    authority_raw = get_local_authority_raw(lat, lon)
    authority = authority_raw.title() if authority_raw else None
    source = SOURCES.get(authority_raw) if authority_raw else None

    if not source:
        log.info("-> RPS/ACA not covered for %s", authority or "unknown local authority")
        return {
            "covered": False,
            "authority": authority,
            "rps": None,
            "aca": None,
            "note": (
                f"RPS/ACA not yet wired in for {authority} — only South Dublin, Wicklow, Fingal, and "
                "Cork City have a live queryable source so far. Check the local authority's own "
                "Development Plan directly."
            ) if authority else "Could not determine local authority for this point.",
        }

    log.info("Querying RPS/ACA for %s…", authority)
    rps = _query_rps(source["rps"], lat, lon) if source.get("rps") else {"available": False}
    aca = _query_aca(source["aca"], lat, lon) if source.get("aca") else {"available": False}

    if rps.get("available"):
        log.info("-> %d RPS structure(s) within %dm", rps["structure_count"], RPS_SEARCH_RADIUS_M)
    if aca.get("available"):
        log.info("-> %s ACA; %d area(s) mapped within %dm", "within an" if aca["in_aca"] else "not within an", aca["area_count"], ACA_SEARCH_RADIUS_M)

    return {
        "covered": True,
        "authority": authority,
        "rps": rps,
        "aca": aca,
        "source": f"{authority} — Record of Protected Structures / Architectural Conservation Areas",
        "note": "NIAH (a separate section of this report) is a heritage survey, not the same as RPS — a "
                "structure can be on either, both, or neither.",
    }
