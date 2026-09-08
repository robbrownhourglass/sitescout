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
  ecology.py                NPWS designated areas (SAC/SPA/NHA/pNHA) — one national dataset
  epa.py                    environmental hazards (EPA): radon risk, closed landfills, licensed
                             IPPC/IED facilities, historic/abandoned mine sites — all off EPA's
                             own GeoServer via WFS, not ArcGIS/WMS like everything else (see
                             verified-sources table)
  geohazards.py             landslide susceptibility, bedrock/sand-gravel aquifer classification,
                             karst features, and groundwater source protection areas — all off
                             the same GSI ArcGIS server gsi.py already uses, just further-out
                             service folders (Geohazards, more of Groundwater) on that same server
  local_authority.py        resolves a point to its city/county council (Tailte Éireann boundaries)
  rps.py                    RPS/ACA (statutory protected structures/conservation areas) —
                             per-local-authority, only 4 of 31 wired in so far; see rps.SOURCES
  cadastral.py              property boundary (Tailte Éireann cadastral parcels) — a single-point
                             lookup (get_boundary, used by the CLI) and a within-radius one
                             (get_nearby_parcels, used by the web UI's plot picker), plus
                             summarise_selected_parcels() to merge whatever the user picks
  utilities.py              drafts ESB/Uisce Éireann data-request emails (no open API exists)
  planning.py               planning applications (National Planning Application Database,
                             radius search + a bonus exact-Eircode match) and flood risk (OPW
                             CFRAM via wms.py), live; zoning stays a link-out (radon moved to
                             epa.py — see below, it's live now too, don't re-add it here)
  pipeline.py               shared section logic: SECTION_SPECS / run_section() runs one named
                             section at a time (used by the web UI's per-section endpoint);
                             run() runs all of them via a ThreadPoolExecutor for the CLI's
                             one-shot report ("boundary" is deliberately not a SECTION_SPEC — the
                             web UI gets it from the plot picker, not a fresh point query)
  report.py                 compiles everything, prints to terminal, optional JSON/MD save
  cli.py                    argparse wiring; the only entry point that lets Autoaddress
                             prompt on stdin (autoaddress.resolve()) when a query is ambiguous
  webapp.py                 Flask app for the local web UI; can't block on stdin, so it uses
                             autoaddress.search()/follow() directly and round-trips
                             disambiguation options to the browser instead (see its docstring)
  templates/index.html      the web UI page (adapted from ireland-site-scout-demo.html, but
                             talks only to this app's own /api/scout(/choose|/section/<name>))
```

Flow (CLI): `autoaddress.resolve()` → `geocode.geocode()` →
`pipeline.run()` (every section concurrently via `ThreadPoolExecutor`,
`boundary` from a single-point `cadastral.get_boundary()`) →
`report.build_report()`.

Flow (web UI) — deliberately different, to hide backend latency behind the
plot-confirmation step rather than making the user wait for one big
response:
1. `POST /api/scout` (or `/choose`, for disambiguation) resolves the point
   and returns quickly: `location` + `cadastral.get_nearby_parcels()`
   (every parcel within 150m, flagging which one the point actually falls
   in) + the list of section names to fetch next. No section data yet.
2. The browser shows the plot picker (map, click to select/merge nearby
   parcels — one property is often several registered parcels) and, at
   the same time, fires `GET /api/scout/section/<name>` once per section
   name, all in parallel — verified at 3.76s wall-clock for all 11
   sections against the live server (was ~19s sequential before this
   split), so in practice most/all of them land before the user's done
   clicking. Each arriving section ticks its sidebar tile from a pending
   (grey, pulsing) dot to its real status.
3. "Confirm N plots" computes the boundary section client-side from
   whatever got selected (`cadastral.summarise_selected_parcels()`'s JS
   port in the template — no extra round-trip, the picker already has
   full parcel geometry) and reveals it as the last tile to complete.

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
| Planning applications | National Planning Application Database `FeatureServer` (`IrishPlanningApplications_FVLayer`) — 500m radius search plus a bonus exact-Eircode match via `arcgis.attribute_query()` | `planning.py` |
| Flood risk (fluvial/coastal) | OPW CFRAM predictive flood-extent maps, GeoServer WMS `GetFeatureInfo` on floodinfo.ie's own server | `planning.py` + `wms.py` |
| Ecology (SAC/SPA/NHA/pNHA) | NPWS `NPWSDesignatedAreas` `FeatureServer` (4 layers, one national dataset — unlike RPS/ACA, which are per-local-authority) | `ecology.py` |
| Local authority (which council a point is in) | Tailte Éireann `Administrative_Areas___OSi_National_Statutory_Boundaries` `FeatureServer` | `local_authority.py` |
| RPS / ACA (4 of 31 local authorities) | Per-authority ArcGIS `FeatureServer`s — South Dublin, Wicklow, Fingal (ACA only), Cork City (RPS only); routing table in `rps.SOURCES` | `rps.py` |
| Environmental hazards (radon, closed landfills, licensed IPPC/IED facilities, historic mine sites) | EPA's own GeoServer (`gis.epa.ie/geoserver`), queried via WFS `GetFeature` (not WMS `GetFeatureInfo` like `wms.py`) | `epa.py` |
| Landslide susceptibility | GSI `IE_GSI_Landslide_Susceptibility_Classification_50K_IE26_ITM` (national coverage, point-in-polygon) | `geohazards.py` |
| Aquifer classification (bedrock + sand/gravel) | GSI `IE_GSI_Aquifer_Datasets_IE26_ITM` (layer 2 = bedrock aquifer, national coverage; layer 0 = sand/gravel, only some areas) | `geohazards.py` |
| Karst features (springs, caves, turloughs, swallow holes) | GSI `IE_GSI_Karst_Datasets_40K_IE32_ITM` (layer 0) — radius search; text list only, see note below on why there's no map overlay | `geohazards.py` |
| Groundwater source protection (public water supply + group water scheme) | GSI `IE_GSI_Group_Water_Scheme_Public_Water_Supply_Source_Protection_Areas_20K_IE26_ITM` (layer 0 = SPAs, layer 1 = zones of contribution) | `geohazards.py` |

Radon risk zones are drawn as a real map overlay (dashed, low-opacity
polygon), not just a text readout — but the raw polygons are large enough
(one tested at ~48km x 36km, 5,597 boundary points) that `epa.py`
simplifies before sending to the browser: `_simplify_ring()` decimates to
at most 400 points, and `_polygon_ring_sets(..., simplify=True)` also
drops interior holes, keeping only the largest ring per part. Tipperary
Landfill coords: 5,597 → 416 points. This is a real (disclosed) shape
simplification, not full accuracy — good for "roughly where does this zone
end" at site-scouting zoom levels, not a survey-grade boundary. Closed
landfills and IPPC facilities are small enough not to need this.

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
7. EPA's "EPA Maps" viewer (gis.epa.ie/EPAMaps) is, like floodinfo.ie, a
   self-hosted GeoServer behind a custom app rather than Esri — its own
   `app-bundle.js` references relative `/geoserver/wms` and `/geoserver/gwc/...`
   paths, confirming the base URL is `gis.epa.ie/geoserver`. Its
   `GetCapabilities` lists a huge national environmental dataset (air
   quality, bathing water, mines, WFD water body status, etc.) — `epa.py`
   uses four layers so far (radon, closed landfills, licensed IPPC
   facilities, historic/abandoned mine sites — `MINES_SiteLocation` +
   `MINES_SiteBoundaries`), found by reading the layer names off that
   capabilities list. Used its WFS `GetFeature` (not WMS `GetFeatureInfo`)
   since it returns real vector features directly in WGS84 with
   `srsName=EPSG:4326` — but the `bbox` filter param needs an explicit CRS
   suffix (`bbox=minx,miny,maxx,maxy,EPSG:4326`) or GeoServer silently
   interprets the numbers in the layer's native storage CRS (Irish
   Transverse Mercator) instead, and the query just returns zero features
   — no error, reads exactly like "nothing nearby." Confirmed by testing
   against a real landfill's own centroid and still getting zero results
   until the suffix was added. WFD water body status (river/lake/coastal
   ecological status) is a large, still-unused category on this same
   capabilities list — a candidate for a future integration.
8. `geohazards.py`'s four layers (landslide susceptibility, aquifer
   classification, karst features, groundwater source protection areas)
   needed none of the above reverse-engineering — gsi.geodata.gov.ie's own
   ArcGIS REST catalogue lists every service folder directly at
   `GET .../server/rest/services?f=json`, no WebAppViewer needed. That
   listing surfaced two folders gsi.py wasn't already using (`Geohazards`,
   plus more of `Groundwater` than just vulnerability) — each service's own
   `?f=json` then lists its layers and field schemas directly. Landslide
   susceptibility classes and aquifer categories were confirmed against the
   layers' full value domains via an ArcGIS `returnDistinctValues=true`
   query (not assumed from a legend). **Karst features have no map overlay
   even though `geohazards.py` reports them** — confirmed the
   `IE_GSI_Karst_Datasets_40K_IE32_ITM` layer never returns geometry via
   its query endpoint (`returnGeometry=true`, a plain `where=1=1`, and
   `f=geojson` all still came back with `geometry: null`), despite its own
   schema advertising `esriGeometryPoint`. The raw `X_ITM`/`Y_ITM`
   attribute fields do carry real coordinates, but they're Irish Transverse
   Mercator (EPSG:2157) — this app has no geospatial/reprojection library,
   and hand-rolling that transform without a verified reference would risk
   silently wrong pins, so karst features are reported as a text list only
   (type + name), not drawn as points. Don't "fix" this by adding lat/lon
   fields back without also adding and testing a real ITM→WGS84 transform.

`Irish_Master_Data_Source_Register_Site_Scout_v2.xlsx` (repo root) is a
working register of further candidate sources (data.gov.ie, local-authority
RPS/ACA, funding schemes, historical records, etc.) — use it before
re-researching what else might be integrable.

**A silent-failure gotcha, found twice already — check for it whenever a
new ArcGIS layer is wired in:** every URL passed to `arcgis.py`'s
`point_query`/`point_query_full`/`attribute_query` must end in `/query`.
Hit the bare layer URL (e.g. `.../FeatureServer/0`) instead, and ArcGIS
doesn't error — it silently returns the layer's own metadata/schema JSON,
which has no `features` key, so `.get("features", [])` quietly becomes an
empty list. That reads exactly like a legitimate "nothing found here"
result, especially at rural test sites where zero real results is
genuinely plausible — which is exactly how `planning.py`'s
`PLANNING_APPLICATIONS_URL` went unnoticed missing `/query` for this
project's entire history until a routine audit caught it (see git log).
`ecology.py` had the same bug in an earlier draft, caught before it ever
shipped. When adding a new source: construct the URL, then actually query
it with a broad filter (`where=1=1` or a known-good point) and confirm a
real `features` array comes back — don't just check for a 200 and no
Python exception.

Not yet wired in / unresolved:

- **Zoning designation itself** (as opposed to planning application
  history) — not in the NPAD dataset. Ireland's ~31 local authorities each
  publish their own zoning maps; no single national layer was found.
  `myplan_zoning` in `planning.get_planning_links()` stays a link-out.
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

- [ ] **The shared `AUTOADDRESS_KEY` in `.env.example` started returning
      401 Unauthorized as of 8 Sept 2026** (confirmed: same query worked
      earlier in this project's history, now fails at the `/3.0/search`
      step regardless of query). Likely expired/revoked on Autoaddress's
      side, not a code regression — the app degrades rather than breaks
      (falls back to geocoding the raw input directly, per `cli.py`/
      `webapp.py`'s existing except-branches), but real address
      disambiguation won't work until a fresh key is obtained.
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
