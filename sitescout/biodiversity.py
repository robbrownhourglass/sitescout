"""Species occurrence records — National Biodiversity Data Centre (NBDC)
data, accessed via GBIF (the Global Biodiversity Information Facility)
rather than NBDC's own map viewer.

NBDC's own interactive map (maps.biodiversityireland.ie) is a live,
custom-built ArcGIS JS app, but its species-occurrence endpoint
(`POST /api/services/app/visualisationService/GetStandardSpeciesVisualisation`)
takes a filter object whose shape is only assembled at runtime in the
browser (Esri Accessor subclasses with no statically-declared properties —
confirmed by reading its compiled TypeScript DTOs directly). Investigating
further surfaced a real, unrelated bug on NBDC's own server: its taxon
*search* endpoint (`GetTaxonsQuery`) matches correctly but then crashes
server-side with an unhandled Entity Framework "self referencing loop
detected" serialization error — a bug on their end, not a request-shape
problem solvable by guessing harder.

NBDC (like most national biodiversity centres) publishes its own records
into GBIF, which has a clean, properly documented, genuinely public REST
API (api.gbif.org, no auth) supporting the exact same "radius around a
point" pattern already used everywhere else in this app
(`geoDistance=lat,lon,radius`). Confirmed live: 3,292 occurrence records
within 2km of a test point near Navan, Co. Meath, including real IUCN Red
List conservation flags (European Eel — Critically Endangered; Rabbit —
Endangered; Horse-chestnut — Vulnerable).

`establishmentMeans` (native/introduced/invasive) was checked and found
essentially unpopulated for Irish-tagged GBIF records (0 nationally for
INTRODUCED or INVASIVE) — not reliable enough to report on, so this module
doesn't attempt an "invasive species" flag. Dropped rather than shipped
with misleadingly-always-empty data.
"""
from __future__ import annotations

import logging

import requests

from . import config

log = logging.getLogger("sitescout.biodiversity")

SEARCH_URL = "https://api.gbif.org/v1/occurrence/search"
SEARCH_RADIUS_KM = 2  # matches ecology.py's 2km NPWS designated-area search radius

# IUCN Red List categories treated as a genuine conservation-concern flag.
# Deliberately excludes NT (Near Threatened — a softer signal), DD (Data
# Deficient — not actually a threat assessment), and LC (Least Concern —
# the vast majority of records, not a flag at all).
THREATENED_CATEGORIES = ["VU", "EN", "CR"]
THREATENED_LABELS = {"VU": "Vulnerable", "EN": "Endangered", "CR": "Critically Endangered"}


def _get(params: dict | list[tuple[str, str]]) -> dict:
    """`requests` accepts either a dict or a list of (key, value) tuples for
    `params` — the list form is needed for `iucnRedListCategory`, which
    GBIF only treats as OR when passed as separate repeated params, not a
    single comma-separated value (tested: the latter returned 0 results).
    """
    resp = requests.get(SEARCH_URL, params=params, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_species_records(lat: float, lon: float) -> dict:
    geo = f"{lat},{lon},{SEARCH_RADIUS_KM}km"

    log.info("Querying GBIF occurrence records within %dkm…", SEARCH_RADIUS_KM)
    total_data = _get({"geoDistance": geo, "country": "IE", "limit": 0})
    total_count = total_data.get("count", 0)

    log.info("Querying GBIF for threatened species (IUCN VU/EN/CR) within %dkm…", SEARCH_RADIUS_KM)
    threatened_params = [
        ("geoDistance", geo), ("country", "IE"), ("hasCoordinate", "true"), ("limit", 50),
    ] + [("iucnRedListCategory", c) for c in THREATENED_CATEGORIES]
    threatened_data = _get(threatened_params)

    # Dedupe by species — GBIF returns individual occurrence *events*, so
    # the same species can appear many times (repeat sightings); keep the
    # most severe category and most recent sighting per species.
    by_species: dict[str, dict] = {}
    severity_rank = {"CR": 3, "EN": 2, "VU": 1}
    for r in threatened_data.get("results", []):
        species = r.get("species") or r.get("scientificName")
        if not species:
            continue
        category = r.get("iucnRedListCategory")
        existing = by_species.get(species)
        if existing is None or severity_rank.get(category, 0) > severity_rank.get(existing["category"], 0):
            by_species[species] = {
                "species": species,
                "common_name": r.get("vernacularName"),
                "category": category,
                "category_label": THREATENED_LABELS.get(category, category),
                "last_observed": r.get("eventDate"),
                "lat": r.get("decimalLatitude"),
                "lon": r.get("decimalLongitude"),
            }

    threatened_species = sorted(
        by_species.values(),
        key=lambda s: severity_rank.get(s["category"], 0),
        reverse=True,
    )

    log.info(
        "-> %d total occurrence record(s), %d distinct threatened species within %dkm",
        total_count, len(threatened_species), SEARCH_RADIUS_KM,
    )
    for s in threatened_species[:6]:
        log.info("   - %s (%s) — %s", s["species"], s["common_name"], s["category_label"])

    severity = "warn" if threatened_species else "ok"

    return {
        "total_record_count": total_count,
        "threatened_species": threatened_species,
        "threatened_count": len(threatened_species),
        "search_radius_km": SEARCH_RADIUS_KM,
        "severity": severity,
        "source": "GBIF.org (Global Biodiversity Information Facility) — includes NBDC-published Irish records",
        "caveat": (
            "Occurrence records reflect where species have been recorded, not a systematic survey — "
            "absence of a record doesn't mean a species isn't present, and presence doesn't mean it's "
            "still there today. A threatened-species record nearby is a due-diligence flag for a "
            "professional ecological assessment, not a determination in itself. IUCN Red List "
            "categories are global/regional assessments, not site-specific protection status."
        ),
    }
