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
                             point sample. New deps: tifffile, numpy, pyproj, Pillow, shapely (see
                             requirements.txt) — the only module using them. get_terrain_mesh()
                             builds a real triangle mesh (not a height grid) over a padded area
                             around a confirmed plot (not clipped to its exact boundary — see
                             CLAUDE.md item 22 for why that was reverted), with the plot boundary
                             and nearby roads painted onto it as a texture (not built as
                             geometry) and nearby buildings.py buildings extruded as real 3D
                             objects, for the /terrain-3d rotatable 3D page (webapp.py) —
                             get_terrain_mesh() and the OSM fetch run concurrently in webapp.py,
                             joined via attach_features() (see CLAUDE.md item 21).
  buildings.py              nearby building footprints AND roads/tracks, from OpenStreetMap in one
                             combined Overpass query (get_nearby_features(), on-disk cached) — used
                             only by the /terrain-3d page's elevation.get_terrain_mesh(), not a
                             pipeline.py SECTION_SPECS entry (nothing to do with the report itself)
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
| Precise terrain elevation (~2m grid or better, ground + surface where available) | OPW, TII, and Westmeath Co Co's own LIDAR survey tiles (`LIDAR_SOURCES`, priority order — GeoTIFF, real float32 metres, ~30% of the country combined, confirmed via a real geometric union — see CLAUDE.md item 24) — downloaded + cached on demand, not a live API (none exists — see below) | `elevation.py` |
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
16. **Follow-up: the single-tile image was too small — now a proper
    multi-tile mosaic.** A ~2km survey tile doesn't reliably cover even a
    1km radius around an arbitrary point — if the point sits near a tile
    edge (confirmed at Trinity College Dublin), most of that radius falls
    in a *neighbouring* tile the single-tile version never fetched.
    Fixed by mosaicking: `_find_touching_tiles()` uses `arcgis.point_query()`'s
    existing `distance_m` (a real server-side buffered spatial query — no
    manual point-sampling needed) to get every tile intersecting a
    `IMAGE_RADIUS_M` (1000m) buffer from one source, confirmed live to
    return up to 4 tiles for one point. "Does this source actually cover
    the exact point" is answered with a plain bbox check against the
    returned tiles' own `EXT_*` attributes rather than a second query or a
    geometry library — these coverage-index features are plain
    axis-aligned squares, so their extent literally IS their exact
    boundary, an exact (not approximate) point-in-tile test with no
    polygon math needed. `_mosaic_dtm()` stitches the tiles into one
    array positioned by each tile's own real ITM extent (confirmed they
    share exact edges on a common grid — no reprojection/resampling), then
    crops to a square of the requested radius. Visually confirmed
    seamless — a real rendered mosaic at Trinity College Dublin shows the
    River Liffey running continuously across all 4 stitched tiles with no
    visible seam.
    - `get_precise_elevation()` and `render_dtm_image()` both call the
      SAME `_mosaic_dtm()` now, guaranteeing the reported bounds/min/max
      and the actual rendered image are always consistent — no risk of
      two independently-built mosaics drifting apart.
    - Real cost tradeoff, disclosed rather than hidden: `get_precise_elevation()`
      now builds the full mosaic (up to 4 tile downloads) even though it
      only needs one pixel from it, because the reported `bounds_wgs84`/
      min/max must match whatever `render_dtm_image()` will later render
      from the SAME mosaic-shaped data — computing them from a smaller,
      single-tile extent while the image covers a bigger cropped area
      would misplace the image overlay on the map (wrong bounds) and show
      a wrong legend. Confirmed cold-cache cost: ~12s for a genuine 4-tile
      case (one-time, per unique area); warm-cache (tiles already cached
      from an earlier lookup nearby): ~0.4s, same as before.
17. **The mosaic still had gaps — fixed by searching a bigger circle than
    the square it feeds.** Follow-up report after item 16 shipped: the
    corners of the 1km-radius crop still weren't always covered. Root
    cause, confirmed by testing: `_find_touching_tiles()` searched a
    circular buffer of exactly `radius_m` (1000m), then `_mosaic_dtm()`
    cropped to a `radius_m`-*square* — but a 1000m square's corners sit
    √2×1000 ≈ 1414m from the centre, further than the 1000m circular
    search ever reached. Any tile that only touched the square's corner
    region was never fetched, leaving that corner as NoData/transparent
    in the final image. Confirmed live at Fermoy, Co. Cork: a 1000m
    circular search found 3 tiles; a 1414m one found 7 — the extra 4
    covering exactly the missed corners. Fixed by searching
    `radius_m * 1.5` (comfortably past the exact √2 factor, for rounding
    margin) while still cropping to the original `radius_m` square —
    confirmed the crop is now 0% NoData at both Fermoy (7 tiles) and
    Trinity College Dublin (still 4, unchanged — it was never affected,
    which is exactly why this shipped once already without this bug
    surfacing there). The wider search does mean a few tiles get
    downloaded whose area doesn't end up in the final square crop at all
    — an accepted, harmless over-fetch (cached either way) in exchange
    for a real correctness guarantee, not a hand-tuned "seems to work"
    radius.
18. **User-reported clipping at R32 E4F8 was investigated and confirmed to
    be a real data-coverage edge, not a bug.** Screenshot showed a hard
    vertical line on the terrain image overlay, most of the visible map
    with no data. Confirmed directly: NASC's real survey coverage at this
    location starts exactly at ITM x=634,000; the site itself sits at
    x=634,445 (only 445m inside covered territory), while the 1km-radius
    crop needs to reach back to x=633,445 — 555m past where the real
    survey data physically ends (`point_query` returned two real tiles,
    `OPW_2004`/`OPW_2005`, stacked north-south with nothing further west).
    Checked the other source ("OPW pre-NASC") too, in case the wrong
    source had been picked — confirmed it has zero coverage here either,
    ruling that out. Also hit (and diagnosed as unrelated) two separate
    GSI server quirks along the way: transient `503`/timeout errors on
    first attempts (the same known GSI flakiness noted elsewhere in this
    doc — retries succeed), and a persistent `ArcGIS service error` from
    the pre-NASC service specifically at this location when queried with
    `distance_m` >= ~1000-1500m (works fine at 500m, and works fine at
    other locations even at large radii) — a real, location-specific
    server-side issue, harmless here since that source has no coverage
    regardless. Bottom line: item 17's square/circle fix was already live
    and correct; this clipping is what "no OPW survey data available"
    genuinely looks like at a real coastal/inland coverage boundary, same
    honest gap this whole feature has documented from the start (item 14).
