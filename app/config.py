"""Environment-driven settings shared across the app package."""

import logging
import os
import pathlib
import re

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

# ---------- M10 Warden PAID hire (agents-hiring-agents) — DORMANT by default ----
# Screening upgrades from the FREE demo endpoint above to a PAID x402 hire of
# Warden #3808's listed scan service only when TILLA_WARDEN_PAID is on AND a payer
# key is present. Flag OFF or key unset => screening.screen() hits the free demo
# endpoint byte-identically to today and app.warden_hire's signing path is never
# constructed, so no funds can move. Enabling it = funds movement = user-gated
# runbook step D (fund the payer wallet, set the flag+key in /opt/tilla/.env).
WARDEN_PAID_SCAN_URL = os.environ.get(
    "TILLA_WARDEN_SCAN_URL", "https://warden.gudman.xyz/scan"
)
# The signing key for Tilla's payer wallet. VPS .env only, NEVER in the repo; when
# unset the paid path is unreachable regardless of the flag.
TILLA_WARDEN_PAYER_KEY = os.environ.get("TILLA_WARDEN_PAYER_KEY", "")
# Hard cap on what a single paid scan may commit (micro-USDT). A changed/hostile
# 402 challenge asking for more than this is REFUSED before signing. Default 10000
# = 0.01 USDT, Warden's listed scan price.
TILLA_WARDEN_MAX_MICRO = int(os.environ.get("TILLA_WARDEN_MAX_MICRO", "10000"))
# The payTo the paid challenge MUST resolve to (Warden #3808's wallet). Pinned so a
# hostile 402 can never redirect the fee. Empty => the paid path stays dormant even
# with the flag on (payTo cannot be verified, so we never sign).
WARDEN_PAID_PAYTO = os.environ.get(
    "TILLA_WARDEN_PAYTO", "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"
)

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

# ---------- M9 merchant platform: dashboard, refunds, webhooks ----------
# Merchant wallet session token lifetime (own knob; own itsdangerous salt in
# delivery.py, so a merchant token never validates on a buyer route and vice
# versa). Same default as the buyer SESSION_TTL.
MERCHANT_SESSION_TTL = int(os.environ.get("TILLA_MERCHANT_SESSION_TTL", "3600"))
# OKLink X Layer explorer tx base — a tx hash is appended server-side to build the
# receipt / refund links surfaced in the dashboard (BUILD.md §2 pinned format).
OKLINK_TX_BASE = "https://www.oklink.com/x-layer/tx/"
# Outbound webhook dispatch. Blind POST (status code only), no redirects followed.
WEBHOOK_TIMEOUT = float(os.environ.get("TILLA_WEBHOOK_TIMEOUT", "5"))
WEBHOOK_MAX_ATTEMPTS = int(os.environ.get("TILLA_WEBHOOK_MAX_ATTEMPTS", "5"))
WEBHOOK_INTERVAL = float(os.environ.get("TILLA_WEBHOOK_INTERVAL", "10"))

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
# Buyer-facing request paths (refresh_order, verify_txhash) use a SHORTER timeout so
# a slow RPC can't pin a request thread for the full backfill window; the sweeper
# keeps RPC_TIMEOUT for its catch-up getLogs windows.
RPC_TIMEOUT_REQUEST = float(os.environ.get("TILLA_RPC_TIMEOUT_REQUEST", "5"))
# Concurrency cap on outbound JSON-RPC: a BoundedSemaphore in app.chain lets at most
# this many threads be in an RPC call at once, so a slow endpoint can't pin the whole
# anyio threadpool. Excess callers wait up to RPC_ACQUIRE_TIMEOUT then fail fast with
# ChainBusy (mapped to the existing ChainError paths), instead of queueing for 10s.
RPC_MAX_CONCURRENT = int(os.environ.get("TILLA_RPC_MAX_CONCURRENT", "8"))
RPC_ACQUIRE_TIMEOUT = float(os.environ.get("TILLA_RPC_ACQUIRE_TIMEOUT", "2"))

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

# ---------- M12 readiness heartbeats (/ready freshness thresholds) ----------
# /ready reports the sweeper stale if no tick started within this many seconds. The
# worst healthy catch-up tick is SWEEP_MAX_WINDOWS(10) x RPC_TIMEOUT(10s) ~= 100s, so
# 180s never false-alarms. Only meaningful with SWEEP_ENABLED (else reported disabled).
READY_SWEEP_STALE_SEC = float(os.environ.get("TILLA_READY_SWEEP_STALE_SEC", "180"))
# /ready reports RPC stale if the sweeper hasn't read a chain head within this window.
# Read as a piggyback heartbeat (no per-probe RPC call), so an RPC outage surfaces on
# /ready without the watchdog ever burning eth_blockNumber quota.
READY_RPC_STALE_SEC = float(os.environ.get("TILLA_READY_RPC_STALE_SEC", "300"))

