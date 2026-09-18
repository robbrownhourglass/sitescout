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
  wfs.py                    shared helper for EPA's GeoServer (gis.epa.ie) via WFS GetFeature —
                             extracted from epa.py once water_quality.py needed the same
                             bbox+CRS-suffix query primitive (see its own gotcha note)
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
  water_quality.py          Water Framework Directive (WFD) status — groundwater body + nearby
                             river/lake/coastal/transitional water bodies — off the same EPA
                             GeoServer as epa.py, via wfs.py; its own section/tile, not folded
                             into epa.py's already-crowded environmental hazards card
  biodiversity.py           species occurrence records + IUCN Red List threatened species nearby,
                             via GBIF (not NBDC's own map viewer directly — see its docstring for
                             why: a real bug on NBDC's server, found and worked around, not a
                             request-shape problem)
  local_authority.py        resolves a point to its city/county council (Tailte Éireann boundaries)
  rps.py                    RPS/ACA (statutory protected structures/conservation areas) —
                             per-local-authority, only 4 of 31 wired in so far; see rps.SOURCES
  cadastral.py              property boundary (Tailte Éireann cadastral parcels) — a single-point
                             lookup (get_boundary, used by the CLI) and a within-radius one
                             (get_nearby_parcels, used by the web UI's plot picker), plus
                             summarise_selected_parcels() to merge whatever the user picks
  utilities.py              drafts ESB/Uisce Éireann data-request emails (no open API exists for
                             the local distribution network they operate)
  eirgrid.py                EirGrid's *transmission* grid (110kV+ substations/lines/cables,
                             existing + committed/planned) — distinct from utilities.py's ESB
                             Networks *distribution* network; public because transmission
                             projects require statutory consultation. Merged into the same
                             "utilities" section/tile by pipeline.py, not its own tile.
  elevation.py              precise terrain elevation (OPW LIDAR DTM/DSM, ~2m grid) — the ONE
                             module in this app that downloads and caches its own data
                             (.cache/lidar_tiles/, gitignored) rather than querying a live API for
                             every request, because no live elevation-value API exists (see its
                             own docstring for the full "hillshade isn't elevation" investigation);
                             falls back to national 10m-interval contour lines (EPA Hydrological
                             DTM, live ArcGIS query, no caching needed) where OPW hasn't surveyed.
                             Also renders the real per-pixel DTM as a color-mapped PNG on demand
                             (render_dtm_image(), served by webapp.py's /api/terrain-image route,
                             fetched by the map's imageOverlay only when that layer's toggled on)
                             — every pixel is a genuine LIDAR elevation value, not a hillshade or a
                             point sample. New deps: tifffile, numpy, pyproj, Pillow (see
                             requirements.txt) — the only module using them.
  planning.py               planning applications (National Planning Application Database,
                             radius search + a bonus exact-Eircode match) and flood risk (OPW
                             CFRAM via wms.py — fluvial/coastal/pluvial x current/mid-future/
                             high-future, 21 layers total, queried concurrently), live; zoning
                             stays a link-out (radon moved to epa.py — see below, it's live now
                             too, don't re-add it here)
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
- `elevation.py` caches downloaded LIDAR tiles to `.cache/lidar_tiles/`
  (gitignored) — on Railway this is ephemeral (wiped on redeploy/restart),
  which is fine: it's a pure performance cache, not a data store the app
  depends on, and a cache miss just re-downloads the ~4MB tile. Don't
  "fix" this by adding a persistent volume unless cache-miss latency
  actually becomes a real problem in practice.

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
| Flood risk (fluvial/coastal/pluvial, current + future climate) | OPW CFRAM predictive flood-extent maps, GeoServer WMS `GetFeatureInfo` on floodinfo.ie's own server — 21 layers (3 hazards x up to 3 scenarios x 3 AEP bands), queried concurrently | `planning.py` + `wms.py` |
| Water body status (groundwater + river/lake/coastal/transitional) | EPA WFD `*_WFD_LatestStatus` layers (`GWB_WFD_LatestStatus`, `RWB_WFD_LatestStatus`, etc.) — each carries geometry AND status in one query | `water_quality.py` + `wfs.py` |
| Ecology (SAC/SPA/NHA/pNHA) | NPWS `NPWSDesignatedAreas` `FeatureServer` (4 layers, one national dataset — unlike RPS/ACA, which are per-local-authority) | `ecology.py` |
| Local authority (which council a point is in) | Tailte Éireann `Administrative_Areas___OSi_National_Statutory_Boundaries` `FeatureServer` | `local_authority.py` |
| RPS / ACA (4 of 31 local authorities) | Per-authority ArcGIS `FeatureServer`s — South Dublin, Wicklow, Fingal (ACA only), Cork City (RPS only); routing table in `rps.SOURCES` | `rps.py` |
| Environmental hazards (radon, closed landfills, licensed IPPC/IED facilities, historic mine sites) | EPA's own GeoServer (`gis.epa.ie/geoserver`), queried via WFS `GetFeature` (not WMS `GetFeatureInfo` like `wms.py`) | `epa.py` |
| Major industrial facilities — incl. active mines/quarries (EPA PRTR) | EPA's Pollutant Release and Transfer Register, 9 sector `WFS` layers (`EPA:PRTR_*`) — Ireland's largest per-sector emitters only, searched at 10km (much wider than the other `epa.py` layers — see note below) | `epa.py` |
| Landslide susceptibility | GSI `IE_GSI_Landslide_Susceptibility_Classification_50K_IE26_ITM` (national coverage, point-in-polygon) | `geohazards.py` |
| Aquifer classification (bedrock + sand/gravel) | GSI `IE_GSI_Aquifer_Datasets_IE26_ITM` (layer 2 = bedrock aquifer, national coverage; layer 0 = sand/gravel, only some areas) | `geohazards.py` |
| Karst features (springs, caves, turloughs, swallow holes) | GSI `IE_GSI_Karst_Datasets_40K_IE32_ITM` (layer 0) — radius search; text list only, see note below on why there's no map overlay | `geohazards.py` |
| Groundwater source protection (public water supply + group water scheme) | GSI `IE_GSI_Group_Water_Scheme_Public_Water_Supply_Source_Protection_Areas_20K_IE26_ITM` (layer 0 = SPAs, layer 1 = zones of contribution) | `geohazards.py` |
| Transmission grid (substations, overhead lines, underground cables — existing + committed/planned) | EirGrid's own public "TDP 2024 Web Map PUBLIC" `FeatureServer`, found via ArcGIS Online's public content search rather than a specific viewer | `eirgrid.py` |
| Species occurrence records + IUCN Red List threatened species | GBIF (Global Biodiversity Information Facility) public REST API (`api.gbif.org`), not NBDC's own map viewer — see below | `biodiversity.py` |
| Precise terrain elevation (~2m grid, ground + surface) | OPW's own LIDAR survey tiles (GeoTIFF, real float32 metres) — downloaded + cached on demand, not a live API (none exists — see below) | `elevation.py` |
| National elevation contours (10m interval, 20m grid, fallback) | GSI/EPA "Hydrologically Corrected DTM" contour `MapServer` — attribute-only query, no map geometry (see below) | `elevation.py` |

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

**Two different radii for "industrial facility nearby" — don't collapse
them into one:** `get_ippc_facilities()` (any EPA-licensed premises,
1km) and `get_major_industrial_facilities()` (EPA's PRTR register —
Ireland's biggest per-sector emitters only, 10km) are deliberately
separate lookups at deliberately different radii, both folded into the
same `epa` section. Found this split was necessary after a real gap
report: Boliden Tara Mines (Europe's largest zinc mine, still actively
operating) sits in `IPPC_LicFacilities` with a "Licensed" status, but its
own licensed-facility coordinate is over 4km from Navan town centre —
well outside a 1km (or even 3km) radius. Blowing IPPC_SEARCH_RADIUS_M out
to cover that isn't the right fix — it would flood every town's report
with every small licensed premises (dry cleaners, print shops) within
several km. PRTR is the right list to search wider: it's a EU
emissions-reporting register that only includes facilities crossing a
reporting threshold, confirmed nationally small per sector (16-166
facilities each, 9 sectors) — confirmed live that a 10km radius returns a
sane, cappable count even in genuine industrial clusters (34 near Cork
Harbour, 69 in dense Dublin city centre — both fine capped/paginated like
every other list here). Each PRTR feature carries its own "Main PRTR
Sector" attribute as a ready-made human label (e.g. "Mineral industry" for
Tara Mines, "Chemical industry", "Energy sector", etc.) — used directly
rather than re-deriving a category from the layer name.

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
   uses five layer-groups so far (radon, closed landfills, licensed IPPC
   facilities, historic/abandoned mine sites — `MINES_SiteLocation` +
   `MINES_SiteBoundaries` — and the 9-sector PRTR register), found by
   reading the layer names off that capabilities list. Used its WFS
   `GetFeature` (not WMS `GetFeatureInfo`) since it returns real vector
   features directly in WGS84 with `srsName=EPSG:4326` — but the `bbox`
   filter param needs an explicit CRS suffix
   (`bbox=minx,miny,maxx,maxy,EPSG:4326`) or GeoServer silently interprets
   the numbers in the layer's native storage CRS (Irish Transverse
   Mercator) instead, and the query just returns zero features — no error,
   reads exactly like "nothing nearby." Confirmed by testing against a
   real landfill's own centroid and still getting zero results until the
   suffix was added. The PRTR sector layers (`EPA:PRTR_Mineral_industry`,
   `EPA:PRTR_Chemical_industry`, etc. — see the "Two different radii" note
   above for why they're a separate, wider-radius lookup from
   IPPC_LicFacilities) were found the same way, after `IPPC_LicFacilities`
   alone missed an actively-operating major mine a user reported nearby.
   WFD water body status (river/lake/coastal ecological status) is still a
   large, unused category on this same capabilities list — a candidate for
   a future integration.
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
9. Flood risk's expansion (fluvial/coastal/pluvial x current/mid/high-future)
   needed no new reverse-engineering either — floodinfo.ie's GeoServer
   `GetCapabilities` (`GET /geoserver/wms?service=WMS&version=1.1.1&request=GetCapabilities`)
   lists every layer directly, decodable from the naming convention already
   known from the current-climate layers already in use:
   `ext_{hazard}_{scenario}_{AEP}` (hazard: f/c/p = fluvial/coastal/pluvial;
   scenario: c/m/h = current/mid-future/high-future). Pluvial only exists at
   current climate — no `ext_p_m_*`/`ext_p_h_*` in the capabilities list.
   Confirmed each new layer live before wiring in: mid/high-future fluvial
   hit at Fermoy (a known flood-prone town) even past current-climate's
   extent; pluvial hit in Dublin city centre; coastal high-future hit at
   Cork city centre. Also present on this GeoServer but still unused: the
   `nat_depth_2m_*` raster depth-grid layers (see "Not yet wired in" below).