19. **3D terrain rendering — a new page, not a new data source.** Asked
    whether the real LIDAR elevation data already being fetched could be
    shown as a mouse-rotatable 3D model of a confirmed plot's own shape
    (not a bounding box). Broken into two genuinely separate problems:
    - **Clipping the elevation grid to the plot's real boundary shape.**
      `cadastral.py`'s web-UI boundary convention
      (`polygon_ring_sets_wgs84` — a LIST of ring-sets, one per
      selected/merged parcel, each itself Esri's own `rings` structure:
      first ring = outer boundary, rest = holes) already exists and is
      exactly what's needed. Added a textbook ray-casting
      point-in-polygon test (`_point_in_ring`/`_point_in_polygon` in
      `elevation.py`) — unit-tested directly against a square and a
      concave L-shape before use. This is a deliberately different risk
      class from the karst-features ITM-transform situation documented
      above (where hand-rolling was correctly avoided): ray-casting is a
      simple, standard, easily-verified algorithm operating on
      coordinates already in the right frame, not an unverified
      projection that could silently shift every point. `get_terrain_mesh()`
      reuses the existing `_mosaic_dtm()` (sized to the boundary's own
      bounding box + a margin, not a fixed radius), downsamples to a
      max ~150x150 grid for a reasonable payload/mesh size, and marks any
      cell outside the polygon (or NoData) as `null`. Verified against a
      real cadastral boundary at Fermoy, Co. Cork (2.447 ha leasehold
      parcel): 146x145 grid, 28.9% of the bounding-box cells valid,
      elevation 21.5-25.9m — then rendered a quick standalone PNG of just
      the clipped cells and visually confirmed a genuine irregular parcel
      outline (not a rectangle, not garbled), cleaned up afterward.
    - **Rendering it as a rotatable 3D model.** New page
      (`GET /terrain-3d`, `sitescout/templates/terrain3d.html`) using
      Three.js loaded from jsDelivr as ES modules — confirmed live before
      use (`three@0.160.0/build/three.module.min.js` and
      `.../examples/jsm/controls/OrbitControls.js` both HTTP 200; note
      cdnjs only mirrors Three's `dist/` folder, no `OrbitControls` there
      at any recent version, so jsDelivr is the only option for it). This
      app's own Flask template pages aren't bound by the Artifact tool's
      CDN allowlist — that restriction is specific to the sandboxed
      Artifact tool, not this codebase. One vertex per real (non-null)
      grid cell; a quad's two triangles are only emitted when all four
      corners are real data, which is what makes the mesh's own silhouette
      trace the plot's actual boundary instead of a filled rectangle.
      Real-world elevation differences are usually subtle relative to a
      site's horizontal extent, so the model applies a disclosed 3x
      vertical exaggeration (labelled on-page, not hidden) so the
      "bending" the user asked to see is actually visible. Vertex colours
      reuse the same 4-stop hypsometric scale as the 2D image
      overlay/legend (`_COLOR_STOPS` in `elevation.py`), hand-kept in sync
      in the page's own JS — small and stable enough not to warrant a
      round-trip. `POST /api/terrain-mesh` takes `polygon_ring_sets_wgs84`
      as JSON body (not query params — a boundary's ring geometry is a
      real payload, not a couple of scalars). The boundary itself travels
      in the new page's own URL as a JSON query param (`/terrain-3d?boundary=...`),
      not session/local storage, so the page is self-contained and
      shareable via its own link — same principle as the rest of this app
      never depending on hidden state. The "View 3D terrain" link only
      appears in the Terrain & elevation card once a plot is actually
      confirmed (`plotConfirmed` + `sectionData.boundary.polygon_ring_sets_wgs84`)
      *and* precise LIDAR coverage exists there — checked fresh every time
      the card is rebuilt (`openDetail()` re-calls `build()` on every
      open), not cached from first render, so confirming a plot after
      first opening the tile still surfaces the link on reopen.
    - No new Python dependency — Three.js is pure client-side JS. Verified
      the mesh-generation math (vertex/index buffers, null-clipping,
      legend population, the fetch → `/api/terrain-mesh` wiring) via a
      manual Node harness with mocked Three.js classes (no real jsdom/GL
      context available for this one — WebGL needs a real GPU context
      jsdom doesn't provide) — confirmed correct vertex/triangle counts
      for a grid with a deliberately null diagonal region, and confirmed
      the exact same Fermoy boundary produces the exact same 146x145/
      28.9%/21.5-25.9m result through the live Flask route as it did
      in the standalone Python check. The three link-gating scenarios
      (confirmed+found, not yet confirmed, confirmed but no LIDAR here)
      were verified by extracting `terrainCard()` straight out of
      `index.html` into a small VM sandbox and calling it directly with
      each combination of `plotConfirmed`/`sectionData`/`precise.found`.
20. **The 3D page shipped broken (silently), then got a real boundary fix
    and OSM buildings.** Three separate follow-ups after item 19 shipped:
    - **Fix: `/terrain-3d` hung on "Loading terrain" forever, no request
      ever reached the server.** Root cause: `OrbitControls.js` (loaded
      from jsDelivr) itself does `import { ... } from 'three'` — a bare
      module specifier, which browsers can't resolve without an import
      map. Confirming the file's own URL returns 200 (done before
      shipping item 19) is NOT the same as confirming its *own imports*
      will resolve — that gap is exactly what let this ship broken. A
      bare-specifier import failure kills the whole ES module graph
      silently: no console error, no network request, `main()` never
      runs. Fixed with a standard `<script type="importmap">` mapping
      `"three"` to the real URL, and switched the page's own `THREE`
      import to the same bare specifier so both resolve to one identical
      module instance. Lesson for any future CDN-loaded ES module: check
      what IT imports, not just that its own URL is live.
    - **Fix: the clipped boundary was blocky ("Minecraft stairs"), not
      smooth.** Root cause: the elevation grid was clipped by testing each
      cell's 4 corners with a hand-rolled point-in-polygon test and
      discarding any cell not fully inside — geometrically correct but
      the edge could only ever land on a grid line, never on the plot's
      real boundary point. Fixed by adding `shapely` (GEOS) as a real,
      verified dependency — the same class of decision as adding `pyproj`
      for the WGS84<->ITM transform (see item 14): a textbook hand-rolled
      algorithm (plain ray-casting) is fine for a simple inside/outside
      test, but real polygon clipping and triangulation of an arbitrary
      *concave* shape is a different, much easier-to-get-subtly-wrong
      problem, and the correct response is a verified library, not a
      bigger hand-rolled algorithm. Confirmed live before use: `shapely
      2.1.2`'s `.intersection()` correctly clips a concave test polygon
      (exact area match), and `shapely.constrained_delaunay_triangles()`
      correctly triangulates a concave polygon *respecting its real
      boundary* (also exact area match) rather than falling back to a
      convex-hull Delaunay. `elevation.get_terrain_mesh()` was rewritten
      around this: boundary-straddling grid cells are now intersected
      against the real plot polygon and the resulting exact-boundary
      fragment is triangulated, with new boundary vertices' elevation
      bilinear-interpolated from that cell's own 4 real corner heights
      (exact at the corners themselves, a standard well-defined estimate
      elsewhere in the cell). Fully-interior cells skip the shapely call
      entirely (a plain vectorized `shapely.covers()` over the whole grid
      decides corner in/out status once, not per-quad) — this matters
      since interior cells vastly outnumber boundary ones and a shapely
      call per corner would be needless overhead for cells nothing is
      being clipped against. **This also changed the API contract**:
      `/api/terrain-mesh` now returns an explicit vertex+face mesh
      (`vertices`: `[x, y, elevation_m]` in metres east/north of the
      mesh's own origin; `faces`: triangle index triples) instead of a
      2D `heights` grid — the frontend (`terrain3d.html`) got simpler as
      a result: it no longer does any clipping/quad logic itself, just
      consumes the backend's already-built mesh directly. Verified the
      new mesh has zero degenerate (near-zero-area) triangles and no
      NaN/Inf vertices before shipping, and that the reported elevation
      range still matched the pre-shapely version exactly (21.5-25.9m at
      the same Fermoy test parcel).
    - **Add: nearby buildings (OpenStreetMap), asked for directly** ("we
      have buildings there in the OSM view, can we add them"). New
      `buildings.py` queries the public Overpass API
      (`overpass-api.de/api/interpreter`) for `way["building"]` within the
      mesh's own area — confirmed live before use (55 real footprints near
      Fermoy). Two real gotchas found in that same test: (1) the default
      `python-requests` User-Agent gets a plain `406 Not Acceptable` from
      this server — a custom, descriptive UA fixes it; (2) the free public
      instance genuinely times out under load (`504`, or a hard
      read-timeout) unpredictably, confirmed by seeing both on the exact
      same query that then succeeded on a later retry — handled with a
      few retries, not a fallback data source (no widely-used free
      alternative exists for this). **Confirmed, not assumed: most OSM
      buildings here carry no real height data** — only 8/55 in that test
      had a `building:levels` tag, none had `height`. A disclosed default
      (`DEFAULT_BUILDING_HEIGHT_M = 6.0`, ~2 storeys) is therefore doing
      real work for most buildings shown, not covering some rare edge
      case — every building carries its own `height_is_estimated` flag,
      surfaced in the 3D page's own legend text, not just in code
      comments. Each footprint is extruded (`elevation._extrude_building()`)
      into a flat-roofed prism — walls as simple quads, roof triangulated
      with the SAME `constrained_delaunay_triangles()` already added
      above — and grounded at the real elevation sampled from the SAME
      terrain mosaic under its own footprint centroid (skipped, not
      guessed, if that falls outside the mosaic's own coverage or on a
      real LIDAR data gap). **A real building shouldn't get 3x taller
      just because the terrain's vertical relief is exaggerated 3x for
      visibility** — so each building's own vertices are sent as height
      ABOVE ITS OWN GROUND (0 for the base ring, `height_m` for the roof
      ring), not an absolute elevation; the frontend places the base at
      `terrainY(ground_elevation_m)` (the same exaggerated surface height
      the terrain mesh has right under it) and adds the building's real,
      UNexaggerated height on top — deliberately different treatment from
      the terrain mesh's own vertices, verified directly (a mock Node
      harness confirmed vertex/index buffer sizes and the accumulated
      index-offset math across multiple merged buildings are all correct
      before shipping).