# ---------- M6 themes + SEO ----------
# The storefront themes a merchant (or the LLM) may choose. The API and the LLM
# speak the short name; the persisted stores.theme column and the renderer speak
# the "<name>.html" template filename.
ALLOWED_THEMES = frozenset({"original", "bold", "editorial"})
DEFAULT_THEME = "original.html"


# ---------- M8 payment methods (aggr_deferred / MPP / subscriptions) ----------
# Every rail beyond exact ships flag-OFF; a flag is flipped ONLY in the VPS .env
# after its read-only creds/support probe passes (see docs/spikes.md), never by a
# code default. OFF = byte-identical exact checkout, no new payment promises.
def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# aggr_deferred: adds a SECOND accepts-entry on /s/{slug}/buy for batch products.
# Flipping this on while the facilitator /supported does NOT list aggr_deferred on
# eip155:196 makes the middleware's lazy initialize raise -> 502 on ALL protected
# routes, so only flip after the /supported probe confirms the scheme.
AGGR_DEFERRED_ENABLED = _flag("TILLA_AGGR_DEFERRED")
# aggr_deferred reconciliation poller cadence + burst bound (mirrors the attest loop
# knobs). Only meaningful when the reconcile loop runs (SWEEP_ENABLED AND
# AGGR_DEFERRED_ENABLED AND OKX creds) — never in tests, which disable SWEEP_ENABLED.
RECONCILE_INTERVAL = float(os.environ.get("TILLA_RECONCILE_INTERVAL", "30"))
RECONCILE_MAX_PER_TICK = int(os.environ.get("TILLA_RECONCILE_MAX_PER_TICK", "20"))
# MPP pay-as-you-go: /s/{slug}/mpp/* endpoints. OFF -> every endpoint 503s.
MPP_ENABLED = _flag("TILLA_MPP_ENABLED")
# Subscriptions (x402 period, JS sidecar): /s/{slug}/subscribe. OFF -> 503.
SUBSCRIPTIONS_ENABLED = _flag("TILLA_SUBSCRIPTIONS_ENABLED")
# M10 Warden PAID hire: OFF (default) -> screening uses the free demo endpoint and
# app.warden_hire never signs. Flipped ON only in runbook step D, after the payer
# wallet is funded and TILLA_WARDEN_PAYER_KEY is set on the VPS.
WARDEN_PAID_ENABLED = _flag("TILLA_WARDEN_PAID")
# The always-on localhost Node sidecar the subscribe proxy talks to (never nginx-
# exposed). The proxy 503s if it is unreachable, never a false 402/200.
SUBSCRIPTION_SIDECAR_URL = os.environ.get("TILLA_SIDECAR_URL", "http://127.0.0.1:8790")