10. `water_quality.py`'s WFD layers were found while re-checking EPA's
    GeoServer capabilities for the PRTR work (item 7) — a huge family of
    `WFD_*` layers exists (per-cycle historical status, individual
    pressures like agriculture/urban runoff/industry, catchment/basin
    boundaries, bathing water, salmonid waters, shellfish waters, etc. —
    still mostly unused). `*_WFD_LatestStatus` (`GWB_`/`RWB_`/`LWB_`/`CWB_`/
    `TWB_` prefixes for groundwater/river/lake/coastal/transitional) were
    picked specifically because each one already carries both geometry and
    a plain-English current status in a single query — most of the other
    WFD layers need a separate boundary-layer + status-table join by ID,
    which would be real extra work for less payoff. Confirmed real status
    variation live, not just a single value: sampled 200 river water
    bodies within 50km of Dublin and got 79 Poor / 71 Moderate / 48 Good /
    2 High — this isn't a dataset that always says "Good".
11. `eirgrid.py` (EirGrid transmission grid — substations, overhead lines,
    underground cables, existing + committed/planned): found via ArcGIS
    Online's own public content search
    (`GET https://www.arcgis.com/sharing/rest/search?q=eirgrid&f=json`)
    rather than a specific public viewer — turned up "TDP 2024 Web Map
    PUBLIC" (Transmission Development Plan), a `FeatureServer` owned by
    EirGrid's own ArcGIS org and confirmed current (modified 2024).
    Distinct from ESB Networks' *distribution* network (utilities.py,
    confirmed genuinely closed) — EirGrid's *transmission* network
    (110kV+) is public because new transmission infrastructure requires
    statutory public consultation. Cross-checked against a real-world fact
    found earlier in this project: Knockumber substation (110kV) near
    Navan is literally the connection point feeding Boliden Tara Mines
    (epa.py's PRTR addition) — this dataset reflects real, current grid
    topology, not a stale snapshot.
12. **NBDC's own map viewer — investigated, abandoned in favour of GBIF
    (see `biodiversity.py`).** `maps.biodiversityireland.ie` is a real,
    live, custom-built ArcGIS JS API app (not a simple public
    WebAppViewer), backed by an ASP.NET Boilerplate (ABP) service layer.
    Reverse-engineered as far as: its map configuration endpoint
    (`POST /api/services/app/mapConfigurationService/GetMapConfigurations`
    with a JSON array of view names, e.g. `["Terrestrial"]`, found by
    fetching `/api/AbpServiceProxies/GetAll` — the app's own dynamically
    generated proxy script, which spells out every service URL) returns
    real static layers (NHA/pNHA/SAC/SPA `FeatureServer`s at
    `gisserver.biodiversityireland.ie` — but these duplicate what
    `ecology.py` already covers via NPWS's own, simpler endpoint, so no
    new value there). The actual species-occurrence layer is added
    dynamically per-species via `POST /api/services/app/visualisationService/GetStandardSpeciesVisualisation`
    with a `speciesFilter` object — its DTO classes
    (`SpeciesVisualisationFilter`, `SpeciesInfo`, under
    `Scripts/nbdc/MapIndex/Visualisation/Dto/`) are Esri `Accessor`
    subclasses with properties assigned dynamically at runtime, not
    statically declared, so the shape isn't visible in static JS. Pushed
    one step further — its taxon *search* endpoint
    (`POST /api/services/app/taxonService/GetTaxonsQuery`) DOES match real
    species by name regardless of which plausible field name was guessed
    (`query`/`searchTerm`/`text`/`name`/`searchText` all matched
    "Hedgehog" identically) — but the server then crashes on every guess
    with the exact same unhandled error: `"Self referencing loop detected
    with type 'BiodiversityMaps.EntityFramework.Models.Taxon'"`, an
    Entity Framework serialization bug on NBDC's own backend, not a
    request-shape problem. That's what closed this path off, not lack of
    effort — even the right request likely can't get a usable response
    back from this particular service today.
    `biodiversity.py` uses **GBIF** instead (see the verified-sources
    table) — NBDC's own published records flow into GBIF anyway, and
    GBIF's public API is clean, documented, stable, and actually works.
13. GBIF specifics worth not re-deriving: `geoDistance=lat,lon,Rkm` on
    `api.gbif.org/v1/occurrence/search` is the radius-search parameter —
    no auth needed. **Gotcha, confirmed by testing:** `iucnRedListCategory`
    (or presumably any multi-value enum filter) only works as an OR filter
    when passed as **separate repeated query params**
    (`iucnRedListCategory=VU&iucnRedListCategory=EN&iucnRedListCategory=CR`)
    — a single comma-separated value
    (`iucnRedListCategory=VU,EN,CR`) silently returned **zero** results
    despite the facet count on the same bbox showing 11 real matches
    across those three categories. `requests`' list-of-tuples `params`
    form is what `biodiversity.py` uses to send repeated params — a plain
    dict can't represent that. Also checked and found NOT reliable for
    Irish data: `establishmentMeans` (native/introduced/invasive) returns
    0 nationally for `INTRODUCED` or `INVASIVE` — essentially unpopulated
    for GBIF's Irish-tagged records, so `biodiversity.py` doesn't attempt
    an invasive-species flag from it. `iucnRedListCategory`, by contrast,
    is well populated and confirmed varying (VU/EN/CR all found in a
    single 2km-radius test).
14. `elevation.py` — the terrain/elevation investigation, in full, because
    it took several wrong turns before landing on the real answer:
    - Every raster GSI publishes via ArcGIS Online at fine resolution
      (2m/1m/25cm/12.5cm — found via `GET /sharing/rest/search?q=Ireland
      DTM` the same way `eirgrid.py` was found) is titled "Hillshade" —
      and confirmed, not assumed, to actually BE hillshade: queried each
      one's own `?f=json` and got `pixelType: "U8"` (8-bit, 0-255) on
      every single one, then confirmed live with an `/identify` call
      returning `"146"` at a real point — a shading value, not a
      plausible elevation for Ireland (whose terrain routinely exceeds
      255m). No live API returns real elevation numbers at LIDAR
      resolution; only a cosmetic rendering of the data.
    - The real elevation values only exist in OPW's own downloadable
      LIDAR survey tiles — found via the SAME GSI ArcGIS server's `Lidar`
      folder (`GET .../server/rest/services/Lidar?f=json`) as a "coverage
      index": one polygon per 2km x 2km survey tile, with `DATA_URL`,
      `RESOLUTION`, `DATECAPTUR`, and the tile's own ITM extent
      (`EXT_LEFT`/`EXT_TOP`/`EXT_RIGHT`/`EXT_BOTTOM`) as plain
      queryable attributes — found by actually downloading one
      (`OPW_16.zip`, 4.1MB) and reading the GeoTIFF inside with
      `tifffile`: genuine `float32`, real metres (sample row: 21.6, 22.1,
      22.7m...). `-9999.0` is the NoData sentinel.
    - **Not full national coverage — confirmed, not assumed.** OPW flew
      this for flood-risk mapping, so it concentrates on rivers,
      floodplains, and coasts. Tested four deliberately inland/upland
      points (Slieve Bloom Mountains, Wicklow Mountains interior, Bog of
      Allen, rural mid-Roscommon): zero coverage at all four.
    - **`RESOLUTION` metadata can be wrong — confirmed by actually
      downloading a tile, not trusted at face value.** OPW Cork's
      coverage index claims `RESOLUTION: 2.0` for every tile; downloading
      one (`OPW_5.zip`, 214MB compressed) and reading the TIFF's actual
      shape gave `16003 x 16003` pixels over the same 2km extent — really
      0.125m (12.5cm), not 2m. `elevation.py` deliberately excludes OPW
      Cork from `LIDAR_SOURCES` for this reason (wrong resolution
      metadata, ~50x the file size, DTM only no DSM) — a real gap for
      Cork city specifically, documented rather than silently wrong.
    - **On-demand + cached, not a bulk national download.** The full OPW
      NASC (3,444 tiles) + older OPW (635 tiles) datasets are ~16GB+ —
      downloadable, but (a) not something to bundle into this app's
      normal deployment and (b) wouldn't even be complete coverage (see
      above). `elevation.py` instead queries the coverage index live per
      site (same pattern as every other source in this app), and only
      downloads+caches the one relevant tile (`.cache/lidar_tiles/`,
      gitignored) if one exists — confirmed ~0.7s cold (incl. download),
      ~0.1s warm.
    - **Pixel lookup needs a real coordinate transform, not a hand-rolled
      one.** Unlike karst.py's decision NOT to reproject ITM→WGS84 by
      hand (no verified transform, real risk of silently wrong pins),
      here a genuine WGS84→ITM (EPSG:2157) conversion is unavoidable to
      index into the tile's pixel grid — so `pyproj` was added as a real,
      tested dependency (confirmed against a known Dublin reference
      point) rather than derived from scratch. This is the correct
      response to that class of problem: add and verify a real library,
      don't hand-roll unverified projection maths either way.
    - **Large tiles don't need to be loaded into memory.** OPW Cork's
      excluded 1GB+ tile is stored uncompressed, one row per TIFF strip
      (`rowsperstrip: 1`) — confirmed `tifffile.memmap()` opens it
      instantly and reads a single pixel in <1ms regardless of file size,
      no need for windowed/tiled-TIFF machinery. Same approach used for
      the much smaller (~4MB) tiles actually in use.
    - **The contour fallback (EPA Hydrologically Corrected DTM, 10m
      interval / 20m grid) has NO usable map geometry — confirmed
      exhaustively, don't re-attempt this.** `returnGeometry=true` on
      `.../IE_GSI_EPA_Hydrologically_Corrected_DTM_20m_Contours_10m_IE26_ITM/MapServer/0/query`
      comes back `features: []` with `exceededTransferLimit: true` for
      EVERY combination tried: `f=json` and `f=geojson`, distances from
      10m to 500m, with and without `maxAllowableOffset`/
      `geometryPrecision` generalization, even asking for just one
      feature (`resultRecordCount=1`). This isn't karst.py's situation
      (geometry silently disabled server-side) — the response genuinely
      exceeds whatever size cap this ArcGIS Server enforces, which only
      makes sense if each contour "feature" is one enormous polyline
      spanning a huge stretch of the country rather than being split into
      shorter segments. Attribute-only queries (`CONTOUR_M`, no geometry)
      work fine — that's all `elevation.get_contours()` asks for.
    - **The map overlay was still solved — via `export`, not vector
      geometry.** The vector geometry dead-end above only blocks getting
      the raw *shapes* out of this service; it doesn't have WMS enabled
      either (checked `supportedExtensions` on the MapServer's own
      `?f=json` — only `FeatureServer`, no `WMS`). But its native
      `MapServer/export` operation (Esri's own equivalent of WMS
      `GetMap` — same idea, different name: render a bbox server-side,
      return an image) works, confirmed live, and — bonus — accepts a
      **plain WGS84 bbox directly** (`bboxSR=4326`), no ITM reprojection
      needed for this one. The rendered image is monochrome lines with
      elevation values printed as text labels baked into the image itself
      — no color ramp/hypsometric tinting, so there's no "color back to a
      number" legend to decode (asked and checked); the labels are just a
      picture of the same `CONTOUR_M` values already available as real
      data via the attribute query. Implemented as a small custom
      `L.TileLayer` (standard slippy-map tile→lon/lat math, computed
      independently rather than relying on Leaflet's private tile-bounds
      internals) calling `/export` per tile — deliberately not the
      Esri-Leaflet plugin, to avoid a whole new dependency for one layer.
      Registered directly in `clearOverlayLayers()`, not through
      `SECTION_OVERLAY_BUILDERS`/`addSectionOverlays()` — this layer is a
      general reference layer available everywhere (like the Street/
      Satellite base layers), not a per-site finding tied to one
      section's fetched data, and it's off by default (unlike every
      per-site overlay, which auto-displays) since it's dense enough
      countrywide to clutter the view before the user's asked for it.
