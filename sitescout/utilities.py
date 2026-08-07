"""Utilities (electricity, water, wastewater) — NOT open data in Ireland.

ESB Networks (electricity) and Uisce Éireann (water/wastewater) don't
publish underground asset maps as open data for security reasons. This
module doesn't pretend otherwise — it drafts the actual request emails a
planner would send, pre-filled with the site's coordinates and label.
"""
from __future__ import annotations

import logging

log = logging.getLogger("sitescout.utilities")

ESB_EMAIL = "dig@esb.ie"
WATER_EMAIL = "datarequests@water.ie"


def draft_requests(lat: float, lon: float, label: str) -> dict:
    log.info("Drafting utility data requests (no open API exists for these)…")
    esb_body = (
        f"Hi,\n\nPlease could you send the underground electricity cable maps "
        f"for the following site:\n\nAddress: {label}\n"
        f"Coordinates: {lat:.6f}, {lon:.6f}\n\nThanks."
    )
    water_body = (
        f"Hi,\n\nPlease could you send high-level maps of known water and "
        f"wastewater assets near the following site:\n\nAddress: {label}\n"
        f"Coordinates: {lat:.6f}, {lon:.6f}\n\nThanks."
    )
    return {
        "electricity": {
            "to": ESB_EMAIL,
            "subject": "Dial Before You Dig - cable map request",
            "body": esb_body,
        },
        "water_wastewater": {
            "to": WATER_EMAIL,
            "subject": "Water & wastewater asset map request",
            "body": water_body,
        },
        "note": "Neither ESB Networks nor Uisce Éireann publish these as open "
                "data/APIs — this is a request-based process today.",
    }