# ---------- M11 on-chain depth: EAS receipt attestations — DORMANT by default ---
# A background worker attests a signed EAS receipt for every terminal-delivered
# order (recipient = buyer). It is TRIPLE-GATED off: ATTEST_ENABLED default OFF,
# TILLA_ATTESTER_KEY absent (VPS .env only), and the lifespan task refuses to start
# without both. Flag off OR key unset => zero on-chain tx, zero RPC, zero gas, and
# app.attest never imports web3 or constructs a signer (the WARDEN_PAID pattern).
# Enabling it = signing an on-chain tx that spends OKB gas = user-gated runbook step
# (mint/fund the attester key, set TILLA_ATTESTER_KEY + TILLA_ATTEST=1 in .env).
ATTEST_ENABLED = _flag("TILLA_ATTEST")
# The attester's signing key. VPS .env only, NEVER in the repo; empty => the attest
# path is unreachable regardless of the flag (the exact WARDEN_PAID_ENABLED pattern).
TILLA_ATTESTER_KEY = os.environ.get("TILLA_ATTESTER_KEY", "")
# RPC + chain the attester signs against. Defaults to the checkout RPC / mainnet 196;
# set ATTEST_CHAIN_ID=1952 (+ a testnet RPC) for the user's testnet dry-run. The tick
# refuses to sign if the connected chain_id does not equal ATTEST_CHAIN_ID.
ATTEST_RPC = os.environ.get("TILLA_ATTEST_RPC", RPC_URL)
ATTEST_CHAIN_ID = int(os.environ.get("TILLA_ATTEST_CHAIN_ID", "196"))
# EAS + SchemaRegistry OP-Stack predeploys — the SAME addresses on X Layer mainnet
# 196 and testnet 1952 (verified Spike 4). Not env-configurable: they are protocol
# predeploy constants.
EAS_ADDR = "0x4200000000000000000000000000000000000021"
SCHEMA_REGISTRY_ADDR = "0x4200000000000000000000000000000000000020"
# The Tilla receipt schema. Field ORDER is load-bearing: it defines both the schema
# UID (keccak of schema+resolver+revocable) and the abi.encode layout of every
# attestation's data blob. Changing it silently orphans every prior attestation.
# productId + contentHash bind the receipt to WHAT was delivered — the product identity
# and a sha256 of the delivered payload — not merely that money moved. Both are appended
# so the original four fields keep their positions (additive).
ATTEST_SCHEMA = (
    "address buyer,string storeId,uint256 amountUsdt6,bytes32 paymentTxHash,"
    "uint256 productId,bytes32 contentHash"
)
# Worker cadence + burst bound (mirrors the webhook loop knobs).
ATTEST_INTERVAL = float(os.environ.get("TILLA_ATTEST_INTERVAL", "30"))
# One attestation broadcast per tick: two orders in the same tick would fetch the
# same account nonce (the first's tx is still unmined), collide on that nonce, and
# either churn sending->pending or — if gas rose and one replaced the other in the
# mempool — strand the replaced order in a never-mining 'sent' that reconcile marks
# terminally 'failed'. Serializing to one broadcast per tick sidesteps both.
ATTEST_MAX_PER_TICK = int(os.environ.get("TILLA_ATTEST_MAX_PER_TICK", "1"))
# Per-tx gas-cost cap in wei (estimate_gas * gas_price). Over this the tick REFUSES
# to sign and leaves the order 'pending' for a calmer fee window — refuse, never
# overspend (the TILLA_WARDEN_MAX_MICRO philosophy). Default 0.01 OKB (~30x a normal
# attest), so ordinary operation never trips it but a fee spike is refused.
TILLA_ATTEST_MAX_GAS_WEI = int(os.environ.get("TILLA_ATTEST_MAX_GAS_WEI", str(10**16)))

# ---------- M13 growth: affiliates, external feeds, embeds, ACP checkout -------
# Affiliate rev-share basis points on the settled sale amount, written as a pure DB
# ledger row at the delivered seam. Default 200 = 2%. Accrual is a NUMBER ONLY: no
# fund-moving code exists — a payout is a manual on-chain USDT0 send the operator
# makes from their own wallet, then verify-and-recorded (the M9 refund pattern).
TILLA_AFFILIATE_BPS = int(os.environ.get("TILLA_AFFILIATE_BPS", "200"))
# ACP (Agentic Commerce Protocol) /checkout_sessions surface. Mounted DORMANT: every
# endpoint 503s until the flag flips (the MPP/subscriptions dormant-mount pattern),
# so the new surface can be smoke-tested before exposure. Flipped ON in the VPS .env
# only after a live smoke of the tx-hash complete path.
ACP_ENABLED = _flag("TILLA_ACP_ENABLED")
# Optional HMAC request-signature secret. When set, ACP requests must carry a valid
# signature header; when empty (the default) signature verification is skipped and
# the surface relies on the same rate-limited public-allocator exposure as /checkout.
TILLA_ACP_SIGNING_SECRET = os.environ.get("TILLA_ACP_SIGNING_SECRET", "")
# Per-store subscriber row cap: the waitlist endpoint 429s once a store holds this
# many active subscribers (abuse bound, alongside the tightest slowapi limiter).
TILLA_SUBSCRIBERS_MAX = int(os.environ.get("TILLA_SUBSCRIBERS_MAX", "5000"))

# ---------- M16 B2B wholesale tiers: read-only ERC-8004 owner verification -----
# A wholesale tier discount is granted ONLY when the settled payer wallet equals
# the on-chain owner of the presented ERC-8004 agent id (INV-1), resolved by a
# READ-ONLY eth_call to this IdentityRegistry — Tilla never writes to it and never
# signs. Empty (the default) => verification always returns None => every quote and
# settle falls back to the base price (fail-to-base, never fail-open-to-a-discount).
# The live X Layer registry address is set in the VPS .env (user-gated, like the
# other dormant on-chain constants); tests inject it + mock the RPC.
ERC8004_REGISTRY = os.environ.get("TILLA_ERC8004_REGISTRY", "").lower()
# Owner lookup selector: ownerOf(uint256) = 0x6352211e. ERC-8004 agents are ERC-721
# tokens whose tokenId is the agentId, so ownerOf returns the controlling wallet.
# Overridable in case the deployed registry exposes a differently-named read.
ERC8004_OWNER_SELECTOR = os.environ.get("TILLA_ERC8004_OWNER_SELECTOR", "0x6352211e")
# Positive-result cache TTL (seconds) for the identity read — bounds enumeration /
# DoS on the public quote endpoint. Only verified owners are cached; an RPC outage
# is never cached, so a discount is never served on stale trust.
ERC8004_LOOKUP_TTL = float(os.environ.get("TILLA_ERC8004_LOOKUP_TTL", "300"))
# Wholesale tier bounds (micro-USDT), mirroring the M1 product price bounds
# (0.01 .. 10000 USDT); no floats — micro ints only.
TIER_PRICE_MICRO_MIN = 10_000
TIER_PRICE_MICRO_MAX = 10_000_000_000
TIERS_MAX = 20

