"""Geocoding: turn an Eircode or address into coordinates.

IMPORTANT — read this before trusting a pin from this module:

Neither Google nor OpenStreetMap Nominatim resolve Eircodes to true
per-building coordinates. We proved this directly: three different Eircodes
for three different units in the same building (D09 V2R3, D09 FY51, D09
HW84 — Dublin Airport Business Park) all returned the IDENTICAL coordinate
from Google's Geocoding API (53.4073652, -6.2381539), with
location_type "APPROXIMATE" and address type "postal_code" — i.e. Google
geocodes an Eircode as a postcode-area centroid, not a unique building.
Nominatim has the same fundamental limitation.

The only sources that store one coordinate per Eircode are the licensed
Eircode Address Database (ECAD) or Autoaddress's GetData/ie_location
endpoint (gated to allow-listed backend servers — see autoaddress.py).
Until one of those is wired in, treat every result from this module as an
AREA-LEVEL APPROXIMATION, not the exact site. `precise=True` is only ever
set when Google returns location_type "ROOFTOP", which in practice does not
happen for bare Eircodes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

from . import config

log = logging.getLogger("sitescout.geocode")

GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


@dataclass
class GeocodeResult:
    lat: float
    lon: float
    label: str
    source: str
    precise: bool
    location_type: Optional[str] = None


def geocode_google(query: str) -> GeocodeResult:
    if not config.GOOGLE_MAPS_API_KEY:
        raise RuntimeError("GOOGLE_MAPS_API_KEY is not set (check your .env)")
    params = {"address": query, "region": "ie", "key": config.GOOGLE_MAPS_API_KEY}
    resp = requests.get(GOOGLE_URL, params=params, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    status = data.get("status")
    if status != "OK" or not data.get("results"):
        raise RuntimeError(f"Google Geocoding API status: {status} ({data.get('error_message', '')})")
    r = data["results"][0]
    loc = r["geometry"]["location"]
    location_type = r["geometry"].get("location_type")
    if location_type != "ROOFTOP":
        log.warning(
            'Google returned location_type=%s for "%s" — this is an area/postcode '
            "estimate, not the exact building. See module docstring.",
            location_type, query,
        )
    return GeocodeResult(
        lat=loc["lat"], lon=loc["lng"], label=r["formatted_address"],
        source="google", precise=(location_type == "ROOFTOP"), location_type=location_type,
    )


def geocode_nominatim(query: str) -> GeocodeResult:
    params = {"format": "json", "countrycodes": "ie", "limit": 1, "q": query}
    resp = requests.get(
        NOMINATIM_URL, params=params,
        headers={"User-Agent": config.USER_AGENT}, timeout=config.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise RuntimeError(f'No Nominatim match for "{query}"')
    r = data[0]
    return GeocodeResult(
        lat=float(r["lat"]), lon=float(r["lon"]), label=r["display_name"],
        source="nominatim", precise=False,
    )


def geocode(eircode: Optional[str], address_text: str) -> GeocodeResult:
    """Precision order, matching the (hard-won) findings above:
    1. Google on the raw Eircode — keeps the unique identifier intact
    2. Google on the resolved address text
    3. Nominatim on the resolved address text (last resort)

    All three currently top out at area-level precision for Eircodes — this
    ordering is about giving Google's dataset the best chance, not about
    achieving true per-building accuracy, which isn't available from any of
    these sources today. See CLAUDE.md.
    """
    attempts = []
    if eircode:
        attempts.append(("Google (raw Eircode)", lambda: geocode_google(eircode)))
    attempts.append(("Google (resolved address)", lambda: geocode_google(address_text)))
    attempts.append(("Nominatim (resolved address)", lambda: geocode_nominatim(address_text)))

    failures = []
    for label, fn in attempts:
        log.info("Geocoding via %s…", label)
        try:
            result = fn()
            log.info("-> %s: %.6f, %.6f (%s)", label, result.lat, result.lon, result.label)
            if failures:
                log.warning("Earlier attempts failed first: %s", "; ".join(failures))
            return result
        except Exception as exc:  # noqa: BLE001 — we want to try every fallback
            log.warning("-> %s failed: %s", label, exc)
            failures.append(f"{label}: {exc}")

    raise RuntimeError("All geocoding attempts failed: " + " | ".join(failures))