21. **Real user report: /terrain-3d took ~40s to load, and buildings still
    weren't showing.** Traced live (not guessed) to the log: three
    consecutive Overpass `504`s eating ~38s, at a genuinely rural test
    site that DOES have real buildings nearby (confirmed: a plain retry a
    minute later found 10 of them in 8s) — a transient overload, not a
    real "nothing here" case, and not something the original
    sequential-then-blocking design handled gracefully. Fixed three ways,
    same session as the "add roads too" follow-up below:
    - **Concurrency**: `webapp.py`'s `/api/terrain-mesh` now runs the
      LIDAR mesh build and the Overpass features fetch in a
      `ThreadPoolExecutor` (the exact same pattern `pipeline.py` already
      uses for report sections), instead of sequentially. Required
      restructuring `elevation.get_terrain_mesh()`: it now stashes the raw
      mosaic pieces it needs (`arr`/`ext`/`resolution`/`center_x`/`center_y`)
      in an internal `_raw` key, and a new `elevation.attach_features()`
      grounds/extrudes buildings and roads onto an already-built mesh
      using that stash — so the OSM fetch no longer has to be ready at the
      moment the mesh function itself returns. `_raw` is stripped in
      `webapp.py` before the result is ever `jsonify()`'d.
    - **Tighter retry budget**: 3 attempts x 30s timeout + 3s delay
      (worst case ~99s) down to 2 attempts x 12s timeout + 1.5s delay
      (worst case ~26s) — buildings/roads are a visual nice-to-have, not
      core report data, so failing faster and just showing none is the
      right trade, not chasing every possible transient failure.
    - **On-disk cache** (`.cache/osm_features/`, same atomic-write spirit
      as `elevation.py`'s LIDAR tile cache, keyed by a coarsely-rounded
      lat/lon/radius so near-identical repeat requests for "the same
      site" still hit it): confirmed live — a cached lookup for the same
      area that took 20s+ on a cold Overpass-struggling run came back in
      0.4s once cached. Unlike LIDAR survey data (never changes), OSM
      buildings/roads genuinely do get edited over time, hence a 30-day
      TTL rather than caching forever.
    - **Checked, and deliberately did NOT add, a second Overpass mirror**
      as a fallback: `overpass.osm.ch` responded fast (0.3-0.6s) but with
      **zero results** at three different radii (74m/200m/500m) around a
      point confirmed (via the primary instance, moments apart) to have
      real buildings — a stale or incomplete mirror. A fast wrong answer
      ("no buildings here") is worse than an honest slow timeout — exactly
      the class of silent-failure trap this doc already warns about
      elsewhere (the missing-`/query`-suffix gotcha) — so only the one
      confirmed-correct instance (`overpass-api.de`) is used.
    - **Add: roads/tracks, asked for in the same follow-up** ("roads too
      if there are any in the property"). Rather than a second slow
      Overpass round-trip, `buildings.py`'s query was combined into one
      request fetching both `way["building"]` and `way["highway"]`
      together (`get_nearby_features()`, replacing the old
      `get_nearby_buildings()`) — deliberately, since doubling load on an
      already-confirmed bottleneck would undo the point of the fixes
      above. Each road is extruded (`elevation._extrude_road()`) into a
      thin ribbon at a standard rough width by highway type
      (`ROAD_WIDTH_M` — OSM's own `width` tag is rarely present, same
      "confirmed sparse real data" situation as building heights).
      **Deliberately different vertex encoding from buildings**: a
      building has one flat floor level, so its vertices are sent as
      height-above-its-own-ground and the frontend adds its real,
      unexaggerated height on top of the (exaggerated) terrain surface
      (see item 20). A road's elevation genuinely varies along its length
      — there's no single "floor level" — so each road vertex instead
      carries its own ABSOLUTE elevation, sampled from the same mosaic at
      that exact point, and the frontend runs it through the *identical*
      `terrainY()` exaggeration transform the terrain mesh itself uses:
      the road correctly follows the same stretched slope it actually
      sits on, rather than needing a true-scale exception the way a
      building's height does. A road that partially leaves the mosaic's
      own coverage is split into separate continuous on-coverage segments
      (`_extrude_road()` returns a list, not one mesh) rather than
      guessing an elevation across the gap. Verified via the same mock
      Node harness as buildings: vertex/index buffer sizes for all three
      geometries (terrain/buildings/roads) matched exactly against a real
      captured response (2 buildings, 3 road segments) before shipping.
22. **Real user report with a screenshot, exposing that items 19-21's whole
    approach for the boundary/roads was wrong in an instructive way — not
    just buggy.** The screenshot showed: (a) the road ribbon visibly
    blocky/discontinuous at corners (no mitring between per-segment
    quads); (b) two buildings floating fully disconnected in empty black
    space, nowhere near the rendered terrain island. (b) was the more
    important tell — it meant the exact-polygon-clip approach (item 19-20)
    was fundamentally the wrong shape for this problem: any building or
    road just outside the tight clip (a real, relevant part of a site's
    context) had nothing to render on top of, because the terrain literally
    didn't exist there. No amount of further polishing the clip or the
    road-ribbon mitring would fix that — the boundary/roads needed to stop
    being geometry matched to a location on a small clipped island and
    become part of a much larger, plain terrain surface instead.
    - **Terrain**: dropped the exact shapely clip entirely (items 19-20)
      in favour of a plain, uncipped rectangular grid over the plot's
      bounding box + `MESH_CONTEXT_BUFFER_M` (50m) of real surrounding
      context — not just to the plot's own edge. This is a genuine
      simplification, not a workaround: `get_terrain_mesh()`'s main loop
      no longer needs shapely at all for the terrain itself (no more
      per-cell `covers()`/`intersection()`/`constrained_delaunay_triangles()`
      calls), just "does this cell have 4 real (non-NoData) corners" — 2
      triangles if so. `shapely` stays a real dependency, just narrower in
      scope now: building the boundary polygon (for the texture below) and
      triangulating building roofs.
    - **Boundary + roads: painted onto the terrain as a texture, not built
      as geometry** — literally what was asked for, and it directly fixes
      both screenshot problems at once, not by coincidence: a texture is
      "ink on the surface" that can never be positioned wrong relative to
      the surface it's drawn on (no clipping to misalign, no ribbon
      segments to have joints at all). `get_terrain_mesh()` now generates
      an `overlay_texture_png_base64` (PIL, white background, the plot
      boundary + any roads stroked on top in real-world-proportional pixel
      widths, `ROAD_WIDTH_M`/`BOUNDARY_LINE_WIDTH_M`) applied as the mesh's
      own `THREE.MeshStandardMaterial.map`, multiplied together with the
      existing per-vertex hypsometric elevation tint (`vertexColors`) —
      confirmed live this multiply-composition is exactly what
      `MeshStandardMaterial` does with both set simultaneously. Each
      vertex's UV is computed directly from `grid_extent` (the mesh's own
      local-coordinate bounds, also returned in the response) using the
      SAME mapping the backend used to place ink on the texture
      (`_local_to_pixel()`), so the two are guaranteed pixel-aligned by
      construction rather than by two independently-tuned coordinate
      systems happening to agree. Confirmed Three.js's default
      `texture.flipY = true` needs no extra correction: v increasing with
      real-world north correctly samples the PNG's own top row (drawn as
      the north edge), verified by rendering and visually inspecting the
      overlay PNG directly (a real, correctly-shaped, non-mirrored
      Fermoy parcel outline with real nearby roads, clean and continuous).
      Since roads/boundary fetch (buildings.py, concurrent with the mesh
      build — item 21) may land AFTER the mesh's own texture is first
      drawn, `attach_features()` keeps the live, not-yet-encoded PIL
      `Image` object in `_raw` and draws roads onto that SAME image
      in-place, re-encoding to PNG only once both passes are done —
      avoiding two independently-drawn textures that would need
      reconciling. `_extrude_road()`/3D road ribbons are deleted entirely,
      not just unused — there's no reason to keep two code paths for the
      same information once one of them is both correct and simpler.
    - **Buildings: stayed real 3D objects** (the user explicitly
      distinguished them from roads/boundary: a building is a genuine 3D
      volume, not a marking on the ground) — but fixed the OTHER real
      problem the screenshot showed indirectly (floating/gapping against
      sloped ground): `_extrude_building()` previously sampled elevation
      at only the footprint's centroid, so any part of a real (non-flat)
      footprint that sat higher than that one point would show the base
      floating above the visible terrain. Fixed by sampling elevation at
      EVERY footprint vertex plus the centroid, taking the minimum, and
      subtracting `BUILDING_EMBED_M` (0.75m) below that — the base now
      plants firmly into the terrain everywhere under the footprint, not
      just barely touching at its single lowest real sample.