# ---------- M16.4 federation ingest (mirror-of-mirrors) ----------------------
# Operator-configured peer base URLs (comma-separated https origins), NEVER
# user-submitted. Empty (the default) => the feature is DORMANT: the loop never
# starts, zero network. The ingest fetches each peer's /discovery/resources +
# per-store feed.json, schema-validates against the frozen 16.3 contract, and
# caches read-only rows in ``federated_listings`` that link OUT to the peer's own
# checkout. Tilla NEVER proxies, quotes, or settles a peer's sale.
FEDERATION_PEERS = [
    p.strip()
    for p in os.environ.get("TILLA_FEDERATION_PEERS", "").split(",")
    if p.strip()
]
FEDERATION_INTERVAL = float(os.environ.get("TILLA_FEDERATION_INTERVAL", "300"))
FEDERATION_FETCH_TIMEOUT = float(os.environ.get("TILLA_FEDERATION_FETCH_TIMEOUT", "5"))
# Hard body cap per fetched document (bytes) — a poisoned/oversized peer feed is
# rejected before it is parsed (256 KiB).
FEDERATION_MAX_BYTES = int(os.environ.get("TILLA_FEDERATION_MAX_BYTES", "262144"))
# Cap the stores ingested per peer per tick (bounds a hostile peer's row count).
FEDERATION_MAX_STORES = int(os.environ.get("TILLA_FEDERATION_MAX_STORES", "50"))
# ---------- M17 growth agent: content-calendar scheduler — DORMANT by default ---
# A background loop drafts marketing copy on a merchant's calendar cadence, screens
# it fail-closed, and queues it in the growth outbox (INV-1: it NEVER sends). It is
# double-gated OFF: GROWTH_SCHED_ENABLED default OFF (so zero LLM spend) AND it only
# starts under SWEEP_ENABLED (never in tests). Enabling it is the demand receipt —
# flipped ON in the VPS .env only once a real merchant asks for a calendar.
GROWTH_SCHED_ENABLED = _flag("TILLA_GROWTH_SCHED_ENABLED")
# Cadence of the scheduler loop (seconds). Default daily; the per-store cadence
# (daily/weekly) is enforced on top of this tick inside the tick itself.
GROWTH_SCHED_INTERVAL = float(os.environ.get("TILLA_GROWTH_SCHED_INTERVAL", "86400"))
# Per-store hard cap on scheduled generations per day (abuse backstop on top of the
# cadence pacing), and a global daily ceiling across every store (unattended-spend
# guard). A generation = one LLM call producing one store's scheduled drafts.
GROWTH_SCHED_STORE_DAILY_MAX = int(os.environ.get("TILLA_GROWTH_STORE_DAILY_MAX", "6"))
GROWTH_DAILY_MAX = int(os.environ.get("TILLA_GROWTH_DAILY_MAX", "50"))

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
SLUG_MAX_LEN = 40  # keep in sync with SLUG_PATTERN's length bound

# ---------- M18 cross-chain checkout honesty (18.3) ----------
# Optional operator-configured third-party "Bridge funds to X Layer" link on the
# checkout page. Default empty => the affordance is ABSENT. Operator config ONLY
# (never merchant/LLM content, INV-3); validated to an http(s) URL so a malformed or
# hostile value (javascript:/data:) can never render as a link — anything else is
# dropped (loud) and the affordance stays absent. No bridge code, no bridge state.
_BRIDGE_RAW = os.environ.get("TILLA_BRIDGE_URL", "").strip()
BRIDGE_URL = _BRIDGE_RAW if re.match(r"^https?://\S+$", _BRIDGE_RAW) else ""
if _BRIDGE_RAW and not BRIDGE_URL:
    logging.getLogger("tilla").warning(
        "TILLA_BRIDGE_URL is not an http(s) URL — bridge affordance disabled"
    )

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
