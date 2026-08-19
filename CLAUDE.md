# Ireland Site Scout — project context

Read this before making changes. It captures a lot of hard-won findings from
a long debugging session in a browser-based prototype that this Python app
replaced — don't re-derive them from scratch.

## What this is

A tool for the site-scouting stage of a property/development project in
Ireland: given an address or Eircode, pull together the categories a
planner, PM, or architect would otherwise gather by hand — geology &
subsoil, water table, archaeology & heritage, property boundary, utilities,
and planning context — from Ireland's public data sources, into one report.

Two front ends over the same Python pipeline: a CLI
(`python main.py "<eircode or address>"`) and a local Flask web UI
(`python -m sitescout.webapp`). Started as a single-file HTML/JS browser
demo; moved to Python specifically because browser-side failures (especially
around Google's Geocoder) were silent and very hard to diagnose, whereas a
server-side script prints exactly what it's doing and what came back at
every step. `ireland-site-scout-demo.html` in the repo root is that original
prototype, kept for reference only — it calls Autoaddress/Google/ArcGIS
directly from client JS with embedded API keys, which is the exact class of
problem the Python version exists to avoid. The Flask UI (`sitescout/webapp.py`
+ `sitescout/templates/index.html`) reuses that demo's visual design but the
browser only ever talks to this app's own `/api/scout` endpoint; every
external call still happens server-side in Python, fully logged, same as
the CLI.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate   # or your preferred env manager
pip install -r requirements.txt
cp .env.example .env      # then edit .env with real keys — see "Credentials" below
python main.py "R32 E4F8"
python main.py "Trim Castle, Trim, Co. Meath" --save   # writes output/*.json and *.md
```

## Architecture

```
main.py                    CLI entry point, delegates to sitescout/cli.py
ireland-site-scout-demo.html   original browser-only prototype — reference only, don't open/serve directly
sitescout/
  config.py                env-based config + logging setup
  autoaddress.py            Eircode/address -> full postal address (Autoaddress API)
  geocode.py                address/Eircode -> lat/lon (Google, falls back to Nominatim)
  arcgis.py                 shared helper for querying Esri ArcGIS REST layers by point
  wms.py                    shared helper for OPW's flood-map GeoServer (WMS GetFeatureInfo,
                             Web Mercator <-> WGS84 reprojection) — see planning.get_flood_risk()
  gsi.py                    geology (bedrock, subsoil) + groundwater vulnerability
  heritage.py               archaeology (SMR), SMR Zones (notification zones), NIAH (protected
                             structures) — all National Monuments Service ArcGIS layers
  cadastral.py              property boundary (Tailte Éireann cadastral parcels)
  utilities.py              drafts ESB/Uisce Éireann data-request emails (no open API exists)
  planning.py               planning applications (National Planning Application Database) and
                             flood risk (OPW CFRAM via wms.py), live; zoning + radon stay link-outs
  pipeline.py               shared orchestration (cadastral/gsi/heritage/utilities/planning -> report),
                             used by both cli.py and webapp.py so they can't drift apart
  report.py                 compiles everything, prints to terminal, optional JSON/MD save
  cli.py                    argparse wiring; the only entry point that lets Autoaddress
                             prompt on stdin (autoaddress.resolve()) when a query is ambiguous
  webapp.py                 Flask app for the local web UI; can't block on stdin, so it uses
                             autoaddress.search()/follow() directly and round-trips
                             disambiguation options to the browser instead (see its docstring)
  templates/index.html      the web UI page (adapted from ireland-site-scout-demo.html, but
                             talks only to this app's own /api/scout, /api/scout/choose)
```

Flow: `autoaddress.resolve()` (CLI) or `autoaddress.search()`/`follow()`
(web) → `geocode.geocode()` → `pipeline.run()` (`cadastral`, `gsi` x2,
`heritage`, `utilities`, `planning`) → `report.build_report()`.

## Credentials

Both keys in `.env.example` are **temporary test keys** shared during the
demo phase — replace with real ones before relying on this for real work,
and never commit `.env` (it's gitignored).

- `AUTOADDRESS_KEY` — from [Autoaddress](https://docs.autoaddress.com), a
  `pub_...` public key usable in the query string (`?key=...`) or exchanged
  for a short-lived Bearer token via `/3.0/createtoken`. Used for Search →
  Lookup only in this app.
- `GOOGLE_MAPS_API_KEY` — must have **Geocoding API** enabled on its Google
  Cloud project (Console → APIs & Services → Library → "Geocoding API" →
  Enable). This is a *different* product from "Maps JavaScript API" — the
  browser-demo phase burned real time on this exact confusion (see below).

## Deployment (Railway)

Deployed via `Procfile` + `requirements.txt` (gunicorn included) — see
README.md's "Deploy (Railway)" section for the actual steps. Two things
that only matter for the deployed path, not local dev:

- `webapp.py`'s `if __name__ == "__main__": app.run(debug=True, ...)` block
  is dev-only — gunicorn imports the module-level `app` object directly and
  never executes that block, so `debug=True` (which would be a real
  security issue if it ran in production — Werkzeug's debugger allows
  arbitrary code execution from an error page) never applies on Railway.
- Railway injects `$PORT`; the Procfile binds to it
  (`--bind 0.0.0.0:$PORT`). Don't hardcode a port anywhere in the gunicorn
  start command.

## The Eircode coordinate-precision saga (important — read before "fixing" this again)

This was the single biggest source of confusion across the whole project.
Summary of what's **confirmed true**, so it doesn't get re-investigated:

1. **Autoaddress's Lookup response never includes coordinates.** It only
   returns a postal address (lines, city, region, postcode/Eircode). Confirmed
   by direct inspection of the API response.

2. **Autoaddress's `GetData` endpoint *can* return real per-Eircode
   coordinates** (`dataTypes=location` or `ie_location`, the latter with
   OSI mapping/orthophoto-level accuracy). But it's gated to backend servers
   with an **IP address allow-listed in the Autoaddress Account Centre**.
   Confirmed with a direct 401, both passing the raw `key` and a properly
   issued Bearer token from `/createtoken`. `autoaddress.get_location_data()`
   is written and ready to use — it just doesn't work until that backend
   access is arranged with Autoaddress support.

3. **Neither Google's Geocoding API nor OpenStreetMap Nominatim resolve
   Eircodes to true per-building coordinates.** This was proven directly,
   not assumed: three different Eircodes for three different units in the
   same building (`D09 V2R3`, `D09 FY51`, `D09 HW84` — Dublin Airport
   Business Park, Swords Road, Dublin 9) were geocoded via Google's real
   REST API and **all three returned the identical coordinate**
   (53.4073652, -6.2381539), with `location_type: "APPROXIMATE"` and
   address `types: ["postal_code"]`. Google treats an Eircode as a postal
   code lookup, not a unique-building identifier. A single rural Eircode
   (`R32 E4F8` → Knockanina, Co. Laois) *looks* precise on both Google Maps
   consumer site and the API, but only because that postal-code area
   happens to contain just one building — coincidence of low density, not
   real per-Eircode resolution. This is reproducible any time by running
   the CLI against those three business-park Eircodes and diffing the
   coordinates.

4. **The only sources that store one real coordinate per Eircode** are:
   - The licensed **Eircode Address Database (ECAD)**, sold directly by
     Eircode/An Post/GeoDirectory. Not free. Published 2015 pricing (treat
     as indicative, confirm current rates via sales@eircode.ie) was roughly:
     ECAF (address+Eircode, prerequisite) from €60/user; ECAD (adds
     coordinates/boundaries) adds an annual access fee (€500–1,000) plus
     €120–180/user or fractions of a cent per transaction.
   - Autoaddress's gated `GetData`/`ie_location` (see #2), which is itself
     built on GeoDirectory/ECAD-derived data.

5. **The official finder.eircode.ie site is built on Autoaddress too.**
   Confirmed by pulling apart its own JS bundle — it calls
   `api.autoaddress.com/3.0` directly, plus a private backend at
   `api-finder3.eircode.ie` (not documented, not for third-party use — don't
   try to reverse-engineer/call this) which almost certainly does the
   privileged server-side `GetData` call for the precise pin.

**Bottom line:** `geocode.geocode()` in this app is honest about this — it
always flags non-`ROOFTOP` results as approximate (`precise=False`), and in
practice every Eircode-only query will be approximate until #2 or #4 above
is resolved. Don't try to "fix" this by swapping geocoding providers again;
it's not a provider problem, it's a data-licensing problem.

### Two real Google/browser bugs found and fixed along the way (for reference)

These were genuine bugs in the old HTML demo, not the precision issue above
— worth knowing about since the same mistakes are easy to reintroduce:

- Passing a hard `componentRestrictions: {country: 'IE'}` filter to
  `google.maps.Geocoder` (in addition to `region: 'ie'` bias) caused
  **zero results** client-side even though the identical query worked fine
  over plain REST with just `region=ie`. This codebase only ever uses the
  region-bias form (see `geocode.py`).
- Google's Maps *JavaScript* API can fail silently in a browser — neither
  calling the success callback nor `script.onerror` — for reasons it only
  reports via `console.error` or a `gm_authFailure` global hook. This class
  of bug is the main reason this project moved to a server-side Python
  script calling the plain REST `Geocoding API` directly: no browser, no
  silent failures, every HTTP call and its response is logged as it happens.

## Verified data sources & endpoints

All of the below were tested live (not just found in docs) during this
project. Full URLs are in the relevant module — this is a quick index.

| Category | Source | Module |
|---|---|---|
| Eircode/address resolution | Autoaddress Search/Lookup API | `autoaddress.py` |
| Coordinates | Google Geocoding API (fallback: Nominatim) | `geocode.py` |
| Bedrock geology | GSI `Bedrock_Geology_Datasets_100K` (layer 3 — layer 0 is structural symbols, not the polygon geology) | `gsi.py` |
| Subsoil | GSI `Quaternary_Sediments_50K` | `gsi.py` |
| Groundwater vulnerability | GSI `Groundwater_Vulnerability_40K` | `gsi.py` |
| Archaeology (SMR) | National Monuments Service SMR `FeatureServer` (public ArcGIS Online, CORS-open) | `heritage.py` |
| SMR Zones | National Monuments Service `SMRZone` `FeatureServer`, same ArcGIS org as SMR above | `heritage.py` |
| Protected structures (NIAH) | `NIAHBuildings` `FeatureServer`, same ArcGIS org as SMR above | `heritage.py` |
| Property boundary | Tailte Éireann `Cadastral_Parcels_Freehold` (layer 12) / `Cadastral_Parcels_Leasehold` (layer 13) | `cadastral.py` |
| Planning applications | National Planning Application Database `FeatureServer` (`IrishPlanningApplications_FVLayer`) | `planning.py` |
| Flood risk (fluvial/coastal) | OPW CFRAM predictive flood-extent maps, GeoServer WMS `GetFeatureInfo` on floodinfo.ie's own server | `planning.py` + `wms.py` |
| Ecology (SAC/SPA/NHA/pNHA) | NPWS `NPWSDesignatedAreas` `FeatureServer` (4 layers, one national dataset — unlike RPS/ACA, which are per-local-authority) | `ecology.py` |
| Local authority (which council a point is in) | Tailte Éireann `Administrative_Areas___OSi_National_Statutory_Boundaries` `FeatureServer` | `local_authority.py` |
| RPS / ACA (4 of 31 local authorities) | Per-authority ArcGIS `FeatureServer`s — South Dublin, Wicklow, Fingal (ACA only), Cork City (RPS only); routing table in `rps.SOURCES` | `rps.py` |

**How the second batch above (SMR Zones, NIAH, planning applications, flood
risk, ecology) was found** — same "pull the JS apart" technique as the
finder.eircode.ie writeup in item 5 of the coordinate-precision saga, this
time against public ArcGIS Online **WebAppViewer** apps and one OpenLayers
app, rather than the documented-but-dead endpoints:

1. `GET https://www.arcgis.com/sharing/rest/content/items/<appId>/data?f=json`
   on the WebAppViewer app's id (from its `?id=...` URL) returns the app
   config, including `map.itemId` — the underlying web map.
2. `GET .../items/<mapItemId>/data?f=json` on that returns
   `operationalLayers`, each with a real `url` — a `FeatureServer`/`MapServer`
   layer, queryable exactly like the rest of this app (`arcgis.py`).
3. Confirmed the NMS "Historic Environment Viewer"
   (`heritagedata.maps.arcgis.com/apps/webappviewer/index.html?id=0c9eb9575b544081b0d296436d8f60f8`)
   this way has `SMRZone` and `NIAHBuildings` layers on the *same* ArcGIS org
   (`services-eu1.arcgis.com/HyjXgkV6KGMSF3jt/...`) as the SMR layer already
   in use — a different, working NIAH endpoint from the dead
   `webservices.npws.ie` one below.
4. Same technique on myplan.ie found an embedded "LIVE-NPAD WAB" app whose
   web map has an `IrishPlanningApplications_FVLayer` `FeatureServer` with
   full per-application detail (status, decision, dates, appeals).
5. floodinfo.ie's map viewer isn't Esri at all — it's a custom OpenLayers
   app. Its page source has an inline `<script>` block (not the linked `.js`
   files) defining `ol.source.TileWMS` layers pointing at
   `/geoserver/wms` on floodinfo.ie's own domain — a self-hosted GeoServer.
   Confirmed live with a `GetFeatureInfo` request (WMS's point-query verb —
   different shape from ArcGIS: a small bounding box + a pixel, not lon/lat
   directly) for layer `esds_floodmaps:ext_f_c_0100` (fluvial, 1% AEP) at
   Fermoy, Co. Cork, which returned a real flood-extent polygon. Also
   confirmed GeoServer returns each feature's **full** geometry regardless
   of query bbox size, and that its native SRS (`EPSG:900913`/Web Mercator)
   needs reprojecting to WGS84 for Leaflet — see `wms.py`.
6. Same technique on npws.ie found the "NPWS Designations Viewer" WebAppViewer
   (linked from npws.ie/protected-sites), whose web map has all four
   ecological-designation layers (SAC/SPA/NHA/pNHA) on one `NPWSDesignatedAreas`
   `FeatureServer` — a single national dataset, unlike RPS/ACA below.

`Irish_Master_Data_Source_Register_Site_Scout_v2.xlsx` (repo root) is a
working register of further candidate sources (data.gov.ie, local-authority
RPS/ACA, funding schemes, historical records, etc.) — use it before
re-researching what else might be integrable.

Not yet wired in / unresolved:

- **Zoning designation itself** (as opposed to planning application
  history) — not in the NPAD dataset. Ireland's ~31 local authorities each
  publish their own zoning maps; no single national layer was found.
  `myplan_zoning` in `planning.get_planning_links()` stays a link-out.
- **EPA radon risk map** — link-out only; no query endpoint sought yet.
- **Utilities** (ESB Networks electricity, Uisce Éireann water/wastewater) —
  confirmed these are genuinely not open data (security-sensitive
  underground infrastructure). `utilities.py` drafts the actual request
  emails rather than pretending to have live data.
- Flood risk only queries the **current-climate** CFRAM layers (3 fluvial +
  3 coastal probability bands). Future-scenario and depth-grid layers exist
  on the same GeoServer (see `floodmap.js` on floodinfo.ie) but aren't
  queried — would be straightforward to add via `wms.py` if needed.
- **RPS & ACA — done, but only for 4 of 31 local authorities**
  (`rps.py` + `local_authority.py`). The spreadsheet's "GREEN" rows turned
  out to overstate readiness once actually checked: several are a static
  GeoJSON *file* download (not a live query endpoint — would need fetch +
  cache + our own point-in-polygon, e.g. Dún Laoghaire-Rathdown RPS, Cork
  City ACA) and Kildare is zip/GeoPackage only (would need a new geospatial
  dependency this app doesn't otherwise have). Only South Dublin (RPS+ACA),
  Wicklow (RPS+ACA), Fingal (ACA), and Cork City (RPS) had a genuine live
  ArcGIS `FeatureServer`, confirmed by actually fetching each dataset page
  and checking what it linked to — don't trust the spreadsheet's status
  column alone, re-verify before adding a new county. `rps.SOURCES` is the
  per-authority routing table; add a new entry there (url + field-mapping
  `extract` lambda) to extend coverage. Meath's data.gov.ie listing also has
  a licensing note that blocks use until resolved — don't add it without
  checking that first.
- Ecological designations (`ecology.py`) only reports the 2km search radius
  as "nearby" — the real Appropriate Assessment screening distance isn't a
  fixed radius and can be larger; treat "not within 2km" as a starting
  point, not a clearance.

## Known limitations / TODO

- [ ] Coordinate precision is area-level only (see saga above) — biggest
      open item. Either pursue Autoaddress backend/IP-allowlist access, or
      license ECAD directly. Note this also affects the new radius-based
      lookups (NIAH 500m, planning applications 300m, SMR 2km) — they're
      centred on the same approximate point as everything else.
- [ ] Zoning designation itself still needs a source (see above).
- [ ] `autoaddress.py`'s CLI disambiguation (`input()` prompt) hasn't been
      tested against a genuinely ambiguous query directly on stdin — but the
      shared `search()`/`follow()` logic it's built on was verified via the
      web UI's non-interactive picker against a real multi-level ambiguous
      query ("Main Street" → 8 options → nested drilldown → final lookup).
- [ ] No test suite yet. Given how many "confirmed by direct API test" facts
      this project depends on, some of those live-endpoint checks would be
      good candidates for a small integration test file, run manually
      (not in CI, since it hits real external services with a shared test key).
