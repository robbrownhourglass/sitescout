"""Command-line entry point.

Usage:
    python main.py "R32 E4F8"
    python main.py "Trim Castle, Trim, Co. Meath" --save
    python main.py "D02 AF30" --quiet --save
"""
from __future__ import annotations

import argparse
import sys

from . import config, autoaddress, geocode, pipeline, report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ireland Site Scout")
    parser.add_argument("query", help="Eircode or address, e.g. 'R32 E4F8'")
    parser.add_argument("--save", action="store_true", help="Save JSON + Markdown report to ./output/")
    parser.add_argument("--quiet", action="store_true", help="Only show INFO-level logs, not DEBUG")
    args = parser.parse_args(argv)

    log = config.setup_logging(verbose=not args.quiet)

    if not config.AUTOADDRESS_KEY:
        log.error("AUTOADDRESS_KEY is not set — copy .env.example to .env and fill it in")
        return 1
    if not config.GOOGLE_MAPS_API_KEY:
        log.warning("GOOGLE_MAPS_API_KEY is not set — geocoding will fall back to Nominatim only")

    try:
        resolved = autoaddress.resolve(args.query)
    except Exception as exc:
        log.error("Could not resolve via Autoaddress: %s", exc)
        log.info("Falling back to geocoding the raw input directly.")
        resolved = autoaddress.ResolvedAddress(address_text=args.query, eircode=None, raw={})

    try:
        geo = geocode.geocode(resolved.eircode, resolved.address_text)
    except Exception as exc:
        log.error("Geocoding failed entirely: %s", exc)
        return 1

    site_report = pipeline.run(args.query, resolved, geo)
    report.print_report(site_report)

    if args.save:
        report.save_report(site_report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
