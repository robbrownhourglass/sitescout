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
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, render_template, request

from . import autoaddress, buildings, cadastral, config, elevation, geocode, historical_maps, pipeline, report

log = config.setup_logging(verbose=False)

app = Flask(__name__)


@app.get("/")
def index():
    # historical_layers passed straight from historical_maps.HISTORICAL_LAYERS
    # (single source of truth — the frontend's own base-layer picker builds
    # its "time travel" list directly from this, rather than a second
    # hand-kept-in-sync copy), trimmed to just what the picker needs
    # (key/label/kind, not the internal base_url etc.). A LIST, not a
    # dict — Flask's own JSON encoder sorts dict keys by default, which
    # would silently alphabetise the deliberate newest-to-oldest ordering
    # already documented on HISTORICAL_LAYERS itself (confirmed live: it
    # did exactly that before this fix, "1995" ended up first).
    historical_layers = [
        {"key": key, "label": v["label"], "kind": v["kind"]}
        for key, v in historical_maps.HISTORICAL_LAYERS.items()
    ]
    return render_template("index.html", historical_layers=historical_layers)


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


@app.get("/api/terrain-image")
def api_terrain_image():
    """Renders the LIDAR DTM tile covering (lat, lon) as a real per-pixel
    elevation image (see elevation.render_dtm_image()) — called lazily by
    the map's "Precise terrain (LIDAR)" overlay only when a user actually
    toggles it on, same as the contour tile layer never
    hitting this app's own server at all (it calls GSI's ArcGIS `export`
    operation directly). This is the one image-producing endpoint in the
    app; every other route returns JSON.
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError, TypeError):
        return _error("lat and lon query params are required", 400)

    try:
        result = elevation.render_dtm_image(lat, lon)
    except Exception as exc:
        log.error("Terrain image render failed: %s", exc)
        return _error(f"terrain image render failed: {exc}", 502)

    if not result:
        return _error("no precise LIDAR coverage at this point", 404)

    return Response(result["png_bytes"], mimetype="image/png")


@app.post("/api/terrain-mesh")
def api_terrain_mesh():
    """A real triangle mesh (vertices + faces) of the plot's own real LIDAR
    elevation, exactly clipped to its boundary shape (not a bounding
    rectangle, not a blocky grid staircase — see elevation.get_terrain_mesh()'s
    module comment) plus nearby OSM buildings and roads extruded and
    grounded on that same mesh (buildings.get_nearby_features()) — for the
    /terrain-3d page's rotatable 3D rendering. POST, not GET, since the
    boundary (the confirmed plot selection's own ring geometry) is the
    actual query, not a couple of scalar params like everywhere else.

    The LIDAR mesh build (normally well under a second, tiles permitting)
    and the OSM features fetch (the public Overpass API — confirmed live
    to occasionally take 10s+ per attempt under load, see buildings.py's
    own docstring) run CONCURRENTLY here, same ThreadPoolExecutor pattern
    pipeline.py already uses for report sections — a real user report of
    this page taking ~40s to load traced back to these two running
    sequentially, with Overpass's latency sitting entirely on top of the
    mesh build instead of overlapping it.
    """
    body = request.get_json(silent=True) or {}
    ring_sets = body.get("polygon_ring_sets_wgs84")
    if not ring_sets:
        return _error("polygon_ring_sets_wgs84 is required", 400)

    center = elevation.mesh_center_and_radius(ring_sets)

    with ThreadPoolExecutor(max_workers=2) as pool:
        mesh_future = pool.submit(elevation.get_terrain_mesh, ring_sets)
        features_future = pool.submit(buildings.get_nearby_features, center[1], center[0], center[2]) if center else None

        try:
            result = mesh_future.result()
        except Exception as exc:
            log.error("Terrain mesh build failed: %s", exc)
            return _error(f"terrain mesh build failed: {exc}", 502)

        if not result:
            return _error("no precise LIDAR coverage for this plot boundary", 404)

        if features_future:
            try:
                features = features_future.result()
                elevation.attach_features(result, features.get("buildings"), features.get("roads"))
            except Exception as exc:
                log.warning("OSM building/road lookup failed (continuing without them): %s", exc)

    result.pop("_raw", None)  # internal-only mosaic pieces — never serialize these to the client
    return jsonify({"status": "ok", "data": result})


@app.post("/api/terrain-flow")
def api_terrain_flow():
    """Water flow analysis for the plot's own real terrain (see
    elevation.get_flow_analysis()) — real local minima (where water pools
    and stays) plus a transparent overlay texture showing the drainage
    network that feeds them, for the /terrain-3d page's own optional
    "water flow" toggle. A separate, lazily-fetched endpoint (like
    /api/terrain-image's map layer) rather than folded into
    /api/terrain-mesh's own response — this is a genuinely optional
    analysis layer, not something every 3D-view visit needs to pay for.
    """
    body = request.get_json(silent=True) or {}
    ring_sets = body.get("polygon_ring_sets_wgs84")
    if not ring_sets:
        return _error("polygon_ring_sets_wgs84 is required", 400)

    try:
        result = elevation.get_flow_analysis(ring_sets)
    except Exception as exc:
        log.error("Flow analysis failed: %s", exc)
        return _error(f"flow analysis failed: {exc}", 502)

    if not result:
        return _error("no precise LIDAR coverage for this plot boundary", 404)

    return jsonify({"status": "ok", "data": result})


@app.post("/api/terrain-satellite")
def api_terrain_satellite():
    """Real Esri World Imagery satellite/aerial imagery, resampled into the
    exact same local mesh-coordinate frame /api/terrain-mesh already
    returned and masked to the plot boundary only (see
    elevation.get_satellite_overlay()) — for the /terrain-3d page's own
    "Show satellite imagery" toggle, asked for directly. Takes
    `origin_lon`/`origin_lat`/`grid_extent` back from the frontend (exactly
    what /api/terrain-mesh returned) rather than recomputing them, so this
    overlay is guaranteed pixel-aligned with the already-rendered mesh
    instead of risking two independently-derived frames drifting apart.
    A separate, lazily-fetched endpoint (like /api/terrain-flow) rather
    than folded into /api/terrain-mesh's own response — real tile
    downloads from an external service, not something every 3D-view visit
    needs to pay for.
    """
    body = request.get_json(silent=True) or {}
    ring_sets = body.get("polygon_ring_sets_wgs84")
    origin_lon, origin_lat, grid_extent = body.get("origin_lon"), body.get("origin_lat"), body.get("grid_extent")
    if not ring_sets or origin_lon is None or origin_lat is None or not grid_extent:
        return _error("polygon_ring_sets_wgs84, origin_lon, origin_lat, and grid_extent are required", 400)

    try:
        result = elevation.get_satellite_overlay(ring_sets, origin_lon, origin_lat, grid_extent)
    except Exception as exc:
        log.error("Satellite overlay failed: %s", exc)
        return _error(f"satellite overlay failed: {exc}", 502)

    if not result:
        return _error("could not build a satellite overlay for this plot boundary", 404)

    return jsonify({"status": "ok", "data": result})


@app.get("/terrain-3d")
def terrain_3d():
    """A standalone page (opened in a new tab from the Terrain & elevation
    card) showing the confirmed plot's own real LIDAR elevation as a
    rotatable 3D surface — literally the site's shape, bent the way the
    real ground is. The boundary itself travels in the URL (as JSON in a
    query param, not session/local storage) so this page is self-
    contained and shareable via its own link, same spirit as every other
    page in this app only ever depending on its own URL, not hidden state.
    """
    return render_template("terrain3d.html")


@app.get("/api/historical-tile/<layer_key>/<int:z>/<int:x>/<int:y>.png")
def api_historical_tile(layer_key: str, z: int, x: int, y: int):
    """One real Web Mercator XYZ tile (what Leaflet actually requests),
    reprojected server-side from Tailte Éireann's own historical MapGenie
    tile caches — see historical_maps.py's own module docstring for why
    this needs real reprojection (not a simple URL rewrite) and for the
    licensing basis for calling their service at all (personal,
    non-commercial use, confirmed directly against GeoHive's own Terms of
    Use, not assumed). A 404 here is a normal, expected "no coverage at
    this exact tile" response, same as any other missing map tile — not
    logged as an error.
    """
    try:
        png_bytes = historical_maps.get_historical_tile(layer_key, z, x, y)
    except Exception as exc:
        log.error("Historical tile fetch failed (%s z=%d x=%d y=%d): %s", layer_key, z, x, y, exc)
        return _error(f"historical tile fetch failed: {exc}", 502)

    if not png_bytes:
        return _error("no coverage at this tile", 404)

    return Response(png_bytes, mimetype="image/png")


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
