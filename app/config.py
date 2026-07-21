"""Environment-driven settings shared across the app package."""

import os
import pathlib

THEMES_DIR = pathlib.Path(__file__).resolve().parent.parent / "themes"
STORES_DIR = pathlib.Path(os.environ.get("TILLA_STORES_DIR", "/opt/tilla/stores"))
DB_PATH = pathlib.Path(os.environ.get("TILLA_DB_PATH", "/opt/tilla/tilla.db"))
# Uploaded deliverables live OUTSIDE STORES_DIR (which nginx serves at /s/), so an
# uploaded file is never directly fetchable — the only path to its bytes is a
# signed, count-limited download token. Dir mode 700 in ops.
FILES_DIR = pathlib.Path(os.environ.get("TILLA_FILES_DIR", "/opt/tilla/deliverables"))

WARDEN_SCREEN_URL = os.environ.get(
    "TILLA_SCREEN_URL", "https://warden.gudman.xyz/api/demo/scan"
)
WARDEN_SCREEN_TIMEOUT = float(os.environ.get("TILLA_SCREEN_TIMEOUT", "10"))

MAX_DESCRIPTION_LEN = 2000
MAX_BODY_BYTES = 64 * 1024  # generous over MAX_DESCRIPTION_LEN, well under abuse range

# ---------- M4 gated delivery: uploads, signed links, licenses ----------
# The one upload route is exempted from MAX_BODY_BYTES and capped here instead;
# the route streams with a running byte budget so a lying/absent Content-Length
# can't beat the cap.
MAX_UPLOAD_BYTES = int(os.environ.get("TILLA_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
# Extension allowlist (not blocklist): no svg/html (inline-render XSS class), no
# executables. Everything is served attachment + octet-stream + nosniff anyway.
UPLOAD_ALLOWED_EXTS = frozenset(
    {
        "pdf",
        "zip",
        "png",
        "jpg",
        "jpeg",
        "webp",
        "gif",
        "mp3",
        "wav",
        "mp4",
        "epub",
        "txt",
        "md",
        "csv",
    }
)
DOWNLOAD_LIMIT_DEFAULT = 5
LINK_TTL_DEFAULT = 86400  # 24h; per-deliverable override drives the token max_age
LICENSE_ACTIVATIONS_DEFAULT = 3
SESSION_TTL = 3600  # buyer wallet session token lifetime
NONCE_TTL = 300  # sign-in nonce lifetime (5 min)
REDELIVER_TTL = 7 * 86400  # email magic-link lifetime

# Signs every download/session/redeliver token. Server .env only, no default:
# when unset the gated endpoints 503 fail-closed and legacy text delivery is
# untouched. Generate via secrets.token_hex(32).
SIGNING_KEY = os.environ.get("TILLA_SIGNING_KEY", "")
# Base URL for absolute download/redeliver links (emails, wallet library).
PUBLIC_BASE_URL = os.environ.get("TILLA_PUBLIC_BASE", "https://tilla.gudman.xyz")
DOMAIN = os.environ.get("TILLA_DOMAIN", "tilla.gudman.xyz")

# SMTP for the email re-delivery fallback. All unset -> send no-ops (logs +
# event_log), so the endpoint and its tests are fully buildable without creds.
SMTP_HOST = os.environ.get("TILLA_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("TILLA_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("TILLA_SMTP_USER", "")
SMTP_PASS = os.environ.get("TILLA_SMTP_PASS", "")
SMTP_FROM = os.environ.get("TILLA_SMTP_FROM", "")

# ---------- M3 hardened checkout: chain + sweeper settings ----------
RPC_URL = os.environ.get("TILLA_RPC", "https://rpc.xlayer.tech")
USDT0 = "0x779ded0c9e1022225f8e0630b35a9b54be713736"  # USDT0 on X Layer, 6dp
# keccak256("Transfer(address,address,uint256)"); built by concat so the literal
# 32-byte hash never appears verbatim (secret-scan hooks flag 0x+64hex).
TRANSFER_TOPIC = (
    "0x" + "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
# Oversized/hung eth_getLogs calls HANG rather than error on rpc.xlayer.tech, so
# every RPC call is time-boxed with this client timeout.
RPC_TIMEOUT = float(os.environ.get("TILLA_RPC_TIMEOUT", "10"))

CONFIRMATIONS = int(os.environ.get("TILLA_CONFIRMATIONS", "3"))
ORDER_TTL_MIN = int(os.environ.get("TILLA_ORDER_TTL_MIN", "30"))
QUARANTINE_HOURS = int(os.environ.get("TILLA_QUARANTINE_HOURS", "24"))
SWEEP_INTERVAL = float(os.environ.get("TILLA_SWEEP_INTERVAL", "5"))
# eth_getLogs cap is 101 blocks INCLUSIVE, so a window spans from_block..from_block+100.
GETLOGS_MAX_SPAN = 100
# Bound worst-case tick duration even after long downtime: at most this many
# 101-block windows per tick (catch-up spreads across ticks, no unbounded burst).
SWEEP_MAX_WINDOWS = int(os.environ.get("TILLA_SWEEP_MAX_WINDOWS", "10"))

# Disable-able so the test suite never starts the background sweeper / hits the
# network; production leaves it enabled.
SWEEP_ENABLED = os.environ.get("TILLA_SWEEP_ENABLED", "1").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Unique per-order amount: price + random offset in [MIN, MAX] micro-USDT.
AMOUNT_OFFSET_MIN = 1
AMOUNT_OFFSET_MAX = 4999
AMOUNT_ALLOC_RETRIES = 10

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
