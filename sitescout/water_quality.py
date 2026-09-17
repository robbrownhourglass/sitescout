"""Water Framework Directive (WFD) water body ecological/chemical status —
which groundwater body a site sits above, and which surface water bodies
(river, lake, coastal, transitional) are nearby, each with EPA's official
current status classification (High/Good/Moderate/Poor/Bad).

Off the same EPA GeoServer as epa.py, found the same way (reading layer
names off its WFS GetCapabilities) while investigating that GeoServer for
epa.py's PRTR addition. Kept as its own module rather than folded into
epa.py — this is water body classification, not an environmental hazard
source, and epa.py's "Environmental hazards" card was already carrying
five sub-findings (radon, landfills, IPPC, mines, PRTR).

Each `*_WFD_LatestStatus` layer conveniently carries both geometry AND
status as one query — no need to separately fetch a water body boundary
layer and join it to a status table by ID, which is how these are usually
published elsewhere. Confirmed live: groundwater bodies (point-in-polygon,
national coverage, always found) return values like "Good"; river/lake/
coastal/transitional bodies (radius search, since they're often lines or
scattered polygons rather than something guaranteed to contain the exact
point) show real variation nationally — sampled 200 river water bodies in
a 50km radius around Dublin and got a real spread: 79 Poor, 71 Moderate,
48 Good, 2 High.
"""
from __future__ import annotations

import logging

from .wfs import wfs_query

log = logging.getLogger("sitescout.water_quality")

GROUNDWATER_BODY_LAYER = "EPA:GWB_WFD_LatestStatus"

# (type, layer, search radius) — rivers are lines, searched at a tighter
# radius than lakes/coastal/transitional (polygons, rarer, so a wider net
# makes more sense without returning an unmanageable list).
SURFACE_WATER_LAYERS = [
    ("river", "EPA:RWB_WFD_LatestStatus", 500),
    ("lake", "EPA:LWB_WFD_LatestStatus", 2000),
    ("coastal", "EPA:CWB_WFD_LatestStatus", 2000),
    ("transitional", "EPA:TWB_WFD_LatestStatus", 2000),
]

# EU WFD ecological status classes, best to worst. Groundwater bodies use a
# simpler Good/Poor scale (per the Groundwater Directive) but the same
# ranking works for picking "the worst status found" across both.
STATUS_BADNESS_RANK = {"High": 1, "Good": 2, "Moderate": 3, "Poor": 4, "Bad": 5}


def get_water_body_status(lat: float, lon: float) -> dict:
    log.info("Querying EPA WFD groundwater body status…")
    gw_feats = wfs_query(GROUNDWATER_BODY_LAYER, lat, lon, 100, max_features=1)["features"]
    groundwater_body = None
    if gw_feats:
        p = gw_feats[0]["properties"]
        groundwater_body = {
            "name": p.get("Name"),
            "code": p.get("European_Code"),
            "status": p.get("Overall_GW_Status"),
            "period": p.get("Period_for_WFD_Status"),
            "geometry": gw_feats[0].get("geometry"),
        }
        log.info("-> Groundwater body: %s (%s)", groundwater_body["name"], groundwater_body["status"])
    else:
        log.info("-> No groundwater body classification at this exact point")

    surface_water_bodies = []
    for water_type, layer, half_m in SURFACE_WATER_LAYERS:
        log.info("Querying EPA WFD %s water body status within %dm…", water_type, half_m)
        data = wfs_query(layer, lat, lon, half_m, max_features=5)
        for f in data["features"]:
            p = f["properties"]
            surface_water_bodies.append({
                "type": water_type,
                "name": p.get("Name"),
                "code": p.get("European_Code"),
                "status": p.get("Status"),
                "period": p.get("Period_for_WFD_Status"),
                "geometry": f.get("geometry"),
            })
    log.info("-> %d nearby surface water body(ies)", len(surface_water_bodies))
    for b in surface_water_bodies[:6]:
        log.info("   - %s (%s): %s", b["name"], b["type"], b["status"])

    all_statuses = [b["status"] for b in surface_water_bodies if b.get("status")]
    if groundwater_body and groundwater_body.get("status"):
        all_statuses.append(groundwater_body["status"])
    worst_status = max(all_statuses, key=lambda s: STATUS_BADNESS_RANK.get(s, 0), default=None)
    severity = (
        "bad" if worst_status in ("Poor", "Bad")
        else "warn" if worst_status == "Moderate"
        else "ok" if worst_status
        else "warn"  # no status data at all — unknown, not "clean"
    )

    return {
        "groundwater_body": groundwater_body,
        "surface_water_bodies": surface_water_bodies,
        "surface_water_count": len(surface_water_bodies),
        "worst_status": worst_status,
        "severity": severity,
        "source": "EPA Water Framework Directive (WFD) water body status",
        "caveat": (
            "Status reflects the whole water body (often several km of a river, or a whole lake), "
            "not conditions at this exact point — a Poor or Bad classification is a catchment-level "
            "signal (relevant to any development near or discharging to that water body), not "
            "something specific to this site alone. WFD classifications are reassessed on multi-year "
            "cycles (see each body's own reporting period) — check the EPA's current cycle for the "
            "latest position."
        ),
    }
