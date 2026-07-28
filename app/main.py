#!/usr/bin/env python3
"""Tilla backend.
- ASP endpoint: POST/GET /create-store  (x402-gated; an agent pays Tilla to spin up a live store)
- Store checkout: /api/checkout/*  (buyer pays the merchant in USDT on X Layer; balanceOf verification)
Run: uvicorn app.main:app --host 127.0.0.1 --port 8040   (EnvironmentFile=/opt/tilla/.env)
"""

import asyncio
import contextlib
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal
from xml.sax.saxutils import escape

import httpx
from fastapi import Depends, FastAPI, HTTPException, Path, Request
from itsdangerous import BadSignature, SignatureExpired
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app import (
    acp,
    affiliates,
    agentic,
    attest,
    chain,
    checkout,
    config,
    dashboard,
    delivery,
    embed,
    escrow,
    evidence,
    external_feeds,
    federation,
    growth,
    growth_scheduler,
    mpp,
    payment,
    providers,
    reconcile,
    render,
    subscriptions,
    webhooks,
)
from app.checkout import DEFAULT_DELIVERY
from app.db import (
    SessionLocal,
    current_migration_head,
    expected_migration_head,
    get_session,
)
from app.engine import GenerationUnavailable
from app.engine import create_store as gen_store
from app.engine import rerender_stores, resume_pending, resync_catalog, upgrade_store
from app.limiter import limiter
from app.models import (
    Deliverable,
    Delivery,
    EmailSubscriber,
    Entitlement,
    Merchant,
    Order,
    Product,
    Review,
    ScreeningReceipt,
    Store,
    log_event,
)
from app.screening import ScreeningBlocked, ScreeningUnavailable, screen