23. **Follow-up to item 22: jagged "tearing" along the rendered terrain's
    outer edge, in a real screenshot.** Real LIDAR data can have a handful
    of genuinely noisy/outlier pixels (sensor edge effects, water-surface
    returns) that are invisible in a single point reading or a flat
    top-down colour-mapped image (`get_precise_elevation()`/
    `render_dtm_image()` — neither changed here) but show up as sharp
    spikes once that same raw pixel is part of a LIT, rotatable 3D
    surface — more likely to actually be reached now that item 22 renders
    a much wider padded area rather than a tight clip. Fixed with a 3x3
    median filter (`_median_smooth()`, plain numpy — no new dependency)
    applied to the array only inside `get_terrain_mesh()`, right before
    meshing. Median, not mean/Gaussian: the standard, textbook tool for
    outlier/spike noise specifically, verified directly against a
    synthetic single-pixel spike before use (removed exactly, left an
    unrelated pixel untouched). NoData is excluded from every filter
    window: a pixel whose neighbourhood contains ANY NoData is left at its
    own raw value, untouched — confirmed directly that a pixel one column
    from a NoData region stayed exactly at its original value rather than
    blending toward -9999, since blending real elevation with "no data
    here" would be a worse bug than the noise being fixed.
24. **Wired in TII and Westmeath Co Co as two more real LIDAR sources —
    found by actually computing national coverage, not guessing.** Asked
    what % of the country has this LIDAR data and why; answered by
    querying the exact same GSI coverage index this app already uses and
    computing a real geometric union (shapely) rather than a naive
    tile-count estimate: OPW's own two sources are ~19.6% of the country
    (13,776km², NASC alone, geometrically verified). That led to checking
    whether GSI's server hosts other agencies' coverage indexes too — it
    does, at `Lidar/IE_GSI_LiDAR_Coverage_{TII,GSI_DCHG_DP,NYU_Dublin,
    OPW_Cork,WH_CoCo}_IE26_ITM` — and TII (Transport Infrastructure
    Ireland's road/rail corridor survey) alone adds 10,268km², nearly
    doubling real usable coverage to ~30%.
    - **TII and Westmeath Co Co are now wired into `LIDAR_SOURCES`** (after
      OPW NASC/pre-NASC in priority order). Confirmed each one's real
      on-disk format before trusting it, not assumed to match OPW's
      convention — and they didn't, in three separate ways: TII's coverage
      index has **no `EXT_*` attributes at all** (only real `esriGeometryPolygon`
      geometry, confirmed to be the same exact 2km-square convention via
      its own `SHAPE.AREA` = exactly 4,000,000), **no DSM** (its zip has
      one `.tif`, not a `_DTM`/`_DSM` pair), and **a different NoData
      sentinel, `-99.0` not `-9999.0`** (confirmed directly: 622,256 of
      1,000,000 pixels in a real sample tile are exactly `-99.0`, zero
      pixels at any other implausible negative value). Westmeath, once
      those fixes existed, needed none of its own — same `EXT_*`/
      `_DTM.tif`+`_DSM.tif`/`-9999.0` conventions as OPW, just one folder
      level deeper in its zip.
    - `_tile_extent()` (new): a tile's extent from `EXT_*` attributes when
      present, or derived from its own real polygon geometry when not —
      exact, not approximate, since these are confirmed plain axis-aligned
      squares either way. This ALSO retroactively hardens OPW pre-NASC,
      which — discovered along the way — has **neither** `EXT_*` nor real
      geometry for any of its 635 features (checked the full dataset).
      That was a live, dormant crash risk before this fix (an unguarded
      `f["attributes"]["EXT_LEFT"]` with no such key) that had simply never
      been triggered; now it degrades to "this source has nothing usable
      here," same as any other source with no coverage. Left in
      `LIDAR_SOURCES` anyway rather than deleted — costs nothing to keep
      and would start working again for free if GSI ever fixes that
      service's schema. GSI Phase2 and NYU Dublin have the same problem
      and are excluded for the same reason (both small, a few km²
      combined, not worth chasing).
    - **A real, previously-unknown ArcGIS gotcha, found via a hard failure,
      not assumed**: querying TII's layer for the shared `EXT_LEFT/TOP/
      RIGHT/BOTTOM` field list — fields that don't exist in its schema —
      threw a hard `"Failed to execute query"` error, not a silent ignore.
      Different from (and worse than) the already-documented "hit the bare
      layer URL, get schema JSON back" gotcha above: this is a real 400-
      class failure from a well-formed request to the right endpoint,
      caused purely by naming a field the target layer's schema doesn't
      have. Fixed by requesting `outFields=*` universally for the coverage-
      index query instead of a named field list — sidesteps needing to
      know each source's exact schema up front, and the app already
      handles missing keys gracefully via `.get()`/`_tile_extent()`'s own
      fallback.
    - **A real architectural gap, found by testing TII end-to-end, not
      designed for in advance**: a coverage-index tile's bounding box
      containing a point never guaranteed that exact PIXEL had real data —
      confirmed live, a genuine OPW NASC tile whose square covers a real
      TII-corridor point but whose actual survey has a hole exactly there
      (OPW's own flood survey, like TII's corridor survey, doesn't fill
      every tile edge-to-edge). The original design committed to the
      first source whose bbox matched and stopped — meaning adding TII/
      Westmeath as lower-priority fallbacks would have silently done
      nothing in exactly the cases they're meant to help with (bbox
      overlaps an already-tried higher-priority source, but that source's
      real data is a gap). Fixed with `_point_has_real_data()`: after
      building a candidate source's tile list, read the actual pixel at
      the query point before committing to it, falling through to the
      next source if it's NoData. Uses `tifffile.imread()`, not this
      module's usual memmap-based single-pixel read — confirmed live that
      `tifffile.memmap()` throws `"image data are not memory-mappable"` on
      a real, ordinary OPW tile (memmap only works on uncompressed,
      contiguous TIFF data, and not every real tile turns out to be one) —
      a second pre-existing gap surfaced by exercising this code path for
      the first time, on a source (OPW) that had never needed it before.
      Verified the fix live end-to-end: the same TII point now correctly
      logs OPW NASC's bbox match, rejects it as real-NoData, tries OPW
      pre-NASC (no bbox match at all), then finds and correctly uses TII
      (66.35m, matching the tile's own real pixel value exactly) — and
      confirmed no regression at Fermoy (still OPW NASC, same 22.24m) or
      the R32 E4F8 coverage-edge case from item 18 (still resolves the
      exact point the same way as before; only the wider 1km image's own
      honest edge gap is unrelated to this fix).
    - Every user-facing "OPW LIDAR" label (`get_precise_elevation()`'s
      `source` field, the terrain card, the map layer toggle) now reflects
      whichever source actually answered — `TII LIDAR`, `Westmeath Co Co
      LIDAR`, etc. — rather than hardcoding OPW, since that stopped being
      true the moment a second agency's data could be the one shown.
    - **Deliberately not yet wired in: GSI/DCHG/DP (heritage sites, 1,619km²
      confirmed)** — its tiles are ESRI ASCII Grid (`.asc`), a genuinely
      different raster format from GeoTIFF, confirmed by downloading a
      real sample (a plain 6-line text header — `ncols`/`nrows`/
      `xllcorner`/`yllcorner`/`cellsize`/`NODATA_value` — followed by
      space-separated rows). Not hard to parse (the header states its own
      NODATA_value explicitly, no guessing needed) but it's a new raster-
      reading code path through every `tifffile` call in this module, and
      deserves its own dedicated pass rather than being bundled in here.
25. **Water flow analysis — real local minima (where water pools) and the
    drainage network that feeds them, toggleable on the 3D terrain view.**
    Asked whether the plot boundary's own shape in the 3D view could be
    analyzed for water flow; specifically wanted the points where water
    has nowhere further to go (local minima) and the convergent "low
    point lines" — the standard hydrological terms are **sinks** (or
    pits) and **flow accumulation** (the network you get by routing water
    downhill from every cell and counting how much passes through each
    one) — this is exactly the same computation GIS hydrology tools use
    to derive a stream network from a DEM, not a bespoke invention.
    - **D8 flow routing + accumulation** (`_compute_flow_network()`):
      each cell's water flows entirely to whichever of its 8 neighbours
      has the steepest downhill slope; accumulation is computed by
      processing cells from highest to lowest elevation so every upstream
      contributor is finalized before being passed further down. Same
      risk class as bilinear interpolation or ray-casting elsewhere in
      this app: a simple, well-defined, textbook algorithm, not something
      that needed to be guessed at. Verified directly before use, not
      assumed correct: a synthetic 20x20 V-shaped valley draining to one
      corner gave a single sink exactly at the true basin bottom, with
      accumulation there equal to the full 400-cell grid (real mass
      conservation) and accumulation increasing monotonically along the
      valley floor toward that outlet. Deliberately does NOT fill sinks
      before routing (the standard preprocessing step for tools that need
      water to keep flowing somewhere) — this app wants the opposite: the
      real, unfilled local minima are the actual answer to the question
      asked, so flow correctly terminates there.
    - **Raw per-pixel sink detection was unusable, confirmed by actually
      running it, not assumed fine**: 423 individual "sink" pixels on one
      ordinary parcel's padded mosaic — almost all flat micro-plateaus a
      few cm across left over from median smoothing (adjacent cells
      sharing the exact same value have no STRICTLY lower neighbour, so
      D8 marks all of them as separate sinks — a well-known limitation of
      plain D8 on flat/smoothed terrain). Tried stronger smoothing first;
      confirmed live that it made the problem WORSE (a 15x15 median
      filter gave 849 sink pixels, not fewer — bigger kernels create
      bigger flat plateaus, more ties, not less). Fixed instead with
      `_cluster_sinks()`: 8-connected clustering of sink cells into one
      point per contiguous low area, ranked by that cluster's own real
      catchment size (its own flow-accumulation value — how much upstream
      area actually drains into it), which naturally sorts a genuine
      puddle-forming point (fed by real contributing area) ahead of an
      isolated single-cell numerical blip with nothing draining into it.
      Brought the same test parcel down to 32 real clustered low points —
      capped to the top `MAX_REPORTED_SINKS` (20) by catchment size for
      display, with the true total still disclosed alongside them.
    - **The flow-line channel threshold has to scale with the site's own
      area, not a fixed cell count** — confirmed live: a fixed threshold
      of 6 contributing cells was reasonable for a single small parcel's
      own grid but let ~90% of a bigger padded mosaic's cells qualify,
      drawing lines almost everywhere instead of highlighting real
      channels. Fixed with `FLOW_CHANNEL_THRESHOLD_FRACTION` (0.5% of the
      site's own total valid cell count, floored at
      `FLOW_MIN_CONTRIBUTING_CELLS_FLOOR`) — confirmed visually afterward:
      a sparse, genuinely dendritic (branching) network, not a solid wash
      of lines.
    - **Rendering reuses the exact same "paint it on the surface" overlay-
      texture approach as the boundary/roads layer** (see item 22) rather
      than building separate 3D line/marker geometry — `get_flow_analysis()`
      returns its own transparent `flow_overlay_png_base64` (line width/
      opacity log-scaled by catchment size — a single trickle stays thin
      and faint, a channel fed by many converging cells gets thick and
      bold, matching the user's own description) plus red sink markers,
      using the SAME `_local_to_pixel()`/`grid_extent` mapping as the
      terrain mesh so it's pixel-aligned by construction. On the frontend,
      the flow overlay is a SEPARATE `THREE.Mesh` reusing the terrain's
      own `BufferGeometry` object directly (identical vertex positions/
      UVs, confirmed no duplicate buffers needed) with
      `polygonOffset`/`transparent` to sit cleanly on the surface without
      z-fighting, toggled via a checkbox and fetched lazily on first
      toggle-on only (same "expensive rendering only when a layer is
      actually switched on" pattern as the map's own terrain-image
      overlay) — confirmed via a mock harness that toggling on twice
      only fetches once, and toggling off/on again just shows/hides the
      same mesh.
    - A real, disclosed limitation stated directly in the response and the
      UI: D8 routing on real (if median-smoothed) LIDAR terrain models
      where water WOULD go on this exact surface shape — a genuine,
      standard technique, but not a substitute for an actual site
      drainage survey, and it says nothing about subsurface drainage,
      soil permeability, or engineered drainage already on site.
    - **Follow-up, real user report with a screenshot: the lines looked
      like disconnected arrows, and were pixelated/low-res.** Both traced
      to the same root cause: the first version drew one independent
      1-cell segment PER qualifying pixel (cell -> its own downstream
      neighbour), so at every confluence, several short segments from
      different upstream directions all terminated at the same point with
      nothing connecting them into a continuous line — reading visually as
      converging arrowheads, not a stream. Fixed by tracing whole channels
      instead: `_compute_flow_network()` now also returns `flow_to`
      (already computed internally, just not exposed before), used to
      find real "channel heads" (a qualifying cell with no qualifying
      upstream neighbour of its own) and walk each one's full path
      downhill to a sink or the mosaic's own edge, drawing that entire
      path as ONE connected multi-point line — stopping early if a path
      merges into a channel another head already traced, to avoid
      needlessly re-drawing shared downstream tails. Separately: PIL's own
      line/ellipse drawing has no anti-aliasing at all, which combined
      with the old short disconnected segments made everything look
      chunky — fixed by drawing at `FLOW_SUPERSAMPLE` (3x) the final
      texture size and downsampling with LANCZOS resampling, the standard
      supersample-then-downsample technique for anti-aliased-looking
      output from an API with none built in. Confirmed visually
      afterward, including a close zoomed crop: smooth, continuous,
      properly branching drainage lines with real tributary confluences,
      not arrows — performance unaffected (~0.6s, same as before).
26. **Follow-up: the unfilled model wasn't answering the real question —
    "sinks that are tiny and would quickly overflow" need to actually
    overflow, not just be a point where flow stops forever.** The
    unfilled model (item 25) is only half of a real flood event: a real
    depression fills with water until it spills over its own lowest rim,
    then that overflow keeps flowing downhill toward whatever's next —
    asked for directly, wanting to see sinks fill up and the paths to
    the next downstream sink, not a static "flow terminates here" map.
    - **`_fill_and_route()`**: standard priority-flood depression filling
      (Barnes et al. 2014 — the real algorithm hydrology tools use to
      prepare a DEM for basin-to-basin routing), which derives flow
      direction directly from its OWN fill order rather than re-running
      D8 on the filled result afterward. That distinction matters and was
      confirmed live, not assumed: plain D8 on a filled array breaks on
      the flat plateaus filling deliberately creates (every cell in a
      pool shares the exact same elevation, so none has a strictly lower
      neighbour — the standard "flat resolution" problem in DEM
      hydrology). Recording each cell's flow direction AT THE MOMENT the
      priority-flood algorithm "conquers" it sidesteps the problem
      entirely, since a cell's conqueror is always its correct downhill
      neighbour by construction, tie or no tie. Verified on a synthetic
      two-basin case (a shallow basin, a deeper one, separated by a
      saddle) before use: the shallow basin's real bottom went from an
      isolated dead end (flow_acc=1, nowhere to go) to a real contributing
      catchment (28 cells) whose flow direction chain correctly led
      through the saddle, through the deeper basin, all the way to the
      map's own edge — a genuine cascading path, not a basin that just
      stops.
    - **Two separate flow computations, on purpose**: the original
      unfilled `_compute_flow_network()` still decides WHERE the real
      local minima are (filling exists to route water THROUGH a basin,
      not to decide where a basin's own floor is — filling would move/
      hide the very points being asked about). `_fill_and_route()`'s
      filled/routed result decides where an overflowing sink's water
      goes NEXT, and is what the drawn flow-line network now uses.
    - **New per-sink figures, directly answering "how quickly would this
      overflow"**: `spill_elevation_m` (the level a depression fills to
      before it spills over its own rim) and `fill_depth_m` (spill minus
      the real bottom — its actual capacity). Confirmed live at Fermoy:
      most real depressions are shallow (0.0-0.33m fill depth) — small
      real undulations in ordinary terrain, not proper ponds, exactly
      matching the "tiny sinks that would quickly overflow" framing.
    - **Flood-extent visualization**: every cell the fill algorithm
      actually raised (`filled > original + FLOOD_EPSILON_M`) is painted
      as a translucent blue pool directly onto the same overlay texture
      as the flow lines/sink markers (same supersample-then-LANCZOS-
      downsample anti-aliasing as everything else on this texture) — this
      literally IS a flood event visualization: exactly which cells would
      be underwater once every depression fills to its own natural spill
      point. `flooded_area_m2` (total flooded cell count x resolution^2)
      is returned and shown in the 3D page's own toggle summary text.
      Confirmed visually: real irregular-shaped pools (not blocky
      rectangles) with flow lines now correctly running THROUGH them and
      continuing out the other side toward the next basin or the map's
      edge, instead of stopping arbitrarily.
    - Performance cost of the extra priority-flood pass, measured not
      assumed: ~1.3s at Fermoy (roughly double the ~0.6s unfilled-only
      version) — still fine for an on-demand toggle fetch, no frontend
      changes needed since the response only gained new fields.
27. **Interactive catchment selection: click one or more low points, see
    their combined real catchment area highlighted.** Asked for directly
    — select sink(s) in the 3D view and see the total area draining into
    them, in a distinct colour. Kept entirely client-side after one fetch
    (no round trip per click, for a tool meant to be clicked around
    exploratively): `get_flow_analysis()` now also returns the filled/
    routed flow-direction grid itself (`flow_dir_codes`, `rows`, `cols`,
    `cell_size_m`), compactly encoded as one small int per cell (0 = sink/
    NoData, 1-8 = an index into `FLOW_DIR_OFFSETS` — a fixed 8-direction
    table kept byte-for-byte identical in both `elevation.py` and
    `terrain3d.html`, since decoding depends on the two agreeing) rather
    than the raw two-number-per-cell offsets, halving that part of the
    payload. Each sink also now carries its own `row`/`col` so a click
    can seed the trace directly, with no reverse coordinate lookup needed.
    - **Client-side reverse-BFS, not a new endpoint per click**: the
      frontend builds a reverse adjacency ("which cells flow INTO this
      one") from `flow_dir_codes` once, then a click on a sink marker
      seeds a plain breadth-first search over that reverse graph — every
      cell reachable backward from the sink is, by definition, everywhere
      its water could have come from. Multiple selected sinks union their
      BFS results (a cell counts if it's upstream of ANY selection),
      which is also just a set union, no extra graph work.
    - **Reuses the FILLED/routed flow graph, not the raw unfilled one —
      confirmed these give meaningfully different answers, not just
      similar ones**: computed both for the same real sinks at Fermoy and
      found no consistent relationship — one sink's catchment went from
      961 cells (unfilled) to 15 (filled/routed), another from 198 to
      1808. This makes sense once stated plainly: unfilled catchment is
      "this basin's own immediate watershed in isolation"; filled/routed
      catchment is "everywhere that ends up flowing through this exact
      point once upstream depressions overflow into it" — a genuinely
      different question, and the one this feature is actually answering
      ("total catchment of the selected points" only means something
      coherent under the cascading model, item 26's whole reason for
      existing).
    - **Sink markers are real 3D objects** (small spheres, not just baked
      into the flow overlay's own texture image) specifically so they can
      be raycast against for click selection — positioned using the exact
      same `terrainY()` exaggeration function the rest of the scene uses,
      so they sit correctly on the (deliberately stretched) surface.
      Click detection distinguishes a real click from an OrbitControls
      drag-to-rotate gesture by comparing pointerdown/pointerup screen
      position (movement under 5px counts as a click) rather than using
      the browser's native `click` event, which can behave inconsistently
      after a drag depending on what consumed the intervening pointer
      events.
    - The highlighted catchment is its own THIRD overlay mesh (after the
      base terrain and the static flow/flood texture), reusing the same
      shared `BufferGeometry` as everything else on this page, drawn via
      an HTML canvas + `THREE.CanvasTexture` updated in place
      (`needsUpdate = true`) on every selection change rather than
      rebuilt from scratch, and hidden/shown together with the rest of
      the flow-analysis layer when the main toggle is switched off and
      back on.
    - Verified end-to-end via a mock Three.js harness that simulates REAL
      pointerdown/pointerup events through the renderer's own canvas
      (with the mock raycaster forced to report a hit on a specific
      marker) rather than reaching into the module's internal closures —
      confirmed: single-select shows the correct catchment size (15
      cells, exactly matching an independent Python reference
      implementation of the same reverse-BFS run against the identical
      captured data); click-again deselects; multi-select unions
      correctly (36 cells for two sinks independently measured at 15 and
      21 — no overlap between them, so a clean sum); repeat selections
      reuse the same texture/mesh rather than creating duplicates; and
      unchecking/rechecking the main flow toggle correctly hides and
      restores the catchment layer together with everything else.
28. **Follow-up, real user report with a screenshot: two of three selected
    points showed a thin line instead of a proper catchment area.**
    Traced directly, not guessed: the two "broken" points shared the
    EXACT same filled elevation (21.586m, confirmed) — they're the same
    real flat pool. `_fill_and_route()`'s conquest-tree flow direction
    (item 26) is, by construction, a spanning TREE: exactly one path from
    any cell back toward the border. For two cells sharing one flat pool,
    that means each one's reverse-BFS only follows the ONE arbitrary
    branch of the fill algorithm's own tree that happened to reach that
    specific pixel — not the pool's real combined contributing area,
    which is the SAME for every cell in it (hydrologically, one shared
    pool has one shared catchment, not a different one per pixel).
    Confirmed the bug's exact scale before fixing: the two points showed
    21 and 7 cells individually; expanding to the whole 610-cell shared
    pool first and tracing from all of it gave the real answer, 3106
    cells.
    - **`_label_pools()`**: connected-component labelling (8-connected)
      over cells sharing (near-)identical filled elevation — one more
      compact per-cell code sent to the frontend (`pool_labels`, -1 for a
      cell that isn't part of any shared pool), reusing the exact same
      "send a code, decode client-side" pattern as `flow_dir_codes`.
    - The frontend now expands a clicked sink to its WHOLE pool
      (`seedsForSink()` — every cell sharing that sink's `pool_labels`
      value) before seeding the reverse-BFS, not just the single clicked
      pixel. Selecting either of two sinks sharing a pool, or both
      together, now all correctly resolve to the identical, real
      catchment — verified directly (see below), not just visually
      plausible.
    - Verified end-to-end via the same mock-click harness as item 27,
      extended to specifically click the two real sinks confirmed (via a
      live Python check against the exact same captured data) to share a
      pool: selecting either one alone now shows the correct 3,106-cell
      catchment (not 21 or 7), selecting the other alone shows the
      identical 3,106 (not a different number — confirming both correctly
      resolve to the SAME real pool), and selecting both together still
      shows exactly 3,106 (correct union of a pool with itself — no
      double-counting), while distinct-pool multi-select (item 27's own
      test case) still correctly sums instead of collapsing.
29. **Unified the flow-line colour with the flood-pool colour, and added
    markers wherever a channel exits the analyzed area.** Both asked for
    directly. The colour change was simple (`FLOW_LINE_COLOR` and
    `FLOOD_POOL_COLOR` now both derive from one shared `WATER_COLOR`
    constant) — the exit-point markers needed the same "confirm before
    building" discipline as everything else here.
    - **What a "line exits the texture" point actually IS, confirmed
      directly rather than assumed**: in `_fill_and_route()`'s filled/
      routed graph, `flow_to == (-1, -1)` (code 0) turns out to occur
      ONLY for the cells that seeded the fill algorithm in the first
      place — the mosaic's own outer border, or cells next to a real
      internal NoData gap in the LIDAR data. Checked live at Fermoy:
      7,572 such cells, 698 on the literal border and the rest against
      internal NoData — zero "leftover" unrouted local minima, since
      depression filling connects every real basin bottom onward by
      construction (that's the whole point of item 26). So "where does a
      channel exit the texture" and "every code-0 cell in the routed
      graph" are exactly the same question — no separate detection logic
      needed beyond what `_fill_and_route()` already computes.
    - Only code-0 cells that actually carry a meaningful channel (the
      same `_channel_threshold()` — extracted as its own small function,
      reused by both the line-drawing code and this — already used to
      decide whether to draw a line there at all) get a marker; otherwise
      every one of a mosaic's few hundred border pixels would get its own
      point regardless of whether any real water reaches it. Deliberately
      a SEPARATE concept from the existing basin `sinks` (real local
      minima with a genuine fill depth/spill elevation) — an exit point
      has neither, since it's not a basin at all, just where the
      analysis itself runs out of data — so it's added with
      `is_exit: true` and null `spill_elevation_m`/`fill_depth_m`, drawn
      in a visually distinct grey (`EXIT_MARKER_COLOR`) rather than the
      basin markers' red, and counted separately
      (`total_exit_points_found`, alongside the existing
      `total_sinks_found`) so the two are never conflated in the UI text.
    - Exit points are fully interactive the same way basin sinks are — no
      special-casing needed on the frontend beyond the marker colour,
      since click-to-select/catchment-tracing only ever needs a point's
      `row`/`col` (present on both). Verified directly: clicking an exit
      point's own marker correctly traces its real catchment (confirmed
      against the backend's own reported `catchment_cells` for that exact
      point) and unions correctly with other selected points, same as
      confirmed for basin sinks in items 27-28.
    - **Follow-up, real user report: the lake and line blue still didn't
      match even after sharing one RGB constant.** Same RGB isn't the
      same rendered colour once alpha enters it — both layers get
      alpha-blended over the terrain's own varying colour underneath, so
      a translucent pool and a near-opaque line read as visibly different
      shades regardless of RGB. Confirmed the actual numbers: the pool
      was fixed at 120/255 alpha (~47% opaque) while a strong flow line
      reaches 255 (fully opaque) — a big channel looked solid and
      saturated right next to a pale, washed-out lake. Fixed by raising
      `FLOOD_POOL_ALPHA` to 225, matching the SATURATED end of what a
      bold line reaches rather than the line's own faint-trickle end —
      confirmed visually afterward: pools and lines now read as one
      continuous, solid body of water.

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
