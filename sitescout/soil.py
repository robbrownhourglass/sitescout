"""Pedological soil survey — the Irish Soil Information System (ISIS),
a genuinely different dataset from gsi.py's "subsoil": GSI's Quaternary
Sediments layer describes the geological drift (till, sand/gravel, peat)
between true soil and bedrock, whereas ISIS is a real, field-sampled soil
map (drainage class, texture, depth, soil organic carbon) — the kind of
thing a planner or agronomist would actually mean by "soil".

Found while answering a direct user question about what "subsoil" on the
Geology & subsoil card actually meant, and whether real published soil
samples exist for Ireland. They do: ISIS is a 2007-2013 EPA STRIVE +
Teagasc project, built on An Foras Talúntais's original 1950s-1990s
national soil survey (~44% of the country with real field profile
descriptions), enhanced with 246 newly-sampled profile pits (2012-2013)
and digital soil mapping to produce a 1:250,000-scale, 213-soil-series
national map. Published CC BY 4.0 (confirmed via its EPA GeoNetwork
metadata record).

Found on the SAME EPA GeoServer (gis.epa.ie/geoserver) epa.py and
water_quality.py already use — its GetCapabilities lists four soil layers
(`EPA:SOILS_NationalSoils` — an older 1980s-era classification sharing
GSI's own "TDSs"-style codes with gsi.py's subsoil layer;
`EPA:SOILS_WETDRY` — a simplified drainage-only layer; `EPA:Soil_subsoils_ie`
— a subsoil-texture layer; `EPA:SOIL_SISNationalSoils` — the ISIS map
itself). `SOIL_SISNationalSoils` is used here: it's the only one of the
four carrying drainage, texture, depth, AND soil organic carbon together.

Gotcha, confirmed by testing (don't repeat this mistake): a small
query bbox matters more here than for water_quality.py's polygons.
`wfs_query(..., half_m=100+)` around a point can genuinely intersect
several different soil polygons at once (this map has much finer spatial
detail than a WFD water body), and GeoServer's `count=1` just returns
whichever one it finds first — NOT necessarily the one containing the
point. Confirmed live: a 1km-wide bbox at three different real test
points all "matched" the same nearby River Alluvium polygon regardless of
what was actually under the point. Fixed by using a tight half_m=50
bbox (same as gsi.py's own point queries effectively are, just via a
different query primitive) — confirmed this correctly resolves distinct,
plausible soil types per point (River alluvium at Fermoy, Brown Earth in
the Wicklow uplands, Urban/soil-concreted-over in built-up areas).
"""
from __future__ import annotations

import logging

from .wfs import wfs_query

log = logging.getLogger("sitescout.soil")

SOIL_LAYER = "EPA:SOIL_SISNationalSoils"

# Tight on purpose — see module docstring's gotcha note. Wide enough to
# tolerate the point sitting a few metres inside a polygon edge, nowhere
# near wide enough to spill into a neighbouring soil association.
QUERY_HALF_M = 50


def get_soil_survey(lat: float, lon: float) -> dict:
    log.info("Querying EPA/Teagasc Irish Soil Information System (ISIS)…")
    feats = wfs_query(SOIL_LAYER, lat, lon, QUERY_HALF_M, max_features=1)["features"]
    if not feats:
        log.info("-> No ISIS soil polygon at this exact point")
        return {
            "found": False,
            "source": "Irish Soil Information System (ISIS) — EPA/Teagasc",
        }

    p = feats[0]["properties"]
    plain_english = p.get("PlainEnglish")
    log.info("-> Soil: %s (drainage: %s)", plain_english, p.get("Drainage"))

    return {
        "found": True,
        "association_name": p.get("Association_Name"),
        "association_unit": p.get("Association_Unit"),
        "soil_type": plain_english,
        "texture_substrate": p.get("Texture_Substrate_Type"),
        "drainage": p.get("Drainage"),
        "texture": p.get("Texture"),
        "depth_cm": p.get("Depth"),
        "soil_organic_carbon_t_ha": p.get("SOC"),
        "info_url": p.get("URL"),
        "source": "Irish Soil Information System (ISIS) — EPA/Teagasc, gis.epa.ie",
        "caveat": (
            "Mapped at 1:250,000 (soil association level, ~213 series nationally) — a real, "
            "field-sampled national soil survey (246 profile pits sampled 2012-2013, building on "
            "An Foras Talúntais's earlier 1950s-1990s survey work), but not a site-specific soil "
            "sample. For anything load-bearing (foundation design, on-site wastewater treatment "
            "system percolation) a real trial hole / percolation test on this site is still needed — "
            "this tells you what to expect, not a substitute for testing it."
        ),
    }
