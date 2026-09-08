"""Local web UI for Ireland Site Scout.

A thin Flask wrapper around the exact same pipeline the CLI uses
(autoaddress -> geocode -> pipeline.run) — see `cli.py` / `pipeline.py`.
It does NOT reimplement the old browser-only prototype's approach
(`ireland-site-scout-demo.html`, kept in the repo root for reference): that
version called Autoaddress, Google Maps JS, and ArcGIS directly from
client-side JS, with API keys embedded in the page. This is exactly the
class of problem CLAUDE.md documents as the reason this project moved
server-side — silent Google Maps JS failures, and a public-facing page
shipping API keys. Here, the browser only ever talks to this Flask app;
every external HTTP call still happens in Python, still fully logged.

Run:
    python -m sitescout.webapp
Then open http://127.0.0.1:5000

Note on Autoaddress disambiguation: the CLI's `autoaddress.resolve()`
blocks on an `input()` prompt when a query has multiple matches. A web
request can't do that, so this app uses the lower-level `autoaddress.search()`
/ `autoaddress.follow()` primitives instead and, when there's more than one
match, returns the options to the browser (`{"status": "choose", ...}`) for
the user to pick from — then continues via `/api/scout/choose`. This is the
server-side equivalent of the old demo's `<div id="picker">` UI.

Note on the two-stage flow: unlike the CLI (which returns one full report
in a single pipeline.run() call), `/api/scout` here returns quickly with
just the location and nearby cadastral parcels — the frontend shows a
plot-confirmation step with that (the user picks/merges whichever
parcel(s) actually make up the site, since one property is often several
registered parcels), and fires off `/api/scout/section/<name>` once per
section in parallel in the background at the same time, so most of the
real data is already in by the time the user's done picking. Property
boundary itself isn't one of those sections — it's computed client-side
from whatever the user confirms (cadastral.summarise_selected_parcels()).
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

from . import autoaddress, cadastral, config, geocode, pipeline, report

log = config.setup_logging(verbose=False)

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/scout")
def api_scout():
    if not config.AUTOADDRESS_KEY:
        return _error("AUTOADDRESS_KEY is not set — copy .env.example to .env and fill it in.", 500)

    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return _error("query is required", 400)

    try:
        data = autoaddress.search(query)
        options = data.get("options") or []
    except Exception as exc:
        log.warning('Autoaddress search failed for "%s" (%s) — geocoding raw input directly.', query, exc)
        resolved = autoaddress.ResolvedAddress(address_text=query, eircode=None, raw={})
        return _run_from_resolved(query, resolved)

    if not options:
        return _error(f'No Autoaddress match for "{query}"', 404)

    if len(options) == 1 and options[0].get("link", {}).get("rel") == "lookup":
        result = autoaddress.follow(options[0]["link"]["href"])
        return _handle_follow_result(query, result)

    return jsonify({"status": "choose", "options": _slim_options(options)})


@app.post("/api/scout/choose")
def api_scout_choose():
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    href = body.get("href")
    if not href:
        return _error("href is required", 400)

    try:
        result = autoaddress.follow(href)
    except Exception as exc:
        return _error(f"Could not follow Autoaddress link: {exc}", 502)

    return _handle_follow_result(query, result)


def _handle_follow_result(query: str, result):
    """`autoaddress.follow()` returns either a finished `ResolvedAddress`
    or `{"options": [...]}` for another round of disambiguation.
    """
    if isinstance(result, autoaddress.ResolvedAddress):
        return _run_from_resolved(query, result)
    return jsonify({"status": "choose", "options": _slim_options(result["options"])})


def _run_from_resolved(query: str, resolved: autoaddress.ResolvedAddress):
    try:
        geo = geocode.geocode(resolved.eircode, resolved.address_text)
    except Exception as exc:
        return _error(f"Geocoding failed: {exc}", 502)

    try:
        parcels = cadastral.get_nearby_parcels(geo.lat, geo.lon)
    except Exception as exc:
        log.error("Nearby parcels lookup failed: %s", exc)
        parcels = []

    return jsonify({
        "status": "ok",
        "query": query,
        "resolved_address": resolved.address_text,
        "eircode": resolved.eircode,
        "location": report.location_dict(geo),
        "parcels": parcels,
        "section_names": list(pipeline.SECTION_NAMES),
    })


@app.get("/api/scout/section/<name>")
def api_scout_section(name: str):
    """Fetches one section for a point already resolved by /api/scout —
    called once per name in pipeline.SECTION_NAMES, in parallel, while the
    user is on the plot-confirmation step (see module docstring).
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError, TypeError):
        return _error("lat and lon query params are required", 400)
    eircode = request.args.get("eircode") or None
    label = request.args.get("label") or None

    try:
        data = pipeline.run_section(name, lat, lon, eircode, label)
    except ValueError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        log.error("Section '%s' lookup failed: %s", name, exc)
        return _error(f"{name} lookup failed: {exc}", 502)

    return jsonify({"status": "ok", "section": name, "data": data})


def _slim_options(options: list[dict]) -> list[dict]:
    """Strips Autoaddress options down to what the picker UI needs, and
    drops any option with no follow-up link (nothing the UI could do with
    it anyway).
    """
    slim = []
    for o in options:
        href = (o.get("link") or {}).get("href")
        if not href:
            continue
        slim.append({"value": o.get("value"), "suffix": o.get("suffix"), "href": href})
    return slim


def _error(message: str, status: int):
    return jsonify({"status": "error", "message": message}), status


if __name__ == "__main__":
    if not config.AUTOADDRESS_KEY:
        log.error("AUTOADDRESS_KEY is not set — copy .env.example to .env and fill it in")
    if not config.GOOGLE_MAPS_API_KEY:
        log.warning("GOOGLE_MAPS_API_KEY is not set — geocoding will fall back to Nominatim only")
    # threaded=True matters here, not just for speed: the frontend fires
    # one request per section in parallel (see module docstring) — without
    # it Flask's dev server handles them one at a time, defeating the
    # whole point.
    app.run(debug=True, port=5000, threaded=True)
