"""Configuration: API keys and shared constants, loaded from environment."""
import os
import logging
from dotenv import load_dotenv

load_dotenv()

AUTOADDRESS_KEY = os.environ.get("AUTOADDRESS_KEY", "")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

USER_AGENT = "ireland-site-scout/0.1 (+local dev tool)"

# Standard requests timeout (seconds) for all external calls.
HTTP_TIMEOUT = 15


def setup_logging(verbose: bool = True) -> logging.Logger:
    """Configure a logger that prints each step to the console as it happens
    — this is the whole point of moving off the browser demo: you can see
    exactly what's being called and what came back, in real time, in the
    terminal, instead of guessing at silent browser/JS failures.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("sitescout")
