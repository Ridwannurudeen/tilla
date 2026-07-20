"""Environment-driven settings shared across the app package."""

import os
import pathlib

THEMES_DIR = pathlib.Path(__file__).resolve().parent.parent / "themes"
STORES_DIR = pathlib.Path(os.environ.get("TILLA_STORES_DIR", "/opt/tilla/stores"))
DB_PATH = pathlib.Path(os.environ.get("TILLA_DB_PATH", "/opt/tilla/tilla.db"))

WARDEN_SCREEN_URL = os.environ.get(
    "TILLA_SCREEN_URL", "https://warden.gudman.xyz/api/demo/scan"
)
WARDEN_SCREEN_TIMEOUT = float(os.environ.get("TILLA_SCREEN_TIMEOUT", "10"))

MAX_DESCRIPTION_LEN = 2000
MAX_BODY_BYTES = 64 * 1024  # generous over MAX_DESCRIPTION_LEN, well under abuse range

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
SLUG_MAX_LEN = 40  # keep in sync with SLUG_PATTERN's length bound

# Reserved so a merchant's generated slug can never collide with an app route
# or a future well-known path.
RESERVED_SLUGS = frozenset(
    {
        "api",
        "s",
        "health",
        "create-store",
        "files",
        "admin",
        "static",
        "docs",
        "openapi",
        "openapi.json",
        "redoc",
        "favicon.ico",
        "robots.txt",
        "well-known",
    }
)