15. **The real 2m LIDAR data is now actually visible on the map, not just
    a single point value.** Follow-up to a direct user ask: the "Terrain &
    elevation" card only ever showed one pixel out of the ~1000x1000 in a
    tile — the request was to see the whole tile, at its real per-pixel
    resolution, as an actual picture, not a coarser derived product (the
    contour tiles) and not another hillshade. `elevation.render_dtm_image()`
    reads the exact same cached DTM tile `get_precise_elevation()` already
    reads one pixel out of, min/max-normalises it **per tile** (a fixed
    national color scale would wash out contrast in any one 2km area), and
    color-maps every valid pixel through a small 4-stop hypsometric tint
    (green→yellow-green→tan→white, `_COLOR_STOPS`) — genuinely different
    from a hillshade: every pixel's color IS that pixel's real elevation,
    not a shading/slope value. NoData pixels render fully transparent
    (alpha 0), not black or a wrong color.
    - Also directly answered: "is there a color legend mapping brightness
      back to a number" — for GSI's own hillshade rasters, no (confirmed
      `pixelType: U8` = plain 0-255 shading, no legend exists to decode
      because there's no elevation encoded at all). But since this
      rendering is generated by *this app*, from real data, with a scale
      *this app* controls, the answer flips: yes, and the legend just IS
      the known min/max — no decoding needed, the frontend gets it
      directly (`image_min_elevation_m`/`image_max_elevation_m`, computed
      once from the same array already being read for point-value
      lookups) and renders a real gradient-bar legend next to the actual
      numbers in the card.
    - **Lazy, not eager.** The image is rendered by a new
      `GET /api/terrain-image?lat=&lon=` route on demand — only hit when
      the map layer is actually toggled on (an `L.imageOverlay`, wrapped
      in a bare `L.layerGroup()` purely so `addOverlay()`'s existing
      `group.getLayers().length` empty-check keeps working unmodified —
      `L.imageOverlay` isn't an `L.layerGroup` and has no such method of
      its own). A site whose terrain layer is never toggled on never pays
      the PIL/numpy rendering cost. `bounds_wgs84` and the min/max range
      ARE computed eagerly in `get_precise_elevation()` though (during the
      normal, always-run site lookup) — cheap enough to justify skipping a
      second round-trip just for those: the bounds come straight from the
      coverage index's own ITM extent attributes (a pyproj transform, no
      raster I/O at all), and the min/max needs one more small array read
      of a tile that's already been downloaded and cached anyway.
    - New dependency: Pillow (PNG encoding) — tifffile/numpy/pyproj were
      already added for the point-value lookup; this reuses them.

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
- Flood risk now covers fluvial/coastal/pluvial across current + mid/high
  future climate scenarios (21 layers — see `planning.FLOOD_LAYERS`).
  **Depth-grid layers** (`nat_depth_2m_*` — a raster giving an actual depth
  in metres, not just extent, at current climate only) are still unused —
  found on the same `GetCapabilities` listing, not yet queried. Would need
  a different WMS response handling than `GetFeatureInfo`'s vector-polygon
  path (`wms.py` currently assumes a vector hit) — check the response shape
  before assuming it's a drop-in extension.
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
