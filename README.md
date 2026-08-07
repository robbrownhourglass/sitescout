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
