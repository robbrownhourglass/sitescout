"""Eircode / address resolution via the Autoaddress 3.0 API.

Docs: https://docs.autoaddress.com

Important, verified: the Lookup response here gives a clean postal address
and the Eircode, but NEVER coordinates. Their GetData endpoint can return
coordinates (dataType "location" / "ie_location") but is gated to backend
servers with an IP allow-listed in the Autoaddress Account Centre — we
confirmed this with a direct 401 test (both with the raw key and with a
properly-issued Bearer token from /createtoken). See CLAUDE.md for the full
writeup. `get_location_data()` below is provided so that once real backend
access is arranged, coordinates can be wired in with a one-line change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

from . import config

log = logging.getLogger("sitescout.autoaddress")

BASE_URL = "https://api.autoaddress.com/3.0"


@dataclass
class ResolvedAddress:
    address_text: str
    eircode: Optional[str]
    raw: dict


def _headers() -> dict:
    return {"User-Agent": config.USER_AGENT}


def _pick_option(options: list[dict]) -> dict:
    """Prompt the user in the terminal to disambiguate when Autoaddress
    returns more than one match. This is the CLI equivalent of the picker
    UI in the old HTML demo, and is much simpler to get right server-side.
    """
    print("\nMultiple matches found — pick one:")
    for i, opt in enumerate(options, start=1):
        suffix = f"  ({opt.get('suffix')})" if opt.get("suffix") else ""
        print(f"  [{i}] {opt.get('value')}{suffix}")
    while True:
        choice = input(f"Enter a number (1-{len(options)}): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        print("Not a valid choice, try again.")


def search(query: str) -> dict:
    """Raw Autoaddress `/search` call. Returns the parsed JSON response
    (an `options` list — Autoaddress's Search endpoint never returns a
    finished `lookup` directly, only options to drill into).

    This is the non-interactive primitive shared by the CLI's `resolve()`
    (below) and the web UI (`sitescout/webapp.py`), which needs to hand
    disambiguation options to the browser instead of blocking on `input()`.
    """
    log.info('Searching Autoaddress for "%s"…', query)
    url = f"{BASE_URL}/search"
    params = {"address": query, "key": config.AUTOADDRESS_KEY, "limit": 8}
    resp = requests.get(url, params=params, headers=_headers(), timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def follow(href: str) -> ResolvedAddress | dict:
    """Follows one Autoaddress HATEOAS link (search/drilldown/lookup all
    share this response shape).

    Returns a `ResolvedAddress` once a `lookup` is reached. If the link
    instead leads to *another* disambiguation step (a drilldown with more
    than one option), returns `{"options": [...]}` and leaves the choice to
    the caller — the CLI's `resolve()` prompts on stdin; the web UI sends
    the options to the browser and waits for a follow-up request.
    """
    log.debug("Following Autoaddress link: %s", href)
    resp = requests.get(href, headers=_headers(), timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    if data.get("type") == "lookup" and data.get("address"):
        addr = data["address"]
        lines = [l["value"] for l in addr.get("lines", []) if l.get("value")]
        parts = lines + [
            addr.get("city", {}).get("value"),
            addr.get("region", {}).get("value"),
            addr.get("postcode", {}).get("value"),
        ]
        address_text = ", ".join(p for p in parts if p)
        eircode = addr.get("postcode", {}).get("value") or None
        log.info("Resolved address: %s", address_text)
        return ResolvedAddress(address_text=address_text, eircode=eircode, raw=data)

    options = data.get("options") or []
    if not options:
        raise RuntimeError("Unexpected Autoaddress response (no lookup, no options)")

    if len(options) == 1 and options[0].get("link", {}).get("rel") == "lookup":
        return follow(options[0]["link"]["href"])

    return {"options": options}


def _resolve_options(options: list[dict]) -> ResolvedAddress:
    """CLI-only: prompts on stdin until a `ResolvedAddress` is reached,
    recursing through nested drilldowns if Autoaddress returns one.
    """
    chosen = _pick_option(options)
    link = chosen.get("link") or {}
    if not link.get("href"):
        raise RuntimeError("Chosen option has no link to follow")
    result = follow(link["href"])
    if isinstance(result, ResolvedAddress):
        return result
    return _resolve_options(result["options"])


def resolve(query: str) -> ResolvedAddress:
    """Resolve an Eircode or free-text address to a full postal address +
    Eircode via Autoaddress's Search -> (Drilldown |Lookup) chain, prompting
    on stdin if there's more than one match. This is the CLI entry point;
    the web UI uses `search()` / `follow()` directly instead, since it can't
    block on `input()`.
    """
    log.info('Resolving "%s" via Autoaddress…', query)
    data = search(query)

    options = data.get("options") or []
    if not options:
        raise RuntimeError(f'No Autoaddress match for "{query}"')

    if len(options) == 1 and options[0].get("link", {}).get("rel") == "lookup":
        result = follow(options[0]["link"]["href"])
        if isinstance(result, ResolvedAddress):
            return result
        return _resolve_options(result["options"])

    return _resolve_options(options)


def get_location_data(address_id: str, token: str) -> dict:
    """Calls GetData with dataTypes=location,ie_location for true per-Eircode
    coordinates. NOT currently usable — confirmed to return 401 without a
    backend server whose IP is allow-listed in the Autoaddress Account
    Centre. Kept here, unused, as the documented "correct" path for when
    that access is arranged. See CLAUDE.md.
    """
    url = f"{BASE_URL}/getdata"
    params = {"addressId": address_id, "dataTypes": "location,ie_location"}
    resp = requests.get(
        url,
        params=params,
        headers={**_headers(), "Authorization": f"Bearer {token}"},
        timeout=config.HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()
