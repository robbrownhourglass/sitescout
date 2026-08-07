# Ireland Site Scout

Given an Eircode or address, pulls together geology, water table,
archaeology/heritage, property boundary, utilities, and planning context for
a site from Ireland's public data sources.

**Before doing any dev work here, read [`CLAUDE.md`](./CLAUDE.md)** — it has
the full architecture, verified data sources, and (important) a summary of
why exact per-Eircode coordinates aren't achievable with free geocoding
services, confirmed through direct testing. Don't re-investigate that from
scratch.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # already has working temporary test keys, or add your own
```

## Run (CLI)

```bash
python main.py "R32 E4F8"
python main.py "Trim Castle, Trim, Co. Meath" --save
```

`--save` writes a JSON + Markdown report to `./output/`. `--quiet` drops the
DEBUG-level logging (shown by default) down to INFO only.

## Run (web UI)

```bash
python -m sitescout.webapp
```

Then open http://127.0.0.1:5000. It's a Flask app that drives the same
`sitescout` pipeline as the CLI — the browser only ever talks to this app's
own `/api/scout` endpoint, never to Autoaddress/Google/ArcGIS directly (see
`sitescout/webapp.py` and CLAUDE.md for why). `ireland-site-scout-demo.html`
in the repo root is the earlier browser-only prototype this replaced —
kept for reference, not meant to be opened directly (it calls those APIs
straight from client JS with embedded keys, which is exactly what this
Flask version avoids).

## Deploy (Railway)

The web UI is ready to deploy as-is — `Procfile` and `requirements.txt`
(which includes `gunicorn`) are already set up for it:

```
web: gunicorn sitescout.webapp:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 90
```

Railway auto-detects Python via `requirements.txt` and picks up the
`Procfile` for the start command — no other config needed. In the Railway
project's **Variables** tab, set:

- `AUTOADDRESS_KEY`
- `GOOGLE_MAPS_API_KEY`

(the same two keys from `.env` — Railway env vars work the same way
`config.py`'s `os.environ.get(...)` already reads locally, `.env` itself is
never deployed since it's gitignored). Without `AUTOADDRESS_KEY` set, the
app still starts but every `/api/scout` request fails with a clear 500;
without `GOOGLE_MAPS_API_KEY`, geocoding falls back to Nominatim only.

`--timeout 90` is deliberately generous: a single `/api/scout` request
makes ~10 sequential calls to external government/ArcGIS/GeoServer
endpoints (see `pipeline.py`), which can add up under cold/slow upstream
conditions. `--workers 2 --threads 4` gives some concurrency headroom for
more than one visitor at a time without adding a dependency beyond
gunicorn's built-in `gthread` worker class.