logger = logging.getLogger("tilla")
# The app never configured logging, so every logger.info() was silently dropped in
# production — the per-store LLM token spend and the background loops' start lines
# among them, which meant a deploy could not be confirmed from the journal. TWO
# gates were closed: "tilla" sat at NOTSET inheriting root's WARNING, AND neither
# it nor root had any handler, so records fell through to logging.lastResort, which
# is hardwired at WARNING (uvicorn configures only its own uvicorn* loggers). Both
# have to open, hence the handler as well as the level. Attached here rather than
# via basicConfig so INFO is enabled for this app alone — a root handler would also
# uncork httpx/sqlalchemy/web3 chatter onto a box shared with ~18 other projects.
logger.setLevel(logging.INFO)
if not logger.handlers:
    _log_handler = logging.StreamHandler()  # stderr -> journald under systemd
    _log_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    logger.addHandler(_log_handler)

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
# The one route exempted from the 64KB body cap (streamed upload, capped at
# config.MAX_UPLOAD_BYTES instead). The route's own Path validator enforces the
# real slug pattern; here we only need to recognise the shape.
_UPLOAD_PATH_RE = re.compile(r"^/api/stores/[^/]+/deliverable$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Give the default threadpool executor deterministic capacity (16 workers). The
    # asyncio default is min(32, cpu+4) (~8 on this VPS) and is shared by every
    # asyncio.to_thread caller — the sweeper/webhook/attest/reaper ticks AND the M7
    # x402 resolver hooks — so under a burst the buy route's 5s hook_timeout could
    # trip on queueing alone. Sizing it explicitly is transparent to all callers.
    worker_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tilla-worker")
    asyncio.get_running_loop().set_default_executor(worker_pool)
    # Retry any stores held in pending_screening from before this process
    # started, so a restart flips recovered ones live instead of stranding them.
    resume_pending()
    # Re-render every live store's static index.html from its persisted content so
    # a theme fix (e.g. the exact-amount checkout row) reaches already-deployed
    # pages on the next restart, instead of leaving them serving stale HTML.
    rerender_stores()
    # In-process, restart-safe payment sweeper. Disabled via TILLA_SWEEP_ENABLED=0
    # so the test suite never touches the network.
    sweeper = None
    webhook_task = None
    attest_task = None
    reconcile_task = None
    federation_task = None
    growth_sched_task = None
    if config.SWEEP_ENABLED:
        sweeper = asyncio.create_task(checkout.sweeper_loop())
        # M9 outbound webhook dispatcher — shares the sweeper's background-loop gate
        # so the test suite never starts it or hits the network. Idle until a
        # merchant registers a webhook URL.
        webhook_task = asyncio.create_task(webhooks.webhook_loop())
        # M11 EAS attester — TRIPLE-GATED dormant: only starts with SWEEP_ENABLED AND
        # ATTEST_ENABLED AND a signing key. Missing any one => no task, no RPC, no
        # gas, and app.attest never constructs a Web3/Account. Enabling it (key + OKB
        # + flag on the VPS) is an explicit user-gated runbook step.
        if config.ATTEST_ENABLED and config.TILLA_ATTESTER_KEY:
            attest_task = asyncio.create_task(attest.attest_loop())
        # M8 aggr_deferred settle reconciliation poller — DORMANT: only starts with
        # SWEEP_ENABLED AND AGGR_DEFERRED_ENABLED AND OKX creds. Missing any one => no
        # task, no RPC, and the SDK facilitator client is never built. It finalizes
        # deferred (async) settles once their aggregated tx confirms; idle until a
        # batch buy leaves an order in 'settling' with a pending settle_ref.
        if config.AGGR_DEFERRED_ENABLED and os.getenv("OKX_API_KEY"):
            reconcile_task = asyncio.create_task(reconcile.reconcile_loop())
        # M16.4 federation ingest — shares the background-loop gate so tests never
        # start it. DOUBLY dormant: also a no-op unless TILLA_FEDERATION_PEERS is
        # set (empty => the loop idles with zero network). Read-only discovery
        # mirror; never moves funds.
        if config.FEDERATION_PEERS:
            federation_task = asyncio.create_task(federation.federation_loop())
        # M17.3 content-calendar scheduler — DORMANT: only under SWEEP_ENABLED AND
        # TILLA_GROWTH_SCHED_ENABLED (default OFF), so zero LLM spend by default and
        # never started in tests. It drafts + screens + queues only; sends nothing.
        if config.GROWTH_SCHED_ENABLED:
            growth_sched_task = asyncio.create_task(
                growth_scheduler.growth_scheduler_loop()
            )
    # x402 agent commerce (prod only): pre-warm the facilitator /supported off-loop
    # so the first protected request doesn't do the blocking sync GET, and run the
    # reaper that voids agent orders stuck delivered-without-settle.
    reaper = None
    if os.getenv("OKX_API_KEY"):
        await _prewarm_facilitator()
        await _probe_supported()
        reaper = asyncio.create_task(agentic.agent_reaper_loop())
    try:
        yield
    finally:
        for task in (
            sweeper,
            webhook_task,
            attest_task,
            reconcile_task,
            reaper,
            federation_task,
            growth_sched_task,
        ):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        # Don't block shutdown on in-flight worker threads (RPC/hook calls are
        # already time-boxed); let them drain as the process exits.
        worker_pool.shutdown(wait=False)


async def _prewarm_facilitator() -> None:
    """Run the blocking sync facilitator GET /supported at boot, off the event
    loop, then swap ``_srv.initialize`` to a no-op so the middleware's first
    protected request degrades to in-memory route validation (which needs the
    now-populated _supported_responses). On failure, leave the real method in
    place — the middleware's lazy-init/502-retry fallback is preserved."""
    srv = globals().get("_srv")
    if srv is None:
        return
    try:
        await asyncio.to_thread(srv.initialize)
        srv.initialize = lambda: None
        logger.info("x402 facilitator pre-warmed at startup")
    except Exception:
        logger.exception("x402 facilitator pre-warm failed; lazy init will retry")


async def _probe_supported() -> None:
    """18.2 read-only ``/supported`` probe at boot: cache the ``(scheme, network)``
    snapshot the per-chain accepts gate reads, and log it (this increment's live
    artifact). Never raises — a probe failure caches an empty snapshot so the store
    402 stays 196-only, never a crash (196 is grandfathered as the proven rail)."""
    fac = globals().get("_fac")
    if fac is None:
        return
    await asyncio.to_thread(payment.probe_supported, fac)


app = FastAPI(
    title="Tilla",
    description="Storefronts + crypto checkout on X Layer",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.include_router(agentic.router)
# M9 merchant platform: wallet/API-key auth, dashboard read surface, refunds, CSV
# export, webhook config. All routes under /api/merchant/* plus the /dashboard shell.
app.include_router(dashboard.router)
# MPP pay-as-you-go router: always mounted, every endpoint 503s until
# TILLA_MPP_ENABLED + SA creds are set (fail-closed, no SDK import while dormant).
app.include_router(mpp.router)
# Subscription proxy router: always mounted, 503s until TILLA_SUBSCRIPTIONS_ENABLED
# (fail-closed); even then it only settles via the sidecar when OKX creds exist.
app.include_router(subscriptions.router)
# M13 growth routers (all additive, read-only or dormant):
#  - external_feeds: OpenAI/Google export shapes generated from the same rows as M7.
#  - embed: GET /embed.js (the self-contained shadow-DOM buy button asset).
#  - acp: the five ACP /checkout_sessions endpoints, DORMANT-503 until TILLA_ACP_ENABLED.
app.include_router(external_feeds.router)
app.include_router(embed.router)
app.include_router(acp.router)
# Phase 3 growth-kit: merchant-gated POST/GET /api/stores/{slug}/growth-kit. Additive,
# no schema change, no fund-moving or external-posting code (persists to event_log).
app.include_router(growth.router)
# Roadmap Phase 3 escrow: the A2A commissioned-build job machine under /api/escrow/*.
# Always mounted, every endpoint 503s until TILLA_ESCROW is set (the ACP/MPP dormant-
# mount pattern). NON-CUSTODIAL: it tracks state and records verified deposit/release
# txs — no fund-moving code, Tilla never holds keys or sends funds.
app.include_router(escrow.router)


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    """Enforce a body-size budget in-app since FastAPI/Starlette has no
    built-in cap; reads the request in chunks so an unbounded/chunked body
    can't be buffered past the limit before we notice.

    The one multipart upload route is exempted and capped at
    config.MAX_UPLOAD_BYTES instead: its declared Content-Length is checked, then
    it is passed through WITHOUT consuming request.stream() so the route parses it
    streamingly (Starlette spools past 1MB to a temp file → bounded RAM) under its
    own running byte budget that defeats a lying/absent length."""
    if request.method in ("POST", "PUT", "PATCH"):
        is_upload = bool(
            _UPLOAD_PATH_RE.match(request.url.path)
        ) and request.headers.get("content-type", "").startswith("multipart/")
        limit = config.MAX_UPLOAD_BYTES if is_upload else config.MAX_BODY_BYTES
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                return JSONResponse(
                    {"detail": "invalid content-length header"}, status_code=400
                )
            if declared > limit:
                return JSONResponse(
                    {"detail": "request body too large"}, status_code=413
                )
        if is_upload:
            return await call_next(request)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > config.MAX_BODY_BYTES:
                return JSONResponse(
                    {"detail": "request body too large"}, status_code=413
                )
        request._body = body
    return await call_next(request)


@app.get("/health")
def health():
    return {"ok": True, "service": "tilla", "chain": "X Layer (196)"}


@app.get("/ready")
def ready():
    """Readiness probe (unauthenticated, unthrottled, never raises — always JSON,
    200 ready / 503 not). The watchdog polls it every minute. Cost per probe: one
    sqlite SELECT plus in-memory heartbeat reads; NO network call — an RPC blip
    surfaces via the sweeper's piggybacked head heartbeat, not a per-probe RPC."""
    checks: dict[str, str] = {}
    ready_ok = True

    # (1) DB reachable.
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "error"
        ready_ok = False

    # (2) Schema at the expected migration head.
    expected = expected_migration_head()
    if expected == "unknown":
        # Could not compute the expected head (alembic.ini absent) — non-fatal.
        checks["migrations"] = "unknown"
    else:
        try:
            with SessionLocal() as session:
                current = current_migration_head(session)
        except Exception:
            current = None
        if current == expected:
            checks["migrations"] = current
        else:
            checks["migrations"] = f"expected {expected}, got {current}"
            ready_ok = False

    # (3) Sweeper heartbeat + (4) RPC head heartbeat — only meaningful with the
    # sweeper enabled; disabled (tests/dev) reports both as such and never fails.
    if not config.SWEEP_ENABLED:
        checks["sweeper"] = "disabled"
        checks["rpc"] = "disabled"
    else:
        tick_age = time.monotonic() - checkout.LAST_TICK_MONO
        if checkout.LAST_TICK_MONO > 0 and tick_age < config.READY_SWEEP_STALE_SEC:
            checks["sweeper"] = "ok"
        else:
            checks["sweeper"] = "stale"
            ready_ok = False
        head_age = time.monotonic() - checkout.LAST_HEAD_MONO
        if checkout.LAST_HEAD_MONO > 0 and head_age < config.READY_RPC_STALE_SEC:
            checks["rpc"] = "ok"
        else:
            checks["rpc"] = "stale"
            ready_ok = False

    return JSONResponse(
        {"ready": ready_ok, "checks": checks}, status_code=200 if ready_ok else 503
    )


@app.get("/sitemap.xml")
@limiter.limit("60/minute")
def sitemap(request: Request, session: Session = Depends(get_session)):
    """XML sitemap of every LIVE, PUBLIC store URL (pending/blocked stores are excluded
    — their pages aren't served; Phase 1.7 hidden/sandbox stores are excluded too, so
    they stay unindexed). Slugs are a restricted charset, but the <loc> text is
    XML-escaped regardless."""
    base = config.PUBLIC_BASE_URL.rstrip("/")
    slugs = session.scalars(
        select(Store.slug)
        .where(Store.status == "live", Store.visibility == "public")
        .order_by(Store.slug)
    ).all()
    # Hub pages first (nginx-served statics the crawler can't discover from the
    # store URLs alone), then every live store.
    hub = ("/", "/marketplace.html", "/receipt-demo.html", "/library.html")
    urls = "".join(f"<url><loc>{escape(base)}{path}</loc></url>" for path in hub)
    urls += "".join(
        f"<url><loc>{escape(base)}/s/{escape(slug)}/</loc></url>" for slug in slugs
    )
    # Phase 4 link-in-bio: one /m/{address} profile per merchant with at least one
    # live, public store — the SAME visibility filter as the store URLs above, so it
    # surfaces no address a crawler couldn't already read off those stores' checkout
    # pages (every store renders its receive wallet). Addresses are a restricted
    # 0x-hex charset, XML-escaped regardless.
    profile_addrs = session.scalars(
        select(Merchant.wallet_address)
        .join(Store, Store.merchant_id == Merchant.id)
        .where(Store.status == "live", Store.visibility == "public")
        .distinct()
        .order_by(Merchant.wallet_address)
    ).all()
    urls += "".join(
        f"<url><loc>{escape(base)}/m/{escape(addr)}</loc></url>"
        for addr in profile_addrs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/m/{address}")
@limiter.limit("60/minute")
def merchant_profile(
    request: Request,
    address: str = Path(..., min_length=1, max_length=64),
    session: Session = Depends(get_session),
):
    """Phase 4 link-in-bio: a merchant's PUBLIC 'all my stores' profile — their LIVE,
    PUBLIC stores (Phase 1.7 visibility) as one shareable page. Hidden/pending/blocked
    stores are excluded (the same filter as discovery + the sitemap). An unknown
    address, a malformed address, or a merchant with no public stores is a uniform 404
    (no existence oracle). Store names/descriptions are screened LLM copy rendered
    through the autoescaped Jinja env, so they stay inert text; the wallet shown is
    already public (it is every store's on-chain receive address)."""
    addr = address.lower()
    if not _EVM_ADDRESS.fullmatch(addr):
        raise HTTPException(404, "profile not found")
    merchant = session.scalar(select(Merchant).where(Merchant.wallet_address == addr))
    if merchant is None:
        raise HTTPException(404, "profile not found")
    stores = session.scalars(
        select(Store)
        .where(
            Store.merchant_id == merchant.id,
            Store.status == "live",
            Store.visibility == "public",
        )
        .order_by(Store.created_at.desc())
    ).all()
    if not stores:
        raise HTTPException(404, "profile not found")
    items = [
        {
            "slug": s.slug,
            "name": agentic._store_name(s),
            "description": agentic._store_description(s),
            "url": f"/s/{s.slug}/",
        }
        for s in stores
    ]
    return HTMLResponse(render.render_profile(addr, items))


# ---------- Phase 4 custom domains: host-based store resolution ----------
def _request_host(request: Request) -> str:
    """The lowercased Host header without any port. Empty when absent."""
    host = request.headers.get("host", "")
    return host.split(":", 1)[0].strip().lower()


def _platform_subdomain_slug(host: str) -> str | None:
    """The store slug in ``<slug>.tilla.gudman.xyz``, or None if this host is not a
    single-label subdomain of the platform domain.

    Deliberately strict. A store slug reaching this point has already satisfied
    SLUG_PATTERN at creation, but the Host header is attacker-controlled, so the
    label is re-validated here rather than trusted: exactly one label, matching the
    slug pattern, and never a reserved name — otherwise ``api.tilla.gudman.xyz``
    could be made to serve a storefront. The platform host itself returns None so it
    keeps serving the landing page."""
    suffix = "." + config.DOMAIN.lower()
    if not host.endswith(suffix):
        return None
    label = host[: -len(suffix)]
    if not label or "." in label:
        return None
    if label in config.RESERVED_SLUGS:
        return None
    return label if re.fullmatch(config.SLUG_PATTERN, label) else None


def _store_for_host(request: Request, session: Session) -> Store | None:
    """The live store this request's Host resolves to, or None.

    Two ways a host names a store: a VERIFIED custom domain the merchant owns, or a
    ``<slug>.tilla.gudman.xyz`` platform subdomain. Fail-closed throughout — an
    unverified or unclaimed domain, a non-live store, a reserved label, or a
    non-matching host all yield None and the caller 404s, so a host header can never
    conjure a storefront that is not live."""
    host = _request_host(request)
    if not host:
        return None
    store = session.scalar(
        select(Store).where(
            Store.custom_domain == host,
            Store.custom_domain_verified_at.isnot(None),
            Store.status == "live",
        )
    )
    if store is not None:
        return store
    slug = _platform_subdomain_slug(host)
    if slug is None:
        return None
    return session.scalar(
        select(Store).where(Store.slug == slug, Store.status == "live")
    )


@app.get("/")
@limiter.limit("120/minute")
def custom_domain_root(request: Request, session: Session = Depends(get_session)):
    """Serve a store on its VERIFIED custom domain. This route is only reached via the
    operator's custom-domain vhost (the platform host serves its landing page from nginx
    statics); a request whose Host is not a verified custom domain is a fail-closed 404.
    The store is rendered through the same autoescaped env as its static page, with
    canonical/OG pointing at the custom domain root."""
    store = _store_for_host(request, session)
    if store is None or not isinstance(store.content, dict):
        raise HTTPException(404, "not found")
    # From the REQUEST host, not store.custom_domain: a platform subdomain store has
    # no custom_domain at all, and reading the column would put the literal "None"
    # into every canonical and Open Graph URL on the page.
    base_url = f"https://{_request_host(request)}"
    html = render.render(
        store.content, store.pay_to, store.slug, store.theme, base_url=base_url
    )
    return HTMLResponse(html)


@app.get("/feed.json")
@limiter.limit("60/minute")
def custom_domain_feed(request: Request, session: Session = Depends(get_session)):
    """The store's product feed at the root of its VERIFIED custom domain.

    The themes advertise the feed relative to the canonical URL
    (``<link rel="alternate" href="{{CANONICAL_URL}}feed.json">``), which resolves to
    ``/s/{slug}/feed.json`` on the platform host but to ``/feed.json`` on a custom
    domain — where nothing served it, so every custom-domain store advertised a 404
    to the crawlers and agents most likely to read it. Serving it here makes the
    theme's relative link correct on both hosts. Same fail-closed host match as
    :func:`custom_domain_root`; the body is the per-store handler's, verbatim."""
    store = _store_for_host(request, session)
    if store is None:
        raise HTTPException(404, "not found")
    return agentic.feed_json(request, slug=store.slug, session=session)


@app.get("/llms.txt")
@limiter.limit("60/minute")
def custom_domain_llms(request: Request, session: Session = Depends(get_session)):
    """The store's llms.txt at the root of its VERIFIED custom domain — the
    conventional location an LLM crawler looks for it. Same reasoning and same
    fail-closed host match as :func:`custom_domain_feed`."""
    store = _store_for_host(request, session)
    if store is None:
        raise HTTPException(404, "not found")
    return agentic.llms_txt(request, slug=store.slug, session=session)


# HEAD is registered alongside GET on both asset routes below. nginx answers HEAD on
# the platform's static `/s/` path, but a store subdomain and a custom domain proxy
# everything here, so without it those hosts answered 405 to the social scrapers,
# link checkers and monitors that probe an asset with HEAD before fetching it.
# Starlette's FileResponse already suppresses the body for HEAD (it sets
# send_header_only from the request method), so one handler serves both correctly.
@app.get("/og.png")
@app.get("/og.svg")
@app.head("/og.png")
@app.head("/og.svg")
def custom_domain_og(request: Request, session: Session = Depends(get_session)):
    """Serve the store's Open Graph card on its verified custom domain, so the OG image
    URL (``https://<domain>/og.png``) referenced by the custom-domain page resolves. The
    asset is the store's own pre-rendered static card; a non-matching Host is a 404."""
    store = _store_for_host(request, session)
    if store is None:
        raise HTTPException(404, "not found")
    name = "og.png" if request.url.path.endswith(".png") else "og.svg"
    asset = config.STORES_DIR / store.slug / name
    if not asset.is_file():
        raise HTTPException(404, "not found")
    media = "image/png" if name == "og.png" else "image/svg+xml"
    return FileResponse(asset, media_type=media)


# A store photograph's filename, exactly as app.imagery writes it: a digest of the
# bytes. Anchored, so the path parameter cannot name anything else in the store dir.
_STORE_IMAGE_NAME = re.compile(r"[0-9a-f]{8,64}\.jpg")


@app.get("/img/{name}")
@app.head("/img/{name}")
def custom_domain_image(
    name: str, request: Request, session: Session = Depends(get_session)
):
    """Serve a store's own photograph on its verified custom domain or its platform
    subdomain.

    On the platform host these files are static: nginx serves STORES_DIR at ``/s/``,
    so ``/s/<slug>/img/<hex>.jpg`` never reaches the app. The wildcard subdomain
    vhost proxies EVERYTHING to the app (deploy/nginx-tilla-wildcard.conf), and a
    custom-domain store is served at its own root, so on both of those hosts the
    theme's relative ``img/<hex>.jpg`` resolves here instead — without this route
    every photograph on every subdomain store would 404.

    `name` is matched against the digest shape rather than sanitized: the store is
    resolved from the Host header (fail-closed, custom domain first), and the file is
    then read only from THAT store's directory, so one store can never serve
    another's asset and no path parameter can escape the directory.
    """
    store = _store_for_host(request, session)
    if store is None:
        raise HTTPException(404, "not found")
    if not _STORE_IMAGE_NAME.fullmatch(name):
        raise HTTPException(404, "not found")
    asset = config.STORES_DIR / store.slug / "img" / name
    if not asset.is_file():
        raise HTTPException(404, "not found")
    # Content-addressed: the bytes for a given name can never change, so it is safe
    # to let browsers and CDNs keep it for a year.
    return FileResponse(
        asset,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ---------- ASP endpoint: create a store (x402-paid) ----------
# What a paid caller gets when they supply NO description at all. OKX's review
# automation pays the x402 challenge and then replays with an empty body — a 422
# there surfaces in the agent task flow as "endpoint requires parameters", which
# asks a human, and an unattended reviewer has none: the task times out and the
# listing is rejected for it (twice, 2026-07-27). A paid call with no parameters
# now delivers a sample store instead. Only ABSENT input is defaulted — input
# that is present but invalid (bad address, unknown theme, oversized text) still
# refuses before settle, because silently "fixing" an explicit value could route
# a merchant's sales to a mistyped wallet.
DEFAULT_STORE_DESCRIPTION = (
    "A small digital studio selling a downloadable weekly planner template for 9 USDT"
)


class DeliverableBody(BaseModel):
    """Real goods, attached while the store is created. `file` is absent by design:
    multipart cannot ride this JSON call, so files stay on the manage-key upload
    endpoint (which also owns versioning and replacement)."""

    kind: Literal["text", "license"]
    payload: str | None = Field(default=None, max_length=config.MAX_DELIVERY_LEN)
    max_activations: int | None = Field(default=None, ge=1, le=100000)

    @model_validator(mode="after")
    def _payload_required_for_text(self):
        if self.kind == "text" and not (self.payload or "").strip():
            raise ValueError("a text deliverable requires a non-empty payload")
        return self


class CreateStoreBody(BaseModel):
    # Optional since the empty-body fix: "" (or an absent body) means the caller
    # asked for a store without saying what it sells — they get the sample store.
    description: str = Field(default="", max_length=config.MAX_DESCRIPTION_LEN)
    receive_address: str | None = None
    # Optional theme choice; None lets the LLM pick. An unknown value is a 422.
    theme: str | None = None
    # What the buyer actually receives. engine.create_store has always accepted
    # this; it was simply unreachable from the API, which is why every store in
    # production fell back to placeholder text. Screened with the store's copy on
    # the same single Warden call.
    delivery: str | None = Field(default=None, max_length=config.MAX_DELIVERY_LEN)
    # Attaching one turns the store into a real fulfilment: a delivered order mints
    # an Entitlement and the buy response carries a licence key (or, once a file is
    # uploaded, a signed download URL) instead of a message.
    deliverable: DeliverableBody | None = None

    @field_validator("delivery")
    @classmethod
    def _validate_delivery(cls, v):
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("receive_address")
    @classmethod
    def _validate_address(cls, v):
        if v is None or v == "":
            return None
        if not _EVM_ADDRESS.fullmatch(v):
            raise ValueError(
                "receive_address must be a 0x-prefixed 20-byte EVM address"
            )
        if int(v[2:], 16) == 0:
            raise ValueError("receive_address must not be the zero address")
        return v

    @field_validator("theme")
    @classmethod
    def _validate_theme(cls, v):
        if v is None or v == "":
            return None
        if v not in providers.allowed_theme_names():
            raise ValueError("theme must be a built-in or an active installed theme")
        return v


def _run_create_store(
    description: str,
    receive_address: str | None,
    theme: str | None = None,
    delivery: str | None = None,
    deliverable: dict | None = None,
):
    try:
        return gen_store(
            description,
            receive_address,
            delivery=delivery,
            theme=theme,
            deliverable=deliverable,
        )
    except ScreeningBlocked as exc:
        logger.warning(
            "store creation blocked by screening: risk_level=%s",
            exc.verdict.get("risk_level"),
        )
        raise HTTPException(422, "content did not pass safety screening") from exc
    except GenerationUnavailable as exc:
        # An Anthropic outage (or malformed output) is a 503, not a 500. 503 >= 400,
        # so the x402 middleware skips settlement — a paid create-store during an
        # outage moves ZERO funds. No canned/cached fallback store, ever.
        logger.warning("store generation unavailable: %s", exc)
        raise HTTPException(
            503,
            "store generation temporarily unavailable — retry shortly",
            headers={"Retry-After": "60"},
        ) from exc


@app.post("/create-store")
@limiter.limit("6/minute")
def create_store_post(request: Request, body: CreateStoreBody | None = None):
    if not os.environ.get("TILLA_LLM_KEY"):
        raise HTTPException(503, "generation unavailable")
    # `None` covers a POST with no JSON body at all — the same caller-supplied-
    # nothing case as `{}`, so it earns the same sample store, not a 422.
    body = body or CreateStoreBody()
    description = body.description.strip()
    defaulted = not description
    if defaulted:
        description = DEFAULT_STORE_DESCRIPTION
    result = _run_create_store(
        description,
        body.receive_address,
        body.theme,
        delivery=body.delivery,
        deliverable=body.deliverable.model_dump() if body.deliverable else None,
    )
    if defaulted and isinstance(result, dict):
        # Additive: tell the (machine) caller what happened, so an agent that
        # meant to pass a description can see its input never arrived.
        result["note"] = (
            "no description provided; a sample store was generated — POST "
            '{"description": "what you sell"} to create a real one'
        )
    if isinstance(result, dict) and result.get("slug"):
        # The activation gap this closes: a store was created, a manage key handed
        # over, and nothing ever told the merchant that real goods can be attached.
        # Every production store fell back to placeholder text as a result.
        result["fulfilment"] = (
            {
                "configured": True,
                "kind": body.deliverable.kind if body.deliverable else "text",
                "note": "buyers receive this on delivery, not a placeholder message",
            }
            if body.deliverable
            else {
                "configured": False,
                "note": (
                    "no deliverable attached — buyers get the delivery message "
                    "only. Attach real goods (file, licence or text) with POST "
                    f"/api/stores/{result['slug']}/deliverable using the "
                    "manage_key as 'Authorization: Bearer <manage_key>'."
                ),
                # A merchant told us they never learned this call could carry the
                # goods itself; naming only the follow-up endpoint left them to
                # discover the field, which is how a store ships empty twice.
                "next_time": (
                    'pass it on the create call instead: {"description": "...", '
                    '"deliverable": {"kind": "license"}} — kind is "text" or '
                    '"license" here; files use the endpoint above.'
                ),
            }
        )
    return result


@app.get("/create-store")
@limiter.limit("6/minute")
def create_store_get(request: Request):
    # unpaid GET is intercepted by the x402 paywall (402 challenge — onchainos
    # x402-check probes GET). A PAID GET is refused 405 BEFORE settle: a >=400
    # response makes the payment middleware skip settlement, so zero funds move on
    # the GET method (a live reviewer paid 1 USDT for the old usage-JSON 200).
    return JSONResponse(
        {
            "error": "method not allowed; use POST to create a store",
            "how": (
                "POST {description?, receive_address?, theme?} (x402-paid) → "
                "returns a live store URL; an empty body creates a sample store"
            ),
        },
        status_code=405,
        headers={"Allow": "POST"},
    )


# ---------------- delivery evidence: what Tilla sold, portably provable ----------
class VerifyDeliveryBody(BaseModel):
    """One of the three handles a counterparty could plausibly be holding: the order
    id from a receipt, the settlement tx from the chain, or the EAS UID."""

    order_id: str | None = Field(default=None, max_length=32)
    tx_hash: str | None = Field(default=None, max_length=66)
    attestation_uid: str | None = Field(default=None, max_length=66)


class VerifyEvidenceBody(BaseModel):
    bundle: dict
    signature: str = Field(min_length=1, max_length=200)


@app.post("/verify-delivery")
@limiter.limit("60/minute")
def verify_delivery(
    request: Request,
    body: VerifyDeliveryBody | None = None,
    session: Session = Depends(get_session),
):
    """Signed, portable evidence for one delivered order (x402-paid).

    The chain already proves money moved; this proves WHAT moved against it — the
    delivered payload's content hash, whether the buyer accepted or disputed it, and
    any refund — none of which is derivable from the transfer alone. The on-chain
    anchors ride along so a reader can check the verifiable half without trusting us
    (see app/evidence.py for the trust model)."""
    body = body or VerifyDeliveryBody()
    bundle = evidence.build_bundle(
        session,
        order_id=body.order_id,
        tx_hash=body.tx_hash,
        attestation_uid=body.attestation_uid,
    )
    try:
        signature = evidence.sign_bundle(bundle)
    except evidence.EvidenceUnavailable:
        # Fail closed: an unsigned bundle would still LOOK authoritative.
        raise HTTPException(503, "evidence signing is not configured") from None
    return {"bundle": bundle, "signature": signature}


@app.post("/api/verify-evidence")
@limiter.limit("120/minute")
def verify_evidence(request: Request, body: VerifyEvidenceBody):
    """Re-check a bundle someone else is holding. FREE and unauthenticated on
    purpose: evidence nobody can afford to verify is not evidence, and this reveals
    nothing the bundle's holder does not already have."""
    return {"valid": evidence.verify_bundle(body.bundle, body.signature)}


# ---------- M10 marketplace services: upgrade a store / add a product ----------
class _SlugBody(BaseModel):
    slug: str = Field(min_length=1, max_length=config.SLUG_MAX_LEN)

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v):
        if not re.fullmatch(config.SLUG_PATTERN, v or ""):
            raise ValueError("slug must match the store slug pattern")
        return v


class UpgradeStoreBody(_SlugBody):
    # None -> keep the store's existing description as the regeneration prompt.
    description: str | None = Field(default=None, max_length=config.MAX_DESCRIPTION_LEN)
    theme: str | None = None

    @field_validator("description")
    @classmethod
    def _validate_description(cls, v):
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("theme")
    @classmethod
    def _validate_theme(cls, v):
        if v is None or v == "":
            return None
        if v not in providers.allowed_theme_names():
            raise ValueError("theme must be a built-in or an active installed theme")
        return v


class AddProductBody(_SlugBody):
    name: str = Field(min_length=1, max_length=120)
    price_usdt: float = Field(ge=0.01, le=10000)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


def _run_upgrade_store(
    request: Request,
    session: Session,
    slug: str,
    description: str | None,
    theme: str | None,
):
    if not os.environ.get("TILLA_LLM_KEY"):
        raise HTTPException(503, "generation unavailable")
    store = session.scalar(select(Store).where(Store.slug == slug))
    if store is None:
        raise HTTPException(404, "store not found")
    # Payment is monetization; the manage key is the SECURITY gate. A wrong/missing
    # key 401s BEFORE settle (>=400 -> the x402 middleware skips settlement), so a
    # paid call with a bad key moves zero funds and never touches the store.
    _require_store_key(request, store, session)
    if store.status != "live":
        raise HTTPException(409, "store is not live")
    try:
        return upgrade_store(session, store, description, theme)
    except ScreeningBlocked as exc:
        logger.warning(
            "store upgrade blocked by screening: risk_level=%s",
            exc.verdict.get("risk_level"),
        )
        # >=400 BEFORE settle: no funds move and the old live content keeps serving.
        raise HTTPException(422, "content did not pass safety screening") from exc
    except ScreeningUnavailable as exc:
        raise HTTPException(503, "content screening temporarily unavailable") from exc
    except GenerationUnavailable as exc:
        # Same seam as screening-unavailable: 503 (>=400) keeps the old live content
        # serving and moves zero funds during an Anthropic outage.
        logger.warning("store upgrade generation unavailable: %s", exc)
        raise HTTPException(
            503,
            "store generation temporarily unavailable — retry shortly",
            headers={"Retry-After": "60"},
        ) from exc


@app.post("/upgrade-store")
@limiter.limit("6/minute")
def upgrade_store_post(
    request: Request,
    body: UpgradeStoreBody,
    session: Session = Depends(get_session),
):
    return _run_upgrade_store(request, session, body.slug, body.description, body.theme)


@app.get("/upgrade-store")
@limiter.limit("6/minute")
def upgrade_store_get(request: Request):
    # unpaid GET → 402 challenge via the paywall; a PAID GET is refused 405 BEFORE
    # settle (>=400 skips settlement — no funds move on GET, same as agent_buy_get).
    return JSONResponse(
        {
            "error": "method not allowed; use POST to upgrade a store",
            "how": (
                "POST {slug, description?, theme?} + Authorization: Bearer "
                "<manage_key> (x402-paid, 0.03 USDT) → regenerates, re-screens, and "
                "re-renders a store you own"
            ),
        },
        status_code=405,
        headers={"Allow": "POST"},
    )


def _run_add_product(
    request: Request, session: Session, slug: str, name: str, price_usdt: float
):
    store = session.scalar(select(Store).where(Store.slug == slug))
    if store is None:
        raise HTTPException(404, "store not found")
    _require_store_key(request, store, session)
    if store.status != "live":
        raise HTTPException(409, "store is not live")
    # Screen the product name fail-closed. BLOCK -> 422, unavailable -> 503; in both
    # cases the response is >=400 BEFORE settle (no funds move) and no product row is
    # inserted.
    try:
        outcome = screen(name)
    except ScreeningBlocked as exc:
        raise HTTPException(422, "content did not pass safety screening") from exc
    if outcome.status != "allow":
        raise HTTPException(503, "content screening temporarily unavailable")
    # Additive by construction: a second active product. /s/{slug}/buy keeps selling
    # the primary product (lowest Product.id) at its unchanged price; feeds + MCP
    # enumerate all active products. Per-product deliverables are out of scope — all
    # products share the store's one active deliverable.
    price_micro = int(round(price_usdt * 1e6))
    product = Product(
        store_id=store.id, name=name, price_micro=price_micro, active=True
    )
    session.add(product)
    session.flush()
    # Re-render the storefront, exactly as the dashboard's add-product does. The page
    # is a static file: without this the paid product existed in the DB and was
    # buyable by id, but never appeared on the store an agent or human actually
    # looks at — a purchase that visibly changed nothing.
    resync_catalog(session, store)
    r = outcome.receipt
    if r is not None:
        session.add(
            ScreeningReceipt(
                store_id=store.id,
                mode=r.mode,
                verdict=r.verdict,
                risk_level=r.risk_level,
                endpoint=r.endpoint,
                amount_micro=r.amount_micro,
                tx_hash=r.tx_hash,
            )
        )
    log_event(
        session,
        "api",
        "product.added",
        store_id=store.id,
        data={"slug": slug, "product_id": product.id},
    )
    session.commit()
    return {
        "slug": slug,
        "product_id": product.id,
        "name": name,
        "price_usdt": price_usdt,
    }


@app.post("/add-product")
@limiter.limit("6/minute")
def add_product_post(
    request: Request,
    body: AddProductBody,
    session: Session = Depends(get_session),
):
    return _run_add_product(request, session, body.slug, body.name, body.price_usdt)


@app.get("/add-product")
@limiter.limit("6/minute")
def add_product_get(request: Request):
    # unpaid GET → 402 challenge via the paywall; a PAID GET is refused 405 BEFORE
    # settle (>=400 skips settlement — no funds move on GET, same as agent_buy_get).
    return JSONResponse(
        {
            "error": "method not allowed; use POST to add a product",
            "how": (
                "POST {slug, name, price_usdt} + Authorization: Bearer "
                "<manage_key> (x402-paid, 0.01 USDT) → adds a second product to a "
                "store you own"
            ),
        },
        status_code=405,
        headers={"Allow": "POST"},
    )


class TxBody(BaseModel):
    tx_hash: str

    @field_validator("tx_hash")
    @classmethod
    def _validate_tx_hash(cls, v):
        if not _TX_HASH.fullmatch(v or ""):
            raise ValueError("tx_hash must be a 0x-prefixed 32-byte hash")
        return v.lower()


# The neutral message the UNAUTH poll returns in place of an entitlement-backed
# order's real delivery payload (which IS the license key / text secret): the
# buyer signs with their purchase wallet to claim the goods via /api/library.
CLAIM_DELIVERY_MESSAGE = "Paid — sign with your purchase wallet to claim your delivery."


def _order_response(
    session: Session, order: Order, include_gated: bool = False
) -> dict:
    # Surface terminal (delivered / legacy paid) as "paid" so every already-
    # rendered store's poll (`d.status === 'paid'`) keeps working unchanged.
    terminal = order.status in checkout.TERMINAL_DELIVERED
    out = {
        "id": order.id,
        "status": "paid" if terminal else order.status,
        "amount": order.expected_micro / 1e6,
        "amount_micro": order.expected_micro,
        "pay_to": order.pay_to,
    }
    # Storefront depth: the membership tier the paid amount maps to (if any).
    tier_name = _membership_tier_name(session, order)
    if tier_name:
        out["tier"] = tier_name
    if terminal:
        delivery_row = session.scalar(
            select(Delivery).where(Delivery.order_id == order.id)
        )
        out["tx_hash"] = order.tx_hash
        # M11 on-chain receipt (additive, NULL-safe): surfaced only once an
        # attestation exists. Deployed static pages ignore unknown keys, and the
        # link is the verified OKLink attest-tx URL (no unverified EAS-scan link).
        if order.attestation_uid:
            out["attestation_uid"] = order.attestation_uid
        if order.attest_tx:
            out["attestation_tx_url"] = config.OKLINK_TX_BASE + order.attest_tx
        if include_gated:
            # Capability surface (wallet session / magic link): the authorized
            # owner may see the full payload and freshly minted gated keys.
            out["delivery"] = delivery_row.payload if delivery_row else DEFAULT_DELIVERY
            _augment_gated(session, order, out)
        else:
            # Unauth surface (the 64-bit cid is a bearer key that leaks via
            # history/logs/Referer): never surface download_url/license_key, and
            # gate the `delivery` field itself, because Delivery.payload IS the
            # license key for a license order and IS the text secret for an
            # entitlement-backed text order. Rule: if an Entitlement exists, the
            # payload is replaced by the neutral claim message + kind/claim so the
            # page renders the sign-to-claim UI; a pure legacy store.delivery text
            # order (no entitlement) passes through unchanged so every already-
            # deployed static page keeps its delivered state.
            ent = session.scalar(
                select(Entitlement).where(Entitlement.order_id == order.id)
            )
            if ent is not None:
                out["delivery"] = CLAIM_DELIVERY_MESSAGE
                out["kind"] = delivery_row.kind if delivery_row else "text"
                out["claim"] = True
            else:
                out["delivery"] = (
                    delivery_row.payload if delivery_row else DEFAULT_DELIVERY
                )
    return out


def _current_version(session: Session, deliverable: Deliverable) -> Deliverable:
    """Roll an entitlement's deliverable FORWARD to the store's current active version in
    the same (store, product, kind) lineage — the versioned-releases mechanism: a past
    buyer re-downloads the newest release. Returns the highest active deliverable of the
    same kind whose ``version`` is strictly greater, else the deliverable unchanged. A
    plain replace (which keeps version 1) never satisfies ``version >`` so it never rolls
    a past buyer forward; only an explicit new version (version = prev + 1) does."""
    q = select(Deliverable).where(
        Deliverable.store_id == deliverable.store_id,
        Deliverable.kind == deliverable.kind,
        Deliverable.active.is_(True),
        Deliverable.version > deliverable.version,
    )
    if deliverable.product_id is None:
        q = q.where(Deliverable.product_id.is_(None))
    else:
        q = q.where(Deliverable.product_id == deliverable.product_id)
    newer = session.scalar(q.order_by(Deliverable.version.desc()).limit(1))
    return newer or deliverable


def _augment_gated(session: Session, order: Order, out: dict) -> None:
    """Add the additive M4 keys — ``download_url`` (file) / ``license_key``
    (license) — minted fresh at read time. Deployed store pages ignore unknown
    keys, so this never breaks an existing page. Silent when the signing key is
    unset (legacy text delivery is unaffected) or the entitlement is revoked /
    over its download budget."""
    ent = session.scalar(select(Entitlement).where(Entitlement.order_id == order.id))
    if ent is None or ent.revoked_at is not None:
        return
    deliverable = session.get(Deliverable, ent.deliverable_id)
    if deliverable is None:
        return
    # Versioned releases: a file re-download follows the lineage forward to the current
    # active version, so a past buyer always gets the newest file. License keys live on
    # the entitlement (version-independent), so a license is left unchanged.
    if deliverable.kind == "file":
        deliverable = _current_version(session, deliverable)
    if deliverable.kind == "license" and ent.license_key:
        out["license_key"] = ent.license_key
    elif deliverable.kind == "file" and ent.download_count < deliverable.max_downloads:
        with contextlib.suppress(delivery.SigningUnavailable):
            token = delivery.mint_download_token(ent.id, deliverable.id)
            out["download_url"] = delivery.download_url(token)


class CheckoutCreateBody(BaseModel):
    # M13 affiliate attribution (additive, optional). The referring agent's payout
    # wallet; themes forward `?ref=` here. Validated + lowercased, zero-address
    # rejected. Absent/empty -> no attribution, byte-identical to the pre-M13 flow.
    ref: str | None = None
    # Multi-product: the buyer's chosen product. product_id is the stable DB id
    # (preferred — a deactivate/reorder can't shift it); product_index is the
    # legacy render-order fallback still sent by pages cached before the id switch.
    # Both absent -> the primary product, byte-identical to the single-product flow.
    product_id: int | None = None
    product_index: int | None = None
    # Storefront depth (additive, optional). ``tier`` is the buyer-chosen membership tier
    # NAME (its configured price becomes the order amount); ``amount_micro`` is the
    # buyer-chosen pay-what-you-want amount (must be >= the configured floor). Both absent
    # -> the product list price, byte-identical to the pre-storefront-depth flow.
    tier: str | None = Field(default=None, max_length=60)
    amount_micro: StrictInt | None = None

    @field_validator("ref")
    @classmethod
    def _v_ref(cls, v):
        try:
            return affiliates.normalize_ref(v)
        except affiliates.RefRejected as exc:
            raise ValueError(str(exc)) from exc


def _pricing_params(product: Product) -> dict:
    return product.pricing_params if isinstance(product.pricing_params, dict) else {}


def _resolve_checkout_price(
    product: Product, tier: str | None, amount_micro: int | None
) -> int | None:
    """The buyer-chosen order price (micro-USDT) for a membership / pay-what-you-want
    product, or None for a plain list-price product. Fail-closed:

    - Membership: the buyer MUST name a configured tier (400 if none picked, 404 if the
      name is unknown); the tier's price is returned.
    - Pay-what-you-want: the buyer MUST name an amount (400 if absent) that is at or above
      the configured floor (400 below the floor).

    A product without membership/pwyw config ignores tier/amount and returns None so the
    caller uses the product list price."""
    params = _pricing_params(product)
    membership = params.get("membership_tiers")
    pwyw_min = params.get("pwyw_min_micro")
    if isinstance(membership, list) and membership:
        if not tier:
            raise HTTPException(400, "select a membership tier")
        match = next(
            (t for t in membership if isinstance(t, dict) and t.get("name") == tier),
            None,
        )
        if match is None:
            raise HTTPException(404, "unknown membership tier")
        return int(match["price_micro"])
    if isinstance(pwyw_min, int):
        if amount_micro is None:
            raise HTTPException(400, "choose an amount to pay")
        if amount_micro < pwyw_min:
            raise HTTPException(400, "amount is below the minimum")
        return int(amount_micro)
    return None


def _membership_tier_name(session: Session, order: Order) -> str | None:
    """The membership tier NAME an order's paid amount maps to, or None. Derived by
    matching ``order.amount_micro`` (the chosen tier price recorded at checkout) against
    the product's configured membership tiers — so the tier is recorded by the amount,
    with no extra column."""
    if order.product_id is None:
        return None
    product = session.get(Product, order.product_id)
    if product is None:
        return None
    membership = _pricing_params(product).get("membership_tiers")
    if not isinstance(membership, list):
        return None
    for t in membership:
        if isinstance(t, dict) and int(t.get("price_micro", -1)) == order.amount_micro:
            return t.get("name")
    return None


# ---------- store checkout: buyer pays the merchant ----------
@app.post("/api/checkout/{slug}")
@limiter.limit("20/minute")
def create_checkout(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    body: CheckoutCreateBody | None = None,
    session: Session = Depends(get_session),
):
    store = session.scalar(select(Store).where(Store.slug == slug))
    if store is None:
        raise HTTPException(404, "store not found")
    if store.status == "pending_screening":
        raise HTTPException(409, "store is not yet live (pending content screening)")
    if store.status != "live":
        raise HTTPException(404, "store not found")
    products = session.scalars(
        select(Product)
        .where(Product.store_id == store.id, Product.active.is_(True))
        .order_by(Product.id)
    ).all()
    if not products:
        raise HTTPException(409, "store has no active product")
    # Select the buyer's chosen product: prefer the stable product_id (validated to
    # be an ACTIVE product of THIS store — a foreign or inactive id fails closed
    # 404, never charges the wrong product / no IDOR), else the legacy render-order
    # index, else the primary product.
    pid = body.product_id if body is not None else None
    idx = body.product_index if body is not None else None
    if pid is not None:
        product = next((p for p in products if p.id == pid), None)
        if product is None:
            raise HTTPException(404, "product not found")
    elif idx is not None:
        if 0 <= idx < len(products):
            product = products[idx]
        else:
            raise HTTPException(400, "product_index out of range")
    else:
        product = products[0]
    referrer_addr = body.ref if body is not None else None
    tier = body.tier if body is not None else None
    chosen_amount = body.amount_micro if body is not None else None
    price_override = _resolve_checkout_price(product, tier, chosen_amount)
    try:
        order = checkout.create_order(
            session, store, product, referrer_addr, price_micro_override=price_override
        )
    except checkout.AmountUnavailable as exc:
        raise HTTPException(503, "checkout busy, retry") from exc
    log_event(
        session,
        "api",
        "order.created",
        store_id=store.id,
        order_id=order.id,
        data={"tier": _membership_tier_name(session, order)}
        if price_override is not None
        else None,
    )
    session.commit()
    tier_name = _membership_tier_name(session, order)
    return {
        "id": order.id,
        "pay_to": store.pay_to,
        "product_name": product.name,
        **({"tier": tier_name} if tier_name else {}),
        # Phase 1.3 SLA: the delivery-time promise (minutes) shown to the buyer as
        # an ETA, and consumed by embed/custom checkout frontends.
        "sla_minutes": agentic._effective_sla(product),
        "amount": order.expected_micro / 1e6,
        # Exact base-unit amount so the browser builds ERC-20 calldata with BigInt
        # only (amount*1e6 in JS can be off by one micro, which the exact-match
        # sweeper would treat as an unmatched transfer).
        "amount_micro": order.expected_micro,
        # A naive-UTC isoformat with no 'Z' parses as LOCAL time in JS Date; append
        # 'Z' so the buyer's countdown ticks against real UTC expiry.
        "expires_at": order.expires_at.isoformat() + "Z",
        "network": "X Layer (chainId 196)",
        "token": "USDT",
    }


@app.get("/api/checkout/{cid}")
@limiter.limit("40/minute")
def checkout_status(
    request: Request, cid: str, session: Session = Depends(get_session)
):
    order = session.get(Order, cid)
    if order is None:
        raise HTTPException(404, "checkout not found")
    if order.status not in checkout.TERMINAL_DELIVERED and order.status != "refunded":
        checkout.refresh_order(session, order)
        session.expire(order)  # pick up any conditional-UPDATE transition
    return _order_response(session, order)


@app.post("/api/checkout/{cid}/tx")
@limiter.limit("10/minute")
def submit_txhash(
    request: Request,
    body: TxBody,
    cid: str,
    session: Session = Depends(get_session),
):
    order = session.get(Order, cid)
    if order is None:
        raise HTTPException(404, "checkout not found")
    try:
        checkout.verify_txhash(session, order, body.tx_hash)
    except checkout.TxAlreadyUsed as exc:
        raise HTTPException(409, "tx already used") from exc
    except checkout.TxVerificationError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (httpx.HTTPError, chain.ChainError) as exc:
        raise HTTPException(502, "chain verification unavailable") from exc
    try:
        session.commit()
    except IntegrityError:
        # Two concurrent identical /tx submissions both passed the in-session
        # ProcessedTransfer pre-check; the loser trips uq_processed_tx_log at
        # commit. No double-delivery (the unique constraint + idempotent deliver
        # hold) — reconcile to the committed winner state instead of 500ing.
        session.rollback()
        session.expire(order)
        return _order_response(session, order)
    session.expire(order)
    return _order_response(session, order)


# ================= M4 gated delivery =================
def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


def _require_store_key(request: Request, store: Store, session: Session) -> None:
    """Per-store capability auth. Accepts EITHER the per-store manage key
    (Authorization: Bearer <manage_key>, sha256 + constant-time compare against
    stores.manage_key_hash — byte-identical to M4 for headless/agent callers) OR a
    merchant session token / API key whose merchant owns this store (M9 additive).
    Manage keys keep working unchanged."""
    bearer = _bearer(request)
    if delivery.verify_manage_key(bearer, store.manage_key_hash):
        return
    merchant = dashboard.resolve_merchant(session, bearer)
    if merchant is not None and merchant.id == store.merchant_id:
        return
    raise HTTPException(401, "invalid or missing manage key")


def _authorize_order_email(request: Request, session: Session, order: Order) -> bool:
    """Proof required to attach a buyer email / trigger a redelivery on `order`:
    a wallet session token for the order's on-chain payer, or the store's manage
    key. Without it, anyone who learns a cid could overwrite buyer_email or have a
    working redelivery link mailed to an attacker-chosen address."""
    bearer = _bearer(request)
    if not bearer:
        return False
    if config.SIGNING_KEY and order.from_addr:
        with contextlib.suppress(BadSignature, SignatureExpired):
            if delivery.load_session_token(bearer) == order.from_addr.lower():
                return True
    store = session.get(Store, order.store_id)
    if store is not None and delivery.verify_manage_key(bearer, store.manage_key_hash):
        return True
    return False


def _deactivate_deliverables(
    session: Session, store_id: int, product_id: int | None = None
) -> None:
    """Flip active deliverables inactive within one scope: the store-level default
    (product_id NULL) when product_id is None, else just that product's — so
    setting a per-product deliverable never disturbs the store default or other
    products' deliverables (and vice versa)."""
    stmt = update(Deliverable).where(
        Deliverable.store_id == store_id, Deliverable.active.is_(True)
    )
    if product_id is None:
        stmt = stmt.where(Deliverable.product_id.is_(None))
    else:
        stmt = stmt.where(Deliverable.product_id == product_id)
    session.execute(stmt.values(active=False))


def _deliverable_product_id(session: Session, store: Store, raw: object) -> int | None:
    """None (store-level default) when absent/empty; otherwise the int, validated
    to be an ACTIVE product of THIS store (422 otherwise, so a merchant can't bind
    a deliverable to a foreign or non-existent product)."""
    if raw in (None, ""):
        return None
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, "product_id must be an integer") from None
    product = session.scalar(
        select(Product).where(
            Product.id == pid,
            Product.store_id == store.id,
            Product.active.is_(True),
        )
    )
    if product is None:
        raise HTTPException(422, "product_id is not an active product of this store")
    return pid


def _truthy(value: object) -> bool:
    """Coerce a form/JSON flag to bool: JSON true, or the strings '1'/'true'/'yes'/'on'
    (a multipart field is always a string)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _resolve_deliverable_version(
    session: Session,
    store_id: int,
    product_id: int | None,
    kind: str,
    versioned: bool,
) -> int:
    """The ``version`` for a new deliverable. A plain create/replace is a fresh lineage
    (version 1). A versioned release (versioned=True) is ``max(existing version) + 1`` over
    every deliverable of the SAME (store, product, kind) — active or not, so the number is
    strictly monotonic and always greater than any past buyer's entitlement version. It
    409s when no deliverable of that kind exists yet (nothing to version)."""
    if not versioned:
        return 1
    q = select(func.max(Deliverable.version)).where(
        Deliverable.store_id == store_id, Deliverable.kind == kind
    )
    if product_id is None:
        q = q.where(Deliverable.product_id.is_(None))
    else:
        q = q.where(Deliverable.product_id == product_id)
    prev_max = session.scalar(q)
    if prev_max is None:
        raise HTTPException(
            409, "no existing deliverable of this kind to publish a new version of"
        )
    return int(prev_max) + 1


def _positive_int(value, default: int) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


class _AddressBody(BaseModel):
    address: str

    @field_validator("address")
    @classmethod
    def _v_addr(cls, v):
        if not _EVM_ADDRESS.fullmatch(v or ""):
            raise ValueError("address must be a 0x-prefixed 20-byte EVM address")
        return v.lower()


class NonceBody(_AddressBody):
    pass


class VerifyBody(_AddressBody):
    signature: str = Field(min_length=1, max_length=300)


class EmailBody(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class LicenseActionBody(BaseModel):
    license_key: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=128)


class LicenseValidateBody(BaseModel):
    license_key: str = Field(min_length=1, max_length=64)
    device_id: str = Field(default="", max_length=128)


# ================= M8 payment-method declaration =================
class _MeteredParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit: str = Field(min_length=1, max_length=20)
    price_per_unit_micro: int = Field(gt=0)
    min_deposit_micro: int = Field(gt=0)


class _SubscriptionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_per_period_micro: int = Field(gt=0)
    period_sec: int = Field(ge=3600)
    max_periods: int = Field(ge=0)  # 0 = open-ended
    plan_id: str = Field(min_length=1, max_length=64)
    plan_tier: int = Field(ge=1, le=255)
    plan_name: str = Field(min_length=1, max_length=120)


class _WholesaleTier(BaseModel):
    # M16 B2B: one buyer-identity-keyed wholesale price. price_micro is a StrictInt
    # (no float amounts) bounded to the M1 product price range. buyer is either the
    # sentinel 'any_agent' or 'erc8004:<id>' — validated below.
    model_config = ConfigDict(extra="forbid")
    buyer: str
    price_micro: StrictInt = Field(
        ge=config.TIER_PRICE_MICRO_MIN, le=config.TIER_PRICE_MICRO_MAX
    )

    @field_validator("buyer")
    @classmethod
    def _v_buyer(cls, v: str) -> str:
        if v == "any_agent":
            return v
        m = re.fullmatch(r"erc8004:(\d{1,20})", v)
        if m:
            # Canonicalize the id (strip leading zeros) so the stored buyer always
            # matches match_tier's f"erc8004:{int}" form — 'erc8004:07' would
            # otherwise be a dead tier that silently never applies.
            return f"erc8004:{int(m.group(1))}"
        raise ValueError("buyer must be 'any_agent' or 'erc8004:<id>'")


class _MembershipTier(BaseModel):
    # Storefront depth: one named access tier a buyer may pick at checkout. name is
    # merchant copy (rendered client-side via textContent — never innerHTML); price_micro
    # is a StrictInt (no float amounts) in the same micro-USDT range as a wholesale tier.
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    price_micro: StrictInt = Field(
        ge=config.TIER_PRICE_MICRO_MIN, le=config.TIER_PRICE_MICRO_MAX
    )


class PricingBody(BaseModel):
    pricing_model: Literal["one_time", "batch", "metered", "subscription"]
    params: dict | None = None
    # M16 B2B wholesale tiers — additive on the SAME pricing_params JSON column, no
    # migration. A pricing call rewrites the product's wholesale terms wholesale:
    # omitted/None clears any prior tiers (the same replace semantics as params).
    tiers: list[_WholesaleTier] | None = None
    # Storefront depth — membership tiers + pay-what-you-want, both additive on the SAME
    # pricing_params JSON column (no migration). A buyer picks a membership tier by name
    # (its price becomes the order amount) OR names an amount >= pwyw_min_micro; the two
    # are mutually exclusive (rejected together). Omitted/None clears any prior value, the
    # same replace semantics as tiers/params.
    membership_tiers: list[_MembershipTier] | None = None
    pwyw_min_micro: StrictInt | None = Field(
        default=None, ge=config.TIER_PRICE_MICRO_MIN, le=config.TIER_PRICE_MICRO_MAX
    )


_PRICING_PARAM_MODELS = {
    "metered": _MeteredParams,
    "subscription": _SubscriptionParams,
}


def _validate_tiers(
    tiers: list[_WholesaleTier] | None, base_price_micro: int
) -> list[dict] | None:
    """Normalize wholesale tiers or raise 422: at most TIERS_MAX, no duplicate
    buyers, and every tier at or below the base price (a wholesale tier is a
    discount — a tier above base would grief verified buyers once 16.2 wires
    tiers into the 402). Per-tier shape/bounds are enforced by the model."""
    if not tiers:
        return None
    if len(tiers) > config.TIERS_MAX:
        raise HTTPException(422, f"at most {config.TIERS_MAX} wholesale tiers")
    buyers = [t.buyer for t in tiers]
    if len(set(buyers)) != len(buyers):
        raise HTTPException(422, "duplicate tier buyer")
    for t in tiers:
        if t.price_micro > base_price_micro:
            raise HTTPException(
                422, "tier price_micro must not exceed the base product price"
            )
    return [t.model_dump() for t in tiers]


def _validate_membership_tiers(
    tiers: list[_MembershipTier] | None,
) -> list[dict] | None:
    """Normalize membership tiers or raise 422: at most TIERS_MAX and no duplicate
    names (case-insensitive — 'Gold' and 'gold' would be an ambiguous pick). Per-tier
    shape/bounds are enforced by the model."""
    if not tiers:
        return None
    if len(tiers) > config.TIERS_MAX:
        raise HTTPException(422, f"at most {config.TIERS_MAX} membership tiers")
    names = [t.name.strip().lower() for t in tiers]
    if len(set(names)) != len(names):
        raise HTTPException(422, "duplicate membership tier name")
    return [t.model_dump() for t in tiers]


def _validate_pricing_params(model: str, params: dict | None) -> dict | None:
    """Normalize the per-model params or raise 422. one_time/batch take NO params
    (price_micro is the price / per-unit price); metered + subscription validate
    against their pydantic shape (unknown keys forbidden)."""
    if model in ("one_time", "batch"):
        if params:
            raise HTTPException(422, f"{model} pricing takes no params")
        return None
    schema = _PRICING_PARAM_MODELS[model]
    try:
        return schema.model_validate(params or {}).model_dump()
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise HTTPException(
            422, f"invalid {model} params ({loc}): {first.get('msg')}"
        ) from None


@app.post("/api/stores/{slug}/pricing")
@limiter.limit("30/hour")
async def set_pricing(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Declare the active product's payment method (manage-key gated, same seam as
    /deliverable). Pure metadata: it never moves funds and — with every rail flag
    off — never changes the served 402. Default 'one_time' is unchanged exact
    checkout; 'batch' only unlocks the aggr_deferred accepts-entry when its flag is
    on; 'metered'/'subscription' only unlock their (dormant) endpoints."""
    store = session.scalar(select(Store).where(Store.slug == slug))
    if store is None:
        raise HTTPException(404, "store not found")
    _require_store_key(request, store, session)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "invalid JSON body") from None
    try:
        parsed = PricingBody.model_validate(body)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise HTTPException(
            422,
            f"invalid pricing body ({loc or 'pricing_model'}): {first.get('msg')}",
        ) from None
    params = _validate_pricing_params(parsed.pricing_model, parsed.params)
    product = session.scalar(
        select(Product)
        .where(Product.store_id == store.id, Product.active.is_(True))
        .order_by(Product.id)
    )
    if product is None:
        raise HTTPException(409, "store has no active product")
    tiers = _validate_tiers(parsed.tiers, product.price_micro)
    membership_tiers = _validate_membership_tiers(parsed.membership_tiers)
    # Membership tiers and pay-what-you-want are two ways to set the checkout price, so a
    # single product can carry only one (both together is an ambiguous checkout).
    if membership_tiers and parsed.pwyw_min_micro is not None:
        raise HTTPException(
            422, "a product cannot set both membership tiers and pay-what-you-want"
        )
    # The aggr_deferred rail delivers on a facilitator-VERIFIED authorization and
    # settles on-chain later — that is its documented contract. Every claimable good
    # (file download, license activation) is gated on TERMINAL_DELIVERED, so a
    # deferred settlement holds them locked and the void path revokes them. A TEXT
    # deliverable is the one shape that defeats the gate: its whole value rides in
    # the immediate response body, and knowledge cannot be revoked. So the deferred
    # rail is only enableable on a product whose deliverable is a server-gated file
    # — a store with no deliverable row falls back to store.delivery text and is
    # refused for the same reason. Production taught this the hard way: OKX's
    # listing validators have twice driven served-200s whose settles never landed
    # on-chain (three orders on 2026-07-23, six on 2026-07-26); files stayed locked
    # both times, text went out with the response.
    if parsed.pricing_model == "batch":
        active_deliverable = session.scalar(
            select(Deliverable)
            .where(Deliverable.store_id == store.id, Deliverable.active.is_(True))
            .order_by(Deliverable.id.desc())
        )
        if active_deliverable is None or active_deliverable.kind != "file":
            raise HTTPException(
                422,
                "deferred settlement (batch) requires a file deliverable: text and "
                "license deliverables are released in the response body, before a "
                "deferred settle confirms on-chain — upload a file deliverable "
                "first, or keep this product on one_time (settle-first) pricing",
            )
    # Tiers/membership/pwyw all ride the SAME JSON column under their own keys alongside
    # any model params. Absent keys clear prior values (whole-rewrite semantics).
    merged = dict(params) if params else {}
    if tiers:
        merged["tiers"] = tiers
    if membership_tiers:
        merged["membership_tiers"] = membership_tiers
    if parsed.pwyw_min_micro is not None:
        merged["pwyw_min_micro"] = parsed.pwyw_min_micro
    product.pricing_model = parsed.pricing_model
    product.pricing_params = merged or None
    log_event(
        session,
        "api",
        "pricing.updated",
        store_id=store.id,
        data={"product_id": product.id, "pricing_model": parsed.pricing_model},
    )
    session.commit()
    return {
        "product_id": product.id,
        "pricing_model": parsed.pricing_model,
        "params": params,
        "tiers": tiers,
        "membership_tiers": membership_tiers,
        "pwyw_min_micro": parsed.pwyw_min_micro,
    }


@app.post("/api/stores/{slug}/deliverable")
@limiter.limit("10/hour")
async def create_deliverable(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Set (or replace) a store's one active deliverable. multipart `file` field
    for kind=file; JSON {kind, payload?, max_activations?, ...} for text/license.
    Replacing inserts a new active row and flips the old one inactive, so orders
    already sold keep the version they bought."""
    if not config.SIGNING_KEY:
        raise HTTPException(503, "gated delivery not configured")
    store = session.scalar(select(Store).where(Store.slug == slug))
    if store is None:
        raise HTTPException(404, "store not found")
    _require_store_key(request, store, session)

    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            raise HTTPException(422, "multipart field 'file' is required")
        ext = delivery.file_extension(upload.filename)
        if ext not in config.UPLOAD_ALLOWED_EXTS:
            raise HTTPException(422, f"file type '.{ext}' is not allowed")
        try:
            stored = await delivery.store_upload(upload)
        except delivery.UploadTooLarge:
            raise HTTPException(413, "file exceeds the size cap") from None
        product_id = _deliverable_product_id(session, store, form.get("product_id"))
        version = _resolve_deliverable_version(
            session, store.id, product_id, "file", _truthy(form.get("version"))
        )
        _deactivate_deliverables(session, store.id, product_id)
        deliverable = Deliverable(
            store_id=store.id,
            product_id=product_id,
            kind="file",
            file_sha256=stored["sha256"],
            file_name=delivery.sanitize_filename(upload.filename),
            file_size=stored["size"],
            mime=(upload.content_type or "")[:100] or None,
            max_downloads=_positive_int(
                form.get("max_downloads"), config.DOWNLOAD_LIMIT_DEFAULT
            ),
            link_ttl_seconds=_positive_int(
                form.get("link_ttl_seconds"), config.LINK_TTL_DEFAULT
            ),
            version=version,
            active=True,
        )
    else:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(422, "invalid JSON body") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "JSON body must be an object")
        product_id = _deliverable_product_id(session, store, body.get("product_id"))
        kind = body.get("kind")
        if kind == "text":
            payload = body.get("payload")
            if not isinstance(payload, str) or not payload.strip():
                raise HTTPException(
                    422, "text deliverable requires a non-empty payload"
                )
            version = _resolve_deliverable_version(
                session, store.id, product_id, "text", _truthy(body.get("version"))
            )
            _deactivate_deliverables(session, store.id, product_id)
            deliverable = Deliverable(
                store_id=store.id,
                product_id=product_id,
                kind="text",
                payload=payload,
                version=version,
                active=True,
            )
        elif kind == "license":
            version = _resolve_deliverable_version(
                session, store.id, product_id, "license", _truthy(body.get("version"))
            )
            _deactivate_deliverables(session, store.id, product_id)
            deliverable = Deliverable(
                store_id=store.id,
                product_id=product_id,
                kind="license",
                max_activations=_positive_int(
                    body.get("max_activations"), config.LICENSE_ACTIVATIONS_DEFAULT
                ),
                version=version,
                active=True,
            )
        else:
            raise HTTPException(422, "kind must be one of: file, text, license")

    session.add(deliverable)
    session.flush()
    log_event(
        session,
        "api",
        "deliverable.created",
        store_id=store.id,
        data={"kind": deliverable.kind, "deliverable_id": deliverable.id},
    )
    session.commit()
    resp = {
        "id": deliverable.id,
        "product_id": deliverable.product_id,
        "kind": deliverable.kind,
        "version": deliverable.version,
        "active": True,
    }
    if deliverable.kind == "file":
        resp.update(
            file_name=deliverable.file_name,
            file_size=deliverable.file_size,
            max_downloads=deliverable.max_downloads,
            link_ttl_seconds=deliverable.link_ttl_seconds,
        )
    elif deliverable.kind == "license":
        resp["max_activations"] = deliverable.max_activations
    return resp


@app.get("/api/download/{token}")
@limiter.limit("30/minute")
def download(request: Request, token: str, session: Session = Depends(get_session)):
    """Stream a gated file. The signed token is the only path to the bytes: it must
    be authentic, unexpired (per-deliverable TTL), tied to an unrevoked entitlement
    on a terminal (non-refunded) order, and under the download cap — which is
    consumed by a race-proof conditional UPDATE."""
    if not config.SIGNING_KEY:
        raise HTTPException(503, "gated delivery not configured")
    try:
        payload = delivery.peek_download_token(token)
    except BadSignature:
        raise HTTPException(403, "invalid download link") from None
    deliverable = session.get(Deliverable, payload.get("d"))
    ent = session.get(Entitlement, payload.get("e"))
    if deliverable is None or ent is None or deliverable.kind != "file":
        raise HTTPException(404, "download not found")
    try:
        delivery.load_download_token(token, max_age=deliverable.link_ttl_seconds)
    except SignatureExpired:
        raise HTTPException(410, "download link expired") from None
    except BadSignature:
        raise HTTPException(403, "invalid download link") from None
    if ent.revoked_at is not None:
        raise HTTPException(403, "entitlement revoked")
    order = session.get(Order, ent.order_id)
    if order is None or order.status not in checkout.TERMINAL_DELIVERED:
        raise HTTPException(403, "order is not deliverable")
    path = delivery.file_path(deliverable.file_sha256)
    if not path.exists():
        raise HTTPException(404, "file is missing")
    if not delivery.claim_download(session, ent.id, deliverable.max_downloads):
        raise HTTPException(410, "download limit reached")
    session.commit()
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=delivery.sanitize_filename(deliverable.file_name),
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


@app.post("/api/auth/nonce")
@limiter.limit("30/minute")
def auth_nonce(
    request: Request, body: NonceBody, session: Session = Depends(get_session)
):
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    row = delivery.issue_nonce(session, body.address)
    message = delivery.build_signin_message(
        row.address, row.nonce, row.issued_at.isoformat(), row.expires_at.isoformat()
    )
    session.commit()
    return {
        "nonce": row.nonce,
        "message": message,
        "expires_at": row.expires_at.isoformat(),
    }


@app.post("/api/auth/verify")
@limiter.limit("30/minute")
def auth_verify(
    request: Request, body: VerifyBody, session: Session = Depends(get_session)
):
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    token = delivery.verify_signin(session, body.address, body.signature)
    session.commit()
    if token is None:
        raise HTTPException(401, "signature verification failed")
    return {"session_token": token, "expires_in": config.SESSION_TTL}


@app.get("/api/library")
@limiter.limit("30/minute")
def library(request: Request, session: Session = Depends(get_session)):
    """A buyer wallet's purchases (terminal orders paid from that address), each
    with a freshly minted download_url (file, under budget, unrevoked) or
    license_key. Re-issuing links here draws on the same download budget."""
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    try:
        wallet = delivery.load_session_token(_bearer(request))
    except SignatureExpired:
        raise HTTPException(401, "session expired") from None
    except BadSignature:
        raise HTTPException(401, "invalid session") from None
    orders = session.scalars(
        select(Order)
        .where(
            func.lower(Order.from_addr) == wallet,
            Order.status.in_(checkout.TERMINAL_DELIVERED),
        )
        .order_by(Order.paid_at.desc())
    ).all()
    purchases = []
    for order in orders:
        store = session.get(Store, order.store_id)
        product = session.get(Product, order.product_id) if order.product_id else None
        delivery_row = session.scalar(
            select(Delivery).where(Delivery.order_id == order.id)
        )
        item = {
            "order_id": order.id,
            "store_slug": store.slug if store else None,
            "product": product.name if product else None,
            "amount": order.expected_micro / 1e6,
            "paid_at": order.paid_at.isoformat() if order.paid_at else None,
            # Phase 1.6: lets the buyer UI offer confirm/dispute while still 'pending'.
            "eval_status": order.eval_status,
            "kind": delivery_row.kind if delivery_row else "text",
            # The authenticated wallet owner may see the full Delivery payload —
            # this is the ONLY post-gating path to an entitlement-backed text
            # secret (file/license also get download_url/license_key below).
            "delivery": delivery_row.payload if delivery_row else DEFAULT_DELIVERY,
        }
        tier_name = _membership_tier_name(session, order)
        if tier_name:
            item["tier"] = tier_name
        _augment_gated(session, order, item)
        purchases.append(item)
    return {"wallet": wallet, "purchases": purchases}


class EvaluateBody(BaseModel):
    order_id: str = Field(min_length=1, max_length=32)
    verdict: Literal["confirm", "reject"]


@app.post("/api/library/evaluate")
@limiter.limit("30/minute")
def library_evaluate(
    request: Request, body: EvaluateBody, session: Session = Depends(get_session)
):
    """The paying buyer confirms or disputes a delivered order (Phase 1.6). Gated by
    the same wallet-signature session as /api/library, and only for an order whose
    on-chain payer IS the authenticated wallet. Reputation-only: it never moves funds
    (a dispute is not a refund) — it feeds success_rate. Idempotent: a settled
    evaluation is not re-opened; an unknown/foreign order is an opaque 404."""
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    try:
        wallet = delivery.load_session_token(_bearer(request))
    except SignatureExpired:
        raise HTTPException(401, "session expired") from None
    except BadSignature:
        raise HTTPException(401, "invalid session") from None
    order = session.get(Order, body.order_id)
    if (
        order is None
        or order.from_addr is None
        or order.from_addr.lower() != wallet
        or order.status not in checkout.TERMINAL_DELIVERED
    ):
        raise HTTPException(404, "order not found")
    if order.eval_status not in ("none", "pending"):
        raise HTTPException(409, "order already evaluated")
    order.eval_status = "confirmed" if body.verdict == "confirm" else "rejected"
    log_event(
        session,
        "api",
        f"order.eval_{order.eval_status}",
        store_id=order.store_id,
        order_id=order.id,
    )
    session.commit()
    return {"order_id": order.id, "eval_status": order.eval_status}


class ReviewBody(BaseModel):
    order_id: str = Field(min_length=1, max_length=32)
    rating: StrictInt = Field(ge=1, le=5)
    body: str = Field(min_length=1, max_length=2000)


@app.post("/api/library/review")
@limiter.limit("30/minute")
def library_review(
    request: Request, body: ReviewBody, session: Session = Depends(get_session)
):
    """A verified buyer writes ONE review for a delivered order they paid for (Phase
    3). Gated by the same wallet-signature session as /api/library, and un-fakeable:
    the order must exist, be delivered (TERMINAL_DELIVERED), and its on-chain payer
    must equal the authenticated wallet — else an opaque 404 (no order oracle). The
    body is Warden-screened before insert (BLOCK -> 422, unavailable -> 503) so no
    unscreened text is stored; UNIQUE(order_id) makes a second review a 409.
    Reputation-only: it never moves funds."""
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    try:
        wallet = delivery.load_session_token(_bearer(request))
    except SignatureExpired:
        raise HTTPException(401, "session expired") from None
    except BadSignature:
        raise HTTPException(401, "invalid session") from None
    order = session.get(Order, body.order_id)
    if (
        order is None
        or order.from_addr is None
        or order.from_addr.lower() != wallet
        or order.status not in checkout.TERMINAL_DELIVERED
    ):
        raise HTTPException(404, "order not found")
    text_body = body.body.strip()
    if not text_body:
        raise HTTPException(422, "review body is empty")
    # Screen fail-closed BEFORE any row is written: BLOCK -> 422, unavailable -> 503.
    try:
        outcome = screen(text_body)
    except ScreeningBlocked as exc:
        raise HTTPException(422, "content did not pass safety screening") from exc
    if outcome.status != "allow":
        raise HTTPException(503, "content screening temporarily unavailable")
    review = Review(
        store_id=order.store_id,
        product_id=order.product_id,
        order_id=order.id,
        from_addr=order.from_addr.lower(),
        rating=body.rating,
        body=text_body,
    )
    try:
        with session.begin_nested():
            session.add(review)
    except IntegrityError:
        # UNIQUE(order_id): this completed order is already reviewed.
        raise HTTPException(409, "order already reviewed") from None
    log_event(
        session, "api", "order.reviewed", store_id=order.store_id, order_id=order.id
    )
    session.commit()
    return {"order_id": order.id, "rating": review.rating}


class WaitlistBody(BaseModel):
    email: str = Field(min_length=3, max_length=255)


@app.post("/api/stores/{slug}/waitlist")
@limiter.limit("5/minute")
def store_waitlist(
    request: Request,
    body: WaitlistBody,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Public store-level subscriber capture (unauthenticated). Live stores only —
    a pending/blocked store collects nothing (404). CRLF-stripped, valid_email +
    255-cap validated, lowercased. A duplicate returns the same silent {ok:true}
    (no membership oracle); the per-store cap 429s a full list. Capture + export
    only — ANY sending stays dormant (SMTP unset)."""
    store = session.scalar(select(Store).where(Store.slug == slug))
    if store is None or store.status != "live":
        raise HTTPException(404, "store not found")
    email = body.email.replace("\r", "").replace("\n", "").strip().lower()
    if not delivery.valid_email(email):
        raise HTTPException(422, "invalid email address")
    active = session.scalar(
        select(func.count())
        .select_from(EmailSubscriber)
        .where(
            EmailSubscriber.store_id == store.id,
            EmailSubscriber.removed_at.is_(None),
        )
    )
    if active >= config.TILLA_SUBSCRIBERS_MAX:
        raise HTTPException(429, "subscriber list is full")
    try:
        with session.begin_nested():
            session.add(
                EmailSubscriber(store_id=store.id, email=email, source="waitlist")
            )
    except IntegrityError:
        # UNIQUE(store_id, email): already subscribed — silent no-op (no oracle).
        return {"ok": True}
    log_event(session, "api", "subscriber.captured", store_id=store.id)
    session.commit()
    return {"ok": True}


@app.get("/api/affiliate/summary")
@limiter.limit("30/minute")
def affiliate_summary(request: Request, session: Session = Depends(get_session)):
    """A referring agent's OWN rev-share balance, gated by the buyer wallet-signature
    session machinery (they sign as the referrer address). No public amount oracle:
    the balance is only ever returned to the wallet that earned it."""
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    try:
        wallet = delivery.load_session_token(_bearer(request))
    except SignatureExpired:
        raise HTTPException(401, "session expired") from None
    except BadSignature:
        raise HTTPException(401, "invalid session") from None
    data = affiliates.referrer_summary(session, wallet)
    for key in ("accrued_micro", "void_micro", "paid_micro"):
        data["totals"][key.replace("_micro", "_usdt")] = dashboard.usdt(
            data["totals"][key]
        )
    for st in data["stores"]:
        for key in ("accrued_micro", "void_micro", "paid_micro"):
            st[key.replace("_micro", "_usdt")] = dashboard.usdt(st[key])
    return data


@app.post("/api/checkout/{cid}/email")
@limiter.limit("10/minute")
def checkout_email(
    request: Request,
    body: EmailBody,
    cid: str,
    session: Session = Depends(get_session),
):
    """Capture a buyer email (exchange-custody fallback) for a non-refunded order,
    gated on proof the caller owns the order (payer wallet session or store manage
    key); if the order is already delivered, a re-delivery magic link is minted and
    mailed (a no-op that logs when SMTP is unconfigured)."""
    order = session.get(Order, cid)
    if order is None:
        raise HTTPException(404, "checkout not found")
    if not _authorize_order_email(request, session, order):
        raise HTTPException(401, "authentication required")
    if order.status == "refunded":
        raise HTTPException(409, "order refunded")
    email = body.email.replace("\r", "").replace("\n", "").strip()
    if not delivery.valid_email(email):
        raise HTTPException(422, "invalid email address")
    order.buyer_email = email
    log_event(session, "api", "order.email_set", store_id=order.store_id, order_id=cid)
    if order.status in checkout.TERMINAL_DELIVERED and config.SIGNING_KEY:
        token = delivery.mint_redeliver_token(order.id)
        delivery.send_redelivery_email(
            session, order, f"{config.PUBLIC_BASE_URL}/api/redeliver/{token}"
        )
    session.commit()
    return {"ok": True}


@app.get("/api/redeliver/{token}")
@limiter.limit("30/minute")
def redeliver(request: Request, token: str, session: Session = Depends(get_session)):
    """Magic-link re-delivery: returns the same delivery payload plus a fresh
    signed download link, drawing on the SAME entitlement budget (multiplies
    nothing)."""
    if not config.SIGNING_KEY:
        raise HTTPException(503, "gated delivery not configured")
    try:
        order_id = delivery.load_redeliver_token(token)
    except SignatureExpired:
        raise HTTPException(410, "link expired") from None
    except BadSignature:
        raise HTTPException(403, "invalid link") from None
    order = session.get(Order, order_id)
    if order is None:
        raise HTTPException(404, "order not found")
    if order.status == "refunded":
        raise HTTPException(410, "order refunded")
    if order.status not in checkout.TERMINAL_DELIVERED:
        raise HTTPException(409, "order not yet delivered")
    # Possession of the signed, salted, 7-day magic-link token IS the capability
    # (it is only ever mailed to a payer-authorized email), so serve the full
    # payload + gated keys — the exchange-buyer path keeps working post-gating.
    return _order_response(session, order, include_gated=True)


def _license_context(session: Session, license_key: str):
    """Resolve a license key to (entitlement, deliverable, order) iff it exists,
    is unrevoked, sits on a terminal order, and is a license deliverable. None
    otherwise — callers return a uniform {"valid": false} (no exists-oracle)."""
    ent = session.scalar(
        select(Entitlement).where(Entitlement.license_key == license_key)
    )
    if ent is None or ent.revoked_at is not None:
        return None
    order = session.get(Order, ent.order_id)
    if order is None or order.status not in checkout.TERMINAL_DELIVERED:
        return None
    deliverable = session.get(Deliverable, ent.deliverable_id)
    if deliverable is None or deliverable.kind != "license":
        return None
    return ent, deliverable, order


@app.post("/api/licenses/activate")
@limiter.limit("30/minute")
def license_activate(
    request: Request,
    body: LicenseActionBody,
    session: Session = Depends(get_session),
):
    ctx = _license_context(session, body.license_key)
    if ctx is None:
        return {"valid": False}
    ent, deliverable, _ = ctx
    max_act = deliverable.max_activations or config.LICENSE_ACTIVATIONS_DEFAULT
    result = delivery.activate_license(session, ent, body.device_id, max_act)
    if result == "at_limit":
        session.rollback()
        raise HTTPException(409, "activation limit reached")
    session.commit()
    session.refresh(ent)
    return {
        "valid": True,
        "status": result,
        "activations": ent.activations_used,
        "max": max_act,
    }


@app.post("/api/licenses/validate")
@limiter.limit("30/minute")
def license_validate(
    request: Request,
    body: LicenseValidateBody,
    session: Session = Depends(get_session),
):
    ctx = _license_context(session, body.license_key)
    if ctx is None:
        return {"valid": False}
    ent, deliverable, _ = ctx
    max_act = deliverable.max_activations or config.LICENSE_ACTIVATIONS_DEFAULT
    if body.device_id and not delivery.device_active(session, ent, body.device_id):
        return {"valid": False, "activations": ent.activations_used, "max": max_act}
    return {"valid": True, "activations": ent.activations_used, "max": max_act}


@app.post("/api/licenses/deactivate")
@limiter.limit("30/minute")
def license_deactivate(
    request: Request,
    body: LicenseActionBody,
    session: Session = Depends(get_session),
):
    ctx = _license_context(session, body.license_key)
    if ctx is None:
        return {"valid": False}
    ent, _, _ = ctx
    freed = delivery.deactivate_license(session, ent, body.device_id)
    session.commit()
    session.refresh(ent)
    return {"valid": True, "deactivated": freed, "activations": ent.activations_used}


# Test-only checkout confirmation shim. Registered ONLY when TILLA_TEST=1 so the
# route cannot exist in production at all, not merely be gated at call time.
if os.environ.get("TILLA_TEST") == "1":

    @app.post("/api/_test/mark/{cid}")
    def _test_mark(cid: str, session: Session = Depends(get_session)):
        order = session.get(Order, cid)
        if order is None:
            raise HTTPException(404, "no checkout")
        # Simulate an exact on-chain payment at confirmation depth without touching
        # the network: confirm + deliver via the real state-machine path.
        added = order.expected_micro - (order.paid_micro or 0)
        checkout.apply_transfer(
            session,
            order,
            added,
            tx_hash="0x" + "9" * 64,
            log_index=0,
            block_number=1,
            from_addr="0x" + "9" * 40,
            head=10**9,
        )
        session.commit()
        return {"ok": True}


# ---------- x402 paywall on /create-store + per-store /s/{slug}/buy ----------
if os.getenv("OKX_API_KEY"):
    import httpx
    from starlette.middleware.base import BaseHTTPMiddleware
    from x402.http import OKXAuthConfig, OKXFacilitatorConfig
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    from x402.http.types import RouteConfig
    from x402.mechanisms.evm.exact.server import ExactEvmScheme
    from x402.server import x402ResourceServer

    from app.payment import (
        NoRedirectOKXFacilitatorClient,
        build_fee_payment_option,
        build_payment_option,
        build_store_payment_options,
        load_payment_rail,
    )

    _rail = load_payment_rail(os.environ)
    _http = httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=False)
    _fac = NoRedirectOKXFacilitatorClient(
        OKXFacilitatorConfig(
            auth=OKXAuthConfig(
                api_key=os.getenv("OKX_API_KEY", ""),
                secret_key=os.getenv("OKX_SECRET_KEY", ""),
                passphrase=os.getenv("OKX_PASSPHRASE", ""),
            ),
            base_url=_rail.facilitator_url,
            sync_settle=True,
            http_client=_http,
        )
    )
    _srv = x402ResourceServer(_fac)
    _srv.register(_rail.network, ExactEvmScheme())
    # aggr_deferred (batch buys) is registered ONLY behind its flag: a route whose
    # accepts lists an unregistered/unsupported scheme makes initialize() raise
    # RouteConfigurationError -> 502 on every protected route. The flag is flipped
    # only after the read-only /supported probe confirms the scheme on eip155:196.
    if config.AGGR_DEFERRED_ENABLED:
        from x402.mechanisms.evm.deferred.server import AggrDeferredEvmScheme

        _srv.register(_rail.network, AggrDeferredEvmScheme())
    _route = RouteConfig(
        accepts=[build_payment_option(_rail)],
        description="Tilla — create a live crypto storefront on X Layer",
        mime_type="application/json",
        unpaid_response_body=agentic.unpaid_schema_body(
            CreateStoreBody.model_json_schema(),
            "POST {description, theme} with an x402 payment to create a live "
            "storefront. Every field is optional: an empty or absent body "
            "creates a sample store.",
            # The sample the agent card already publishes, kept identical so the
            # card and the challenge cannot disagree.
            {
                "description": "single-origin coffee beans, roasted to order",
                "theme": "original",
            },
        ),
    )
    # ':slug' compiles to [^/]+ (no cross-slash match). pay_to + price resolve
    # per request from the DB; a settle failure runs the compensating hook. The
    # accepts list is [exact] (flag off) or [exact, aggr_deferred] (flag on).
    _store_route = RouteConfig(
        accepts=build_store_payment_options(_rail),
        description="Tilla store purchase",
        mime_type="application/json",
        hook_timeout_seconds=5.0,
        settlement_failed_response_body=agentic.store_settle_failed_hook,
    )
    # M10 platform-fee services (static exact options, payTo = Tilla's own rail —
    # Tilla earning its platform fee). Both register POST and GET: onchainos'
    # x402-check probes GET and expects a 402 challenge (the Warden 405->402
    # lesson); a paid GET with no body returns service-usage JSON.
    _upgrade_route = RouteConfig(
        accepts=[build_fee_payment_option(_rail, "30000")],
        description="Tilla — upgrade an existing storefront (0.03 USDT)",
        mime_type="application/json",
        unpaid_response_body=agentic.unpaid_schema_body(
            UpgradeStoreBody.model_json_schema(),
            "Owner-only: requires the store's manage_key as "
            "'Authorization: Bearer <manage_key>', minted to whoever paid to "
            "create it. Omitting description regenerates from the store's "
            "existing one.",
        ),
    )
    # Priced from ONE constant (config.VERIFY_DELIVERY_FEE_MICRO) shared with the
    # agent card, well under the 0.01 the marketplace's evidence services charge:
    # this is a per-check lookup an agent may run many times, not a one-off purchase.
    _verify_route = RouteConfig(
        accepts=[
            build_fee_payment_option(_rail, str(config.VERIFY_DELIVERY_FEE_MICRO))
        ],
        description="Tilla — verify a delivery: signed evidence of what was sold",
        mime_type="application/json",
        unpaid_response_body=agentic.unpaid_schema_body(
            VerifyDeliveryBody.model_json_schema(),
            "POST any one of {order_id, tx_hash, attestation_uid} to get signed "
            "evidence for that delivered order: content hash, buyer accept/dispute "
            "state, refunds, plus on-chain anchors you can verify yourself. "
            "Re-check any bundle free at POST /api/verify-evidence.",
            {"tx_hash": "0x" + "ab" * 32},
        ),
    )
    _add_product_route = RouteConfig(
        accepts=[build_fee_payment_option(_rail, "10000")],
        description="Tilla — add a product to a storefront (0.01 USDT)",
        mime_type="application/json",
        unpaid_response_body=agentic.unpaid_schema_body(
            AddProductBody.model_json_schema(),
            "Owner-only: requires the store's manage_key as "
            "'Authorization: Bearer <manage_key>', minted to whoever paid to "
            "create it.",
        ),
    )
    _paid = {
        "POST /create-store": _route,
        "GET /create-store": _route,
        "POST /upgrade-store": _upgrade_route,
        "GET /upgrade-store": _upgrade_route,
        "POST /add-product": _add_product_route,
        "GET /add-product": _add_product_route,
        "POST /verify-delivery": _verify_route,
        # GET registered for the same reason as the others: onchainos' x402-check
        # probes GET and expects a challenge, and the handler is POST-only.
        "GET /verify-delivery": _verify_route,
        "POST /s/:slug/buy": _store_route,
        # Register GET so an UNPAID GET on the buy path returns the 402 challenge
        # (listing-review robustness); a PAID GET reaches agentic.agent_buy_get and
        # is refused 405 BEFORE settle, so zero funds can move on the GET method.
        "GET /s/:slug/buy": _store_route,
        # Per-product buy: same dynamic resolvers (resolve_price/resolve_pay_to read
        # the product id from the path), so an agent can x402-buy ANY product, not
        # just the primary. POST + GET registered for the same reasons as the bare
        # buy route above.
        "POST /s/:slug/buy/:product_id": _store_route,
        "GET /s/:slug/buy/:product_id": _store_route,
    }
    app.add_middleware(PaymentMiddlewareASGI, routes=_paid, server=_srv)
    # Registered AFTER the payment middleware so it is OUTERMOST (runs first): it
    # 404/409s a dead store before any payable 402 is emitted and records the
    # settle tx_hash on the way back.
    app.add_middleware(BaseHTTPMiddleware, dispatch=agentic.agent_guard_dispatch)
