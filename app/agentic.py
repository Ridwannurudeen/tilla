"""M7 dual-sided commerce: the agent-facing surface of a Tilla store.

Everything here is app-served (nginx routes these four per-store paths + the two
discovery paths + the well-known agent card past the static ``/s/`` alias):

- ``POST /s/{slug}/buy``  — x402 per-store purchase. The x402 middleware (wired in
  ``app.main`` only when ``OKX_API_KEY`` is set) resolves the merchant payTo +
  product price PER REQUEST via :func:`resolve_pay_to` / :func:`resolve_price`,
  verifies + settles the EIP-3009 payment, and this handler mints the deliverable
  through the exact M3/M4 ``checkout.deliver`` path. NON-CUSTODIAL: payTo is always
  the merchant wallet, never Tilla.
- ``GET/POST /s/{slug}/mcp`` — a hand-rolled stateless JSON-RPC 2.0 MCP server.
- ``GET /s/{slug}/feed.json`` / ``llms.txt`` — machine-readable catalog.
- ``GET /.well-known/agent-card.json`` — A2A card for Tilla as a whole.
- ``GET /discovery/resources`` + ``/discovery/search`` — Tilla-wide store index.
- ``POST /mcp`` — the Tilla-wide root MCP: browse/search stores + list a store's
  products, so one agent runtime is the concierge across every store.

Every JSON surface is a ``JSONResponse`` (json-encoded, no HTML context) and every
text surface is ``text/plain``; all carry ``X-Content-Type-Options: nosniff``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from datetime import timedelta
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, PlainTextResponse, Response
from x402.http.types import HTTPResponseBody
from x402.http.utils import (
    decode_payment_required_header,
    decode_payment_response_header,
    decode_payment_signature_header,
    encode_payment_required_header,
)
from x402.schemas import AssetAmount

from app import affiliates, chain, checkout, config, delivery, webhooks
from app.db import SessionLocal, get_session
from app.limiter import limiter
from app.models import (
    Deliverable,
    Delivery,
    Entitlement,
    Order,
    Product,
    Store,
    log_event,
)
from app.payment import (
    PAYMENT_ASSET,
    PAYMENT_EIP712_NAME,
    PAYMENT_EIP712_VERSION,
    PAYMENT_NETWORK,
    PAYMENT_SCHEME_AGGR_DEFERRED,
)

logger = logging.getLogger("tilla")

router = APIRouter()

CURRENCY = "USDT"
NETWORK = PAYMENT_NETWORK  # "eip155:196"
ASSET = PAYMENT_ASSET  # USDT0 on X Layer
SERVICE = "tilla"
AGENT_ID = 6961  # Tilla's ERC-8004 agent id (OKX ASP #6961)
TILLA_VERSION = "0.1.0"
# Delivery-time promise surfaced to agent buyers (the ACP 'SLA' analog). Digital
# goods deliver on payment confirmation and store generation completes within the
# request, so 10 minutes is a conservative honest upper bound covering both.
DELIVERY_SLA_MINUTES = 10
MCP_PROTOCOL_VERSION = "2025-06-18"

# Short cache + nosniff on every machine surface. Stored content is Warden-screened
# and only ever json-encoded or emitted as text/plain, so there is no HTML context
# to escape; nosniff stops a client from re-typing the body.
_AGENT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=300",
}

# The one path the agent-guard middleware + resolvers key off. ':slug' in the x402
# route pattern compiles to the same [^/]+ (no cross-slash match); the optional
# trailing slash is tolerated here though FastAPI's route never forwards one.
_BUY_PATH_RE = re.compile(r"^/s/([a-z0-9][a-z0-9-]{0,39})/buy(?:/([0-9]{1,18}))?/?$")

# The reaper voids agent orders stuck in the provisional 'settling' status (a
# crash or lost settle between the deliver-commit and the settle-confirm: the goods
# were never claimable out-of-band, so voiding is correct). A settled order is
# flipped to 'delivered' by record_settlement and is never reaped.
REAP_AFTER = timedelta(minutes=15)
REAP_INTERVAL = 300  # seconds


# ============================================================================
# Slug / content helpers
# ============================================================================
def _slug_from_path(path: str) -> str | None:
    m = _BUY_PATH_RE.match(path)
    return m.group(1) if m else None


def _live_store(session: Session, slug: str) -> Store | None:
    store = session.scalar(select(Store).where(Store.slug == slug))
    if store is None or store.status != "live":
        return None
    return store


def _require_live_store(session: Session, slug: str) -> Store:
    """404 for unknown/blocked, 409 for pending_screening, else the live store.
    Mirrors the human ``/api/checkout/{slug}`` gate exactly."""
    store = session.scalar(select(Store).where(Store.slug == slug))
    if store is None:
        raise HTTPException(404, "store not found")
    if store.status == "pending_screening":
        raise HTTPException(409, "store is not yet live (pending content screening)")
    if store.status != "live":
        raise HTTPException(404, "store not found")
    return store


def _active_product(session: Session, store_id: int) -> Product | None:
    return session.scalar(
        select(Product)
        .where(Product.store_id == store_id, Product.active.is_(True))
        .order_by(Product.id)
    )


def _product_id_from_path(path: str) -> int | None:
    """The product id in a ``/s/<slug>/buy/<id>`` path, or None for a bare
    ``/buy`` (primary product)."""
    m = _BUY_PATH_RE.match(path)
    return int(m.group(2)) if m and m.group(2) else None


def _product_for_path(session: Session, store: Store, path: str) -> Product | None:
    """The product a ``/buy`` path targets: the ``/buy/<id>`` product — validated
    to be an ACTIVE product of THIS store — or the primary product for a bare
    ``/buy``. Returns None if the path names an id that isn't an active product of
    the store (so the resolvers fall back to the sentinel and the handler 404s
    BEFORE settle, exactly like an unknown store)."""
    pid = _product_id_from_path(path)
    if pid is None:
        return _active_product(session, store.id)
    return session.scalar(
        select(Product).where(
            Product.id == pid,
            Product.store_id == store.id,
            Product.active.is_(True),
        )
    )


def _is_batch_store(slug: str) -> bool:
    """True iff the store's active product declares pricing_model='batch'. The
    agent-guard uses it to decide whether the aggr_deferred accepts-entry stays in
    the 402 challenge. Any unknown/non-live store or DB error returns False, so the
    aggr entry is stripped (fail-safe: never over-advertise a rail)."""
    try:
        with SessionLocal() as session:
            store = _live_store(session, slug)
            if store is None:
                return False
            product = _active_product(session, store.id)
            return (
                product is not None and (product.pricing_model or "one_time") == "batch"
            )
    except Exception:
        logger.exception("aggr batch-check failed for %s", slug)
        return False


def _payment_scheme(request: Request) -> str | None:
    """The scheme the payment middleware matched for this paid request — the
    server-matched requirements (what will actually settle), falling back to the
    buyer's claimed accepted scheme."""
    reqs = getattr(request.state, "payment_requirements", None)
    scheme = getattr(reqs, "scheme", None)
    if scheme:
        return scheme
    payload = getattr(request.state, "payment_payload", None)
    accepted = getattr(payload, "accepted", None)
    return getattr(accepted, "scheme", None)


def _filter_aggr_from_challenge(header_value: str) -> str | None:
    """Drop aggr_deferred entries from a base64 PAYMENT-REQUIRED header, returning
    the re-encoded header — or None when nothing changed or it could not be decoded
    (fail-open to the handler's hard gate, which 409s a non-batch aggr payment
    BEFORE settle, so zero funds move regardless of the challenge)."""
    try:
        pr = decode_payment_required_header(header_value)
    except Exception:
        logger.exception("aggr guard: undecodable PAYMENT-REQUIRED")
        return None
    kept = [
        a
        for a in pr.accepts
        if getattr(a, "scheme", None) != PAYMENT_SCHEME_AGGR_DEFERRED
    ]
    if len(kept) == len(pr.accepts):
        return None
    pr.accepts = kept
    return encode_payment_required_header(pr)


def _content(store: Store) -> dict:
    return store.content if isinstance(store.content, dict) else {}


def _store_name(store: Store) -> str:
    return str(_content(store).get("store_name") or store.slug)


def _store_description(store: Store) -> str:
    c = _content(store)
    return str(c.get("hero_subcopy") or c.get("tagline") or store.description or "")


def _product_description(store: Store) -> str:
    return str(_content(store).get("product_blurb") or "")


def _usdt_str(micro: int) -> str:
    """Exact decimal USDT string from integer micro-units (no float drift)."""
    return str(Decimal(micro) / Decimal(1_000_000))


def _pricing_model(product: Product) -> str:
    return product.pricing_model or "one_time"


def enabled_schemes(product: Product) -> list[str]:
    """The x402 schemes actually OFFERED on this product's /buy route, given the
    live server flags. 'exact' is always present; 'aggr_deferred' appears ONLY for
    a batch product AND only when TILLA_AGGR_DEFERRED is on — so feeds/MCP never
    advertise a rail that cannot settle. Metered (MPP) and subscription (period)
    are separate endpoints, not x402 buy schemes, so they do not appear here."""
    schemes = ["exact"]
    if _pricing_model(product) == "batch" and config.AGGR_DEFERRED_ENABLED:
        schemes.append("aggr_deferred")
    return schemes


def _pricing_block(product: Product) -> dict:
    params = product.pricing_params if isinstance(product.pricing_params, dict) else {}
    return {"model": _pricing_model(product), "params": params}


# ============================================================================
# Per-request x402 resolvers (spike-8 dynamic accepts) — NEVER raise
# ============================================================================
def _sentinel_price() -> AssetAmount:
    return AssetAmount(
        amount="1",
        asset=ASSET,
        extra={"name": PAYMENT_EIP712_NAME, "version": PAYMENT_EIP712_VERSION},
    )


def resolve_pay_to(path: str, sentinel_pay_to: str) -> str:
    """Merchant payTo for the store in ``path`` — byte-identical to stores.pay_to
    (no .lower(), no reformat: find_matching_requirements matches on it, so the
    402 challenge and the paid retry MUST agree). Any unknown/non-live store or DB
    error returns the deterministic sentinel (Tilla's own PAY_TO_ADDRESS) rather
    than raising (a raising hook returns HTTP 500, not 404)."""
    try:
        slug = _slug_from_path(path)
        if slug is None:
            return sentinel_pay_to
        with SessionLocal() as session:
            store = _live_store(session, slug)
            if store is None:
                return sentinel_pay_to
            return store.pay_to
    except Exception:
        logger.exception("resolve_pay_to failed for %s", path)
        return sentinel_pay_to


def resolve_price(path: str) -> AssetAmount:
    """Exact product price as an AssetAmount for the store in ``path``. Any
    unknown/non-live store, missing product, or DB error returns the sentinel
    (amount '1') rather than raising."""
    try:
        slug = _slug_from_path(path)
        if slug is None:
            return _sentinel_price()
        with SessionLocal() as session:
            store = _live_store(session, slug)
            if store is None:
                return _sentinel_price()
            product = _product_for_path(session, store, path)
            if product is None:
                return _sentinel_price()
            return AssetAmount(
                amount=str(product.price_micro),
                asset=ASSET,
                extra={
                    "name": PAYMENT_EIP712_NAME,
                    "version": PAYMENT_EIP712_VERSION,
                },
            )
    except Exception:
        logger.exception("resolve_price failed for %s", path)
        return _sentinel_price()


# ============================================================================
# Agent buy handler (runs only after the x402 middleware verifies payment)
# ============================================================================
def _augment_agent_gated(session: Session, order: Order, body: dict) -> None:
    """Add the freshly minted gated key for an entitlement-backed deliverable:
    ``license_key`` (license) or ``download_url`` (file, under budget). The full
    goods surface is justified here — the payment is cryptographically bound to
    this exact request (mirrors ``main._augment_gated``)."""
    ent = session.scalar(select(Entitlement).where(Entitlement.order_id == order.id))
    if ent is None or ent.revoked_at is not None:
        return
    deliverable = session.get(Deliverable, ent.deliverable_id)
    if deliverable is None:
        return
    if deliverable.kind == "license" and ent.license_key:
        body["license_key"] = ent.license_key
    elif deliverable.kind == "file" and ent.download_count < deliverable.max_downloads:
        with contextlib.suppress(delivery.SigningUnavailable):
            token = delivery.mint_download_token(ent.id, deliverable.id)
            body["download_url"] = delivery.download_url(token)


def _agent_buy_body(
    session: Session, order: Order, store: Store, product: Product | None
) -> dict:
    delivery_row = session.scalar(select(Delivery).where(Delivery.order_id == order.id))
    body = {
        "order_id": order.id,
        "product": product.name if product else None,
        "amount": order.expected_micro / 1e6,
        "amount_micro": order.expected_micro,
        "kind": delivery_row.kind if delivery_row else "text",
        "delivery": delivery_row.payload if delivery_row else checkout.DEFAULT_DELIVERY,
        "tx": order.tx_hash or "pending",
    }
    _augment_agent_gated(session, order, body)
    return body


def _assert_nonce_owner(order: Order, store: Store, payer: str) -> None:
    """Scope the nonce idempotency to exactly one (store, payer). The EIP-3009
    nonce is globally unique on-chain but PUBLIC (it appears in the settle tx
    calldata), and unique only per payer. A match on the nonce alone would let a
    different payer replay a victim's nonce on the same store to receive the
    victim's deliverable, or a nonce first used on store A be replayed against
    store B. On any mismatch raise 409 so the payment middleware skips settlement
    (>=400): the signed authorization is never executed, zero funds move."""
    if (
        order.store_id != store.id
        or (order.from_addr or "").lower() != (payer or "").lower()
    ):
        raise HTTPException(409, "x402 nonce already used")


def fulfill_agent_order(
    session: Session,
    store: Store,
    product: Product,
    payer: str,
    nonce: str,
    referrer_addr: str | None = None,
) -> tuple[Order, dict]:
    """Idempotently create + deliver an agent order. The (store, payer, nonce)
    triple is the idempotency key (see :func:`_assert_nonce_owner`): an existing
    order for this payer+store returns the stored deliverable (no new rows). A
    fresh order is born 'confirmed' at the EXACT price (offset 0, so it never
    collides with a human order's price+[1..4999]) and delivered through the exact
    M3/M4 ``checkout.deliver`` path (idempotent Delivery + Entitlement), then held
    in a NON-TERMINAL 'settling' status until the x402 settle confirms on-chain:
    the out-of-band claim paths (library, download, license, redeliver) all gate on
    a TERMINAL_DELIVERED status, so the goods stay unreachable until
    :func:`record_settlement` flips settling -> delivered. The reaper voids orders
    stuck 'settling'."""
    existing = session.scalar(select(Order).where(Order.x402_nonce == nonce))
    if existing is not None:
        _assert_nonce_owner(existing, store, payer)
        return existing, _agent_buy_body(session, existing, store, product)
    order = Order(
        id=uuid.uuid4().hex[:16],
        store_id=store.id,
        product_id=product.id,
        pay_to=store.pay_to,
        amount_micro=product.price_micro,
        expected_micro=product.price_micro,
        status="confirmed",
        channel="agent",
        x402_nonce=nonce,
        from_addr=payer or None,
        referrer_addr=referrer_addr,
        paid_at=checkout._now(),
    )
    session.add(order)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent request inserted the same nonce first (single-use on-chain,
        # so this is a duplicate submission): reconcile to the committed winner,
        # asserting it belongs to this payer+store (a busy-timeout rollback can
        # leave the re-select empty -> 409 rather than an AttributeError 500).
        session.rollback()
        existing = session.scalar(select(Order).where(Order.x402_nonce == nonce))
        if existing is None:
            raise HTTPException(409, "x402 nonce reconcile failed") from None
        _assert_nonce_owner(existing, store, payer)
        return existing, _agent_buy_body(session, existing, store, product)
    log_event(
        session, "agentic", "agent_order.created", store_id=store.id, order_id=order.id
    )
    checkout.deliver(session, order)
    # Hold the delivered order in a non-terminal 'settling' status: the goods are
    # minted (in-band body is cryptographically bound to this paid request) but
    # every out-of-band claim path gates on TERMINAL_DELIVERED, so nothing is
    # claimable until settle confirms.
    checkout.transition(session, order.id, checkout.TERMINAL_DELIVERED, "settling")
    log_event(
        session,
        "agentic",
        "agent_order.settling",
        store_id=store.id,
        order_id=order.id,
    )
    return order, _agent_buy_body(session, order, store, product)


def _do_agent_buy(
    request: Request, session: Session, slug: str, ref: str | None
) -> JSONResponse:
    """Shared body for POST /s/{slug}/buy and /s/{slug}/buy/{product_id}. The
    product is resolved from the request PATH (bare /buy = primary; /buy/{id} =
    that product), the SAME resolution ``resolve_price`` used to build the 402
    challenge — so the price the agent paid always equals what is charged here."""
    # FAIL CLOSED: no verified payment (middleware absent — OKX_API_KEY unset — or
    # payment not provided) → 402. Goods are never served free.
    payload = getattr(request.state, "payment_payload", None)
    if payload is None:
        raise HTTPException(402, "payment required")
    # M13 affiliate attribution: the ?ref= query param survives the 402->paid-retry
    # roundtrip. Validate BEFORE settlement so a malformed ref makes this handler
    # return >=400 (middleware skips settle, zero funds move) rather than settling a
    # sale that can never be attributed.
    try:
        referrer_addr = affiliates.normalize_ref(ref)
    except affiliates.RefRejected as exc:
        raise HTTPException(400, str(exc)) from exc
    # RACE re-check: if the store flipped non-live between challenge and retry (or
    # a sentinel challenge got paid), return >=400 so the middleware SKIPS
    # settlement — the signed authorization is never executed, zero funds move.
    store = _require_live_store(session, slug)
    product = _product_for_path(session, store, request.url.path)
    if product is None:
        # >=400 BEFORE settle -> middleware skips settle, zero funds move. A bare
        # /buy with no active product is 409; a /buy/{id} that names something that
        # isn't an active product of THIS store is 404 (no wrong charge, no IDOR).
        if _product_id_from_path(request.url.path) is None:
            raise HTTPException(409, "store has no active product")
        raise HTTPException(404, "product not found")
    # Pay-time HARD GATE: an aggr_deferred payment against a non-batch product is
    # refused BEFORE settlement (same funds-safe >=400-skips-settle pattern).
    if (
        _payment_scheme(request) == PAYMENT_SCHEME_AGGR_DEFERRED
        and (product.pricing_model or "one_time") != "batch"
    ):
        raise HTTPException(409, "aggr_deferred is only available for batch products")
    auth = (
        payload.payload.get("authorization")
        if isinstance(payload.payload, dict)
        else None
    )
    if not isinstance(auth, dict) or not auth.get("nonce"):
        raise HTTPException(400, "missing authorization nonce")
    order, body = fulfill_agent_order(
        session, store, product, auth.get("from") or "", auth["nonce"], referrer_addr
    )
    session.commit()
    # Shared scope["state"] hands the order id to the outer agent-guard middleware
    # for settle-success tx_hash bookkeeping.
    request.state.agent_order_id = order.id
    return JSONResponse(body)


@router.post("/s/{slug}/buy")
@limiter.limit("60/minute")
def agent_buy(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
    ref: str | None = None,
):
    return _do_agent_buy(request, session, slug, ref)


@router.post("/s/{slug}/buy/{product_id}")
@limiter.limit("60/minute")
def agent_buy_product(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    product_id: int = Path(..., ge=1),
    session: Session = Depends(get_session),
    ref: str | None = None,
):
    return _do_agent_buy(request, session, slug, ref)


@router.get("/s/{slug}/buy")
def agent_buy_get(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    """A GET on the buy path is registered on the x402 paywall so an UNPAID GET
    returns the 402 challenge (marketplace listing-review robustness — the
    onchainos x402-check probes GET). A PAID GET reaches here and is refused 405
    BEFORE settle: a >=400 response makes the payment middleware skip settlement, so
    zero funds can move on the GET method and no Order row is ever created. Buying is
    POST-only."""
    return JSONResponse(
        {"error": "method not allowed; use POST to buy"},
        status_code=405,
        headers={"Allow": "POST", **_AGENT_HEADERS},
    )


@router.get("/s/{slug}/buy/{product_id}")
def agent_buy_product_get(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    product_id: int = Path(..., ge=1),
):
    """Per-product GET twin of ``agent_buy_get``: a PAID GET is refused 405 BEFORE
    settle so zero funds can move on the GET method; an UNPAID GET returns the 402
    challenge for the specific product via the paywall."""
    return JSONResponse(
        {"error": "method not allowed; use POST to buy"},
        status_code=405,
        headers={"Allow": "POST", **_AGENT_HEADERS},
    )


# ============================================================================
# Settlement-failed hook + reaper: void a provisional 'settling' when settle fails
# ============================================================================
def settle_failed_core(nonce: str) -> dict:
    """Compensator for a settle failure on a just-delivered agent order. If the
    order is already in a terminal delivered status an earlier settle on this
    authorization landed (record_settlement ran — a replay after a lost response):
    return a NEUTRAL recovered receipt (tx + order id + the sign-to-claim message),
    NEVER the deliverable — the full EIP-3009 authorization is reconstructible from
    the public settle calldata, so the license key / download url / text secret
    stay behind the wallet-signature claim gate (/api/library as the paying
    from_addr). Otherwise the provisional 'settling' order is voided (→ canceled)
    and its entitlement revoked, killing the minted download token / license key."""
    with SessionLocal() as session:
        order = session.scalar(
            select(Order).where(Order.x402_nonce == nonce, Order.channel == "agent")
        )
        if order is None:
            return {"error": "settlement_failed"}
        if order.status in checkout.TERMINAL_DELIVERED:
            from app.main import CLAIM_DELIVERY_MESSAGE

            return {
                "recovered": True,
                "tx": order.tx_hash,
                "order_id": order.id,
                "message": CLAIM_DELIVERY_MESSAGE,
            }
        if checkout.transition(session, order.id, ("settling",), "canceled"):
            delivery.revoke_entitlement(session, order)
            log_event(
                session,
                "agentic",
                "agent_order.settle_failed",
                store_id=order.store_id,
                order_id=order.id,
            )
            store = session.get(Store, order.store_id)
            if store is not None:
                session.refresh(order)
                webhooks.enqueue(session, store.merchant_id, "order.voided", order)
            session.commit()
        return {"error": "settlement_failed"}


def _nonce_from_context(context) -> str | None:
    header = getattr(context, "payment_header", None)
    if not header:
        return None
    try:
        payload = decode_payment_signature_header(header)
        auth = (
            payload.payload.get("authorization")
            if isinstance(payload.payload, dict)
            else None
        )
        return auth.get("nonce") if isinstance(auth, dict) else None
    except Exception:
        return None


async def store_settle_failed_hook(context, failure) -> HTTPResponseBody:
    """x402 ``settlement_failed_response_body`` hook (async → off-loop). Recovers
    the nonce from the PAYMENT-SIGNATURE header and voids/recovers the order."""
    nonce = _nonce_from_context(context)
    body = {"error": "settlement_failed"}
    if nonce:
        try:
            body = await asyncio.to_thread(settle_failed_core, nonce)
        except Exception:
            logger.exception("settle_failed hook failed")
            body = {"error": "settlement_failed"}
    return HTTPResponseBody(content_type="application/json", body=body)


def reap_agent_orders(session: Session, now=None) -> int:
    """Void channel='agent' orders stuck in the provisional 'settling' status older
    than 15 min (a crash or lost settle between the deliver-commit and the
    settle-confirm). A settled order (flipped to 'delivered' by record_settlement,
    even when its PAYMENT-RESPONSE header was undecodable and tx_hash is NULL) and
    every human order are never touched — the reaper keys on 'settling', never on
    tx_hash, so a genuinely-paid order can never be clawed back."""
    now = now or checkout._now()
    cutoff = now - REAP_AFTER
    reaped = 0
    orders = session.scalars(
        select(Order).where(
            Order.channel == "agent",
            Order.status == "settling",
        )
    ).all()
    for o in orders:
        reached = checkout._naive(o.paid_at) or checkout._naive(o.created_at)
        if reached is not None and reached <= cutoff:
            if checkout.transition(session, o.id, ("settling",), "canceled"):
                delivery.revoke_entitlement(session, o)
                log_event(
                    session,
                    "agentic",
                    "agent_order.reaped",
                    store_id=o.store_id,
                    order_id=o.id,
                )
                store = session.get(Store, o.store_id)
                if store is not None:
                    session.refresh(o)
                    webhooks.enqueue(session, store.merchant_id, "order.voided", o)
                reaped += 1
    return reaped


def _reap_tick() -> None:
    with SessionLocal() as session:
        if reap_agent_orders(session):
            session.commit()


async def agent_reaper_loop() -> None:
    logger.info("tilla agent reaper loop started (interval=%ss)", REAP_INTERVAL)
    while True:
        try:
            await asyncio.to_thread(_reap_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent reaper tick failed")
        await asyncio.sleep(REAP_INTERVAL)


# ============================================================================
# Agent-guard middleware: 404/409 before any 402; settle-success tx bookkeeping
# ============================================================================
def _guard_store_status(slug: str) -> tuple[int, str] | None:
    """None ⇒ allow (live store, or a DB error → fail open to the payment
    middleware, which the handler re-checks anyway); else (status, detail)."""
    try:
        with SessionLocal() as session:
            store = session.scalar(select(Store).where(Store.slug == slug))
            if store is None:
                return (404, "store not found")
            if store.status == "pending_screening":
                return (409, "store is not yet live (pending content screening)")
            if store.status != "live":
                return (404, "store not found")
            return None
    except Exception:
        logger.exception("agent guard store-status check failed for %s", slug)
        return None


def record_settlement(
    order_id: str, payment_response_header: str, scheme: str | None = None
) -> None:
    """A served 200 with a PAYMENT-RESPONSE header IS the settle-success signal, so
    flip the provisional 'settling' order to 'delivered' — exposing the goods to
    the out-of-band claim paths — and record the settle tx_hash when the header
    decodes. For the EXACT rail the flip does NOT depend on the header decoding: an
    undecodable/empty transaction still delivers (logged for reconciliation)
    instead of leaving a genuinely-paid order in 'settling' for the reaper to void.

    For ``aggr_deferred`` that unparsed-header fallback is WRONG: the aggregated
    settle can succeed with no tx hash at serve time, and the real on-chain tx only
    exists after a later session/settle reconciliation. So without a decoded tx we
    do NOT flip to a terminal 'delivered' and do NOT log 'agent_order.settled'
    (which would claim settlement with no evidence) — the order stays provisional
    'settling' with a distinct non-terminal 'agent_order.settle_pending' marker
    until a reconciliation poll supplies the real tx hash. (That poll must ship
    before TILLA_AGGR_DEFERRED is ever enabled.) Idempotent (the conditional
    transition is a no-op once the order has left 'settling')."""
    tx = None
    try:
        settle = decode_payment_response_header(payment_response_header)
        tx = settle.transaction or None
    except Exception:
        logger.exception("record_settlement: undecodable PAYMENT-RESPONSE")
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        if order is None or order.channel != "agent":
            return
        if scheme == PAYMENT_SCHEME_AGGR_DEFERRED and tx is None:
            if order.status == "settling":
                log_event(
                    session,
                    "agentic",
                    "agent_order.settle_pending",
                    store_id=order.store_id,
                    order_id=order_id,
                )
                session.commit()
            return
        fields = {"tx_hash": tx} if tx else {}
        if checkout.transition(session, order.id, ("settling",), "delivered", **fields):
            log_event(
                session,
                "agentic",
                "agent_order.settled",
                store_id=order.store_id,
                order_id=order_id,
                data=None if tx else {"tx": "unparsed"},
            )
            # Fire the paid/delivered webhooks here (suppressed in checkout.deliver
            # for agent orders): the settle is now confirmed on-chain, so the
            # merchant's consumer counts revenue only for a settled buy.
            store = session.get(Store, order.store_id)
            if store is not None:
                session.refresh(order)
                # M11: queue for EAS attestation ONLY here, on the settling->delivered
                # flip — a voided/reaped agent order (which never reaches this branch)
                # is therefore never attested. Flag OFF => stays 'none', never queued.
                if config.ATTEST_ENABLED:
                    order.attest_status = "pending"
                webhooks.enqueue(session, store.merchant_id, "order.paid", order)
                webhooks.enqueue(session, store.merchant_id, "order.delivered", order)
                # M13 affiliate accrual: written ONLY here, on the settling->delivered
                # flip — a voided/reaped agent order (which never reaches this branch)
                # is therefore never accrued, the same guarantee as EAS queueing.
                affiliates.accrue(session, order, store)
            session.commit()


async def agent_guard_dispatch(request: Request, call_next):
    """Outermost middleware (registered AFTER the x402 middleware, so it runs
    first). For POST /s/{slug}/buy it 404/409s a dead store BEFORE any payable 402
    is emitted; on the way back it records the settle tx_hash for a served buy."""
    if request.method != "POST" or not _BUY_PATH_RE.match(request.url.path):
        return await call_next(request)
    slug = _slug_from_path(request.url.path)
    guard = await asyncio.to_thread(_guard_store_status, slug)
    if guard is not None:
        return JSONResponse({"detail": guard[1]}, status_code=guard[0])
    response = await call_next(request)
    if response.status_code == 402 and config.AGGR_DEFERRED_ENABLED:
        # CHALLENGE HONESTY: with the aggr flag on, the static accepts list carries
        # an aggr_deferred entry on EVERY store's 402. Strip it for a NON-batch
        # store so we never advertise a scheme the handler would 409. Batch stores
        # keep it. The accepts live in the base64 PAYMENT-REQUIRED header (the SDK
        # 402 JSON body is ``{}``), so filtering the header keeps body+header in
        # agreement. Flag off -> this branch never runs -> byte-identical 402.
        if not await asyncio.to_thread(_is_batch_store, slug):
            header = response.headers.get("PAYMENT-REQUIRED")
            if header:
                filtered = _filter_aggr_from_challenge(header)
                if filtered is not None:
                    response.headers["PAYMENT-REQUIRED"] = filtered
        return response
    if response.status_code == 200:
        pr = response.headers.get("PAYMENT-RESPONSE")
        order_id = getattr(request.state, "agent_order_id", None)
        if pr and order_id:
            scheme = _payment_scheme(request)
            await asyncio.to_thread(record_settlement, order_id, pr, scheme)
    return response


# ============================================================================
# MCP: hand-rolled stateless JSON-RPC 2.0 (Streamable HTTP, application/json)
# ============================================================================
_MCP_NO_CONTENT = object()


class _GetProductArgs(BaseModel):
    product_id: int


class _CreateCheckoutArgs(BaseModel):
    # Optional: which active product to check out. Omitted -> the store's primary
    # product (lowest Product.id), byte-identical to the pre-M10 single-product
    # behaviour. A store with several products (M10 add-product) can target any.
    product_id: int | None = None
    # M13 affiliate attribution (optional): the referring agent's payout wallet.
    ref: str | None = None

    @field_validator("ref")
    @classmethod
    def _v_ref(cls, v):
        try:
            return affiliates.normalize_ref(v)
        except affiliates.RefRejected as exc:
            raise ValueError(str(exc)) from exc


class _PayArgs(BaseModel):
    checkout_id: str = Field(min_length=1, max_length=64)
    tx_hash: str

    @field_validator("tx_hash")
    @classmethod
    def _v_tx(cls, v: str) -> str:
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", v or ""):
            raise ValueError("tx_hash must be a 0x-prefixed 32-byte hash")
        return v.lower()


class _ToolError(Exception):
    """A tool's business failure (not found, chain unavailable) — surfaced as an
    MCP tool result with isError, not a JSON-RPC protocol error."""


def _rpc_result(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _rpc_tool_error(req_id, message: str) -> dict:
    payload = {"error": message}
    return _rpc_result(
        req_id,
        {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": payload,
            "isError": True,
        },
    )


def _mcp_tools() -> list[dict]:
    return [
        {
            "name": "list_products",
            "description": "List the store's active products with price and network.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "get_product",
            "description": (
                "Get one product's detail, deliverable kind, and the x402 buy "
                "endpoint (x402-capable agents can POST straight to /s/{slug}/buy)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "create_checkout",
            "description": (
                "Create a unique-amount on-chain checkout (for agents that pay the "
                "merchant themselves and submit the tx hash via `pay`). Pass an "
                "optional product_id to check out a specific product; omit it for "
                "the store's primary product. Pass an optional `ref` (a 0x EVM "
                "address) to attribute the sale to a referring agent's payout wallet."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer"},
                    "ref": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "pay",
            "description": (
                "Submit the on-chain tx hash for a checkout; returns the order "
                "status (claim the goods by signing with the purchase wallet)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "checkout_id": {"type": "string"},
                    "tx_hash": {"type": "string"},
                },
                "required": ["checkout_id", "tx_hash"],
                "additionalProperties": False,
            },
        },
    ]


def _tool_list_products(session: Session, store: Store) -> dict:
    products = session.scalars(
        select(Product)
        .where(Product.store_id == store.id, Product.active.is_(True))
        .order_by(Product.id)
    ).all()
    return {
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "price": p.price_micro / 1e6,
                "price_micro": p.price_micro,
                "currency": CURRENCY,
                "network": NETWORK,
                "sla_minutes": _effective_sla(p),
            }
            for p in products
        ]
    }


def _tool_get_product(
    session: Session, store: Store, slug: str, product_id: int
) -> dict:
    product = session.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.store_id == store.id,
            Product.active.is_(True),
        )
    )
    if product is None:
        raise _ToolError("product not found")
    deliverable = session.scalar(
        select(Deliverable)
        .where(Deliverable.store_id == store.id, Deliverable.active.is_(True))
        .order_by(Deliverable.id.desc())
        .limit(1)
    )
    return {
        "id": product.id,
        "name": product.name,
        "description": _product_description(store),
        "price": product.price_micro / 1e6,
        "price_micro": product.price_micro,
        "currency": CURRENCY,
        "network": NETWORK,
        "sla_minutes": _effective_sla(product),
        "deliverable_kind": deliverable.kind if deliverable else "text",
        "pricing": _pricing_block(product),
        "x402": {
            "endpoint": f"/s/{slug}/buy/{product.id}",
            "network": NETWORK,
            "asset": ASSET,
            "schemes": enabled_schemes(product),
        },
    }


def _tool_create_checkout(
    session: Session,
    store: Store,
    product_id: int | None = None,
    referrer_addr: str | None = None,
) -> dict:
    if product_id is not None:
        product = session.scalar(
            select(Product).where(
                Product.id == product_id,
                Product.store_id == store.id,
                Product.active.is_(True),
            )
        )
        if product is None:
            raise _ToolError("product not found")
    else:
        product = _active_product(session, store.id)
        if product is None:
            raise _ToolError("store has no active product")
    try:
        order = checkout.create_order(session, store, product, referrer_addr)
    except checkout.AmountUnavailable as exc:
        raise _ToolError("checkout busy, retry") from exc
    log_event(session, "agentic", "order.created", store_id=store.id, order_id=order.id)
    session.commit()
    return {
        "checkout_id": order.id,
        "pay_to": store.pay_to,
        "amount": order.expected_micro / 1e6,
        "amount_micro": order.expected_micro,
        "expires_at": order.expires_at.isoformat() + "Z",
        "network": "X Layer (chainId 196)",
        "token": "USDT",
    }


def _tool_pay(session: Session, store: Store, args: _PayArgs) -> dict:
    order = session.get(Order, args.checkout_id)
    if order is None or order.store_id != store.id:
        raise _ToolError("checkout not found")
    try:
        checkout.verify_txhash(session, order, args.tx_hash)
    except checkout.TxAlreadyUsed as exc:
        raise _ToolError("tx already used") from exc
    except checkout.TxVerificationError as exc:
        raise _ToolError(str(exc)) from exc
    except (httpx.HTTPError, chain.ChainError) as exc:
        raise _ToolError("chain verification unavailable") from exc
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        session.expire(order)
    session.expire(order)
    # Reuse the exact unauth human-poll surface — the MCP never leaks more than it
    # (entitlement-backed secrets stay behind the sign-to-claim gate).
    from app.main import _order_response

    return _order_response(session, order, include_gated=False)


def _mcp_tools_call(session: Session, store: Store, slug: str, req_id, params) -> dict:
    if not isinstance(params, dict):
        return _rpc_error(req_id, -32602, "params must be an object")
    name = params.get("name")
    raw_args = params.get("arguments") or {}
    if not isinstance(raw_args, dict):
        return _rpc_error(req_id, -32602, "arguments must be an object")
    try:
        if name == "list_products":
            result = _tool_list_products(session, store)
        elif name == "get_product":
            args = _GetProductArgs.model_validate(raw_args)
            result = _tool_get_product(session, store, slug, args.product_id)
        elif name == "create_checkout":
            args = _CreateCheckoutArgs.model_validate(raw_args)
            result = _tool_create_checkout(session, store, args.product_id, args.ref)
        elif name == "pay":
            result = _tool_pay(session, store, _PayArgs.model_validate(raw_args))
        else:
            return _rpc_error(req_id, -32602, f"unknown tool: {name}")
    except ValidationError as exc:
        return _rpc_error(
            req_id, -32602, "invalid tool arguments", data=json.loads(exc.json())
        )
    except _ToolError as exc:
        return _rpc_tool_error(req_id, str(exc))
    return _rpc_result(
        req_id,
        {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "structuredContent": result,
        },
    )


def _handle_mcp(slug: str, payload):
    """Sync JSON-RPC dispatch (run off-loop). Raises HTTPException for the
    transport-level per-store 404/409; returns a JSON-RPC dict, or _MCP_NO_CONTENT
    for a notification (→ 202)."""
    with SessionLocal() as session:
        store = _require_live_store(session, slug)
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            req_id = payload.get("id") if isinstance(payload, dict) else None
            return _rpc_error(req_id, -32600, "invalid JSON-RPC request")
        method = payload.get("method")
        req_id = payload.get("id")
        params = payload.get("params") or {}
        if method == "notifications/initialized":
            return _MCP_NO_CONTENT
        if method == "initialize":
            proto = (
                params.get("protocolVersion") if isinstance(params, dict) else None
            ) or MCP_PROTOCOL_VERSION
            return _rpc_result(
                req_id,
                {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": f"tilla-{slug}", "version": TILLA_VERSION},
                },
            )
        if method == "ping":
            return _rpc_result(req_id, {})
        if method == "tools/list":
            return _rpc_result(req_id, {"tools": _mcp_tools()})
        if method == "tools/call":
            return _mcp_tools_call(session, store, slug, req_id, params)
        return _rpc_error(req_id, -32601, "method not found")


@router.post("/s/{slug}/mcp")
@limiter.limit("30/minute")
async def mcp_post(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            _rpc_error(None, -32700, "parse error"), headers=_AGENT_HEADERS
        )
    result = await asyncio.to_thread(_handle_mcp, slug, payload)
    if result is _MCP_NO_CONTENT:
        return Response(status_code=202)
    return JSONResponse(result, headers=_AGENT_HEADERS)


@router.get("/s/{slug}/mcp")
async def mcp_get(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    # Streamable HTTP GET (SSE) is not offered — JSON responses only.
    return JSONResponse(
        {"error": "method not allowed; use POST"},
        status_code=405,
        headers={"Allow": "POST", **_AGENT_HEADERS},
    )


# ============================================================================
# Root MCP: a Tilla-wide JSON-RPC server so any agent runtime is the concierge —
# browse/search across every live store and route to the right one. Buying then
# happens on the per-store endpoints each result advertises (its `mcp` / `buy`).
# NOTE (deploy): a new root path needs an nginx proxy location, like /ready —
# see deploy/nginx-m15.snippet. Until then it is reachable in-app / via tests.
# ============================================================================
class _BrowseArgs(BaseModel):
    sort: str = "sold"
    limit: int = 20
    offset: int = 0


class _SearchStoresArgs(BaseModel):
    query: str
    limit: int = 20


class _StoreSlugArgs(BaseModel):
    slug: str


def _root_mcp_tools() -> list[dict]:
    return [
        {
            "name": "browse_stores",
            "description": (
                "List live Tilla stores ranked by performance. Each result carries "
                "its page URL, per-store MCP + x402 buy endpoints, and reputation "
                "(sold_count, success_rate, unique_buyer_count, last_sale_at)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sort": {"type": "string", "enum": list(DISCOVERY_SORTS)},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "search_stores",
            "description": "Keyword-search live stores by slug or description.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_products",
            "description": (
                "List one store's active products by slug; then buy via that "
                "store's mcp/buy endpoint from browse_stores."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
                "additionalProperties": False,
            },
        },
    ]


def _root_mcp_tools_call(session: Session, req_id, params) -> dict:
    if not isinstance(params, dict):
        return _rpc_error(req_id, -32602, "params must be an object")
    name = params.get("name")
    raw_args = params.get("arguments") or {}
    if not isinstance(raw_args, dict):
        return _rpc_error(req_id, -32602, "arguments must be an object")
    try:
        if name == "browse_stores":
            a = _BrowseArgs.model_validate(raw_args)
            sort = a.sort if a.sort in DISCOVERY_SORTS else "sold"
            rows = _discovery_rows(
                session,
                [],
                max(1, min(a.limit, 50)),
                max(0, min(a.offset, 100_000)),
                sort,
            )
            result = {"resources": rows, "sort": sort}
        elif name == "search_stores":
            a = _SearchStoresArgs.model_validate(raw_args)
            q = a.query.strip()
            if len(q) < 2 or len(q) > 100:
                raise _ToolError("query must be 2 to 100 characters")
            result = {
                "resources": _search_discovery(session, q, max(1, min(a.limit, 50)))
            }
        elif name == "list_products":
            a = _StoreSlugArgs.model_validate(raw_args)
            store = session.scalar(select(Store).where(Store.slug == a.slug))
            if store is None or store.status != "live":
                raise _ToolError("store not found")
            result = _tool_list_products(session, store)
        else:
            return _rpc_error(req_id, -32602, f"unknown tool: {name}")
    except ValidationError as exc:
        return _rpc_error(
            req_id, -32602, "invalid tool arguments", data=json.loads(exc.json())
        )
    except _ToolError as exc:
        return _rpc_tool_error(req_id, str(exc))
    return _rpc_result(
        req_id,
        {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "structuredContent": result,
        },
    )


def _handle_root_mcp(payload):
    """Sync JSON-RPC dispatch for the Tilla-wide root MCP (no slug)."""
    with SessionLocal() as session:
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            req_id = payload.get("id") if isinstance(payload, dict) else None
            return _rpc_error(req_id, -32600, "invalid JSON-RPC request")
        method = payload.get("method")
        req_id = payload.get("id")
        params = payload.get("params") or {}
        if method == "notifications/initialized":
            return _MCP_NO_CONTENT
        if method == "initialize":
            proto = (
                params.get("protocolVersion") if isinstance(params, dict) else None
            ) or MCP_PROTOCOL_VERSION
            return _rpc_result(
                req_id,
                {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "tilla", "version": TILLA_VERSION},
                },
            )
        if method == "ping":
            return _rpc_result(req_id, {})
        if method == "tools/list":
            return _rpc_result(req_id, {"tools": _root_mcp_tools()})
        if method == "tools/call":
            return _root_mcp_tools_call(session, req_id, params)
        return _rpc_error(req_id, -32601, "method not found")


@router.post("/mcp")
@limiter.limit("30/minute")
async def root_mcp_post(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            _rpc_error(None, -32700, "parse error"), headers=_AGENT_HEADERS
        )
    result = await asyncio.to_thread(_handle_root_mcp, payload)
    if result is _MCP_NO_CONTENT:
        return Response(status_code=202)
    return JSONResponse(result, headers=_AGENT_HEADERS)


@router.get("/mcp")
async def root_mcp_get(request: Request):
    return JSONResponse(
        {"error": "method not allowed; use POST"},
        status_code=405,
        headers={"Allow": "POST", **_AGENT_HEADERS},
    )


# ============================================================================
# Feeds: feed.json (ACP product-feed shape) + llms.txt
# ============================================================================
def _effective_sla(product: Product) -> int:
    """The delivery-time promise (minutes) a buyer agent should expect: the
    merchant's per-product override, else the platform default. Always an int, so
    every purchasable action carries an ETA an agent can rank on."""
    return (
        product.sla_minutes if product.sla_minutes is not None else DELIVERY_SLA_MINUTES
    )


def _feed_product(store: Store, slug: str, product: Product, store_url: str) -> dict:
    return {
        "id": str(product.id),
        "title": product.name,
        "description": _product_description(store),
        "link": store_url,
        "price": {"amount": _usdt_str(product.price_micro), "currency": CURRENCY},
        "availability": "in_stock",
        "sla_minutes": _effective_sla(product),
        "pricing": _pricing_block(product),
        "x402": {
            "endpoint": f"/s/{slug}/buy/{product.id}",
            "network": NETWORK,
            "asset": ASSET,
            "schemes": enabled_schemes(product),
        },
    }


@router.get("/s/{slug}/feed.json")
@limiter.limit("60/minute")
def feed_json(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    store = _live_store(session, slug)
    if store is None:  # feeds 404 for ANY non-live store (never leak pending)
        raise HTTPException(404, "store not found")
    products = session.scalars(
        select(Product)
        .where(Product.store_id == store.id, Product.active.is_(True))
        .order_by(Product.id)
    ).all()
    store_url = f"{config.PUBLIC_BASE_URL.rstrip('/')}/s/{store.slug}/"
    body = {
        "store": {
            "slug": store.slug,
            "name": _store_name(store),
            "description": _store_description(store),
            "url": store_url,
        },
        "products": [_feed_product(store, slug, p, store_url) for p in products],
    }
    return JSONResponse(body, headers=_AGENT_HEADERS)


@router.get("/s/{slug}/llms.txt")
@limiter.limit("60/minute")
def llms_txt(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    store = _live_store(session, slug)
    if store is None:  # 404 for ANY non-live store (never leak pending)
        raise HTTPException(404, "store not found")
    products = session.scalars(
        select(Product)
        .where(Product.store_id == store.id, Product.active.is_(True))
        .order_by(Product.id)
    ).all()
    base = config.PUBLIC_BASE_URL.rstrip("/")
    lines = [f"# {_store_name(store)}", ""]
    desc = _store_description(store)
    if desc:
        lines += [desc, ""]
    lines.append("## Products")
    for p in products:
        lines.append(
            f"- {p.name} — {_usdt_str(p.price_micro)} {CURRENCY} [{_pricing_model(p)}]"
        )
    lines += [
        "",
        "## Machine endpoints",
        f"- Product feed (JSON): {base}/s/{slug}/feed.json",
        f"- MCP server (JSON-RPC): {base}/s/{slug}/mcp",
        f"- x402 buy: {base}/s/{slug}/buy",
        f"- Human storefront: {base}/s/{slug}/",
        f"- Network: {NETWORK} (X Layer); asset {ASSET} (USDT0)",
        "",
    ]
    return PlainTextResponse("\n".join(lines), headers=_AGENT_HEADERS)


@router.get("/.well-known/agent-card.json")
@limiter.limit("60/minute")
def agent_card(request: Request):
    base = config.PUBLIC_BASE_URL.rstrip("/")
    # Lazy import avoids the agentic<-main circular import at module load. Advertising
    # create-store's exact input contract lets an agent hiring Tilla self-serve without
    # trial and error (the ACP 'requirement schema' idea, applied to our own service).
    from app.main import CreateStoreBody

    body = {
        "name": "Tilla",
        "description": (
            "AI storefront builder with agent-native crypto checkout on X Layer. "
            "Agents can create stores (x402) and buy from any store (x402)."
        ),
        "url": base,
        "version": TILLA_VERSION,
        "skills": [
            {
                "id": "create-store",
                "name": "Create a storefront",
                "description": "Spin up a live one-product crypto store.",
                "x402": {"endpoint": "/create-store", "price": "1 USDT"},
                "input_schema": CreateStoreBody.model_json_schema(),
                "sample_request": {
                    "description": "single-origin coffee beans, roasted to order",
                    "theme": "original",
                },
                "sla_minutes": DELIVERY_SLA_MINUTES,
            },
            {
                "id": "buy",
                "name": "Buy from a store",
                "description": "Purchase a store's product; payTo is the merchant.",
                "x402": {"endpoint": "/s/{slug}/buy"},
                "input_schema": {
                    "type": "object",
                    "required": ["slug"],
                    "properties": {
                        "slug": {"type": "string", "description": "store slug"},
                        "product_id": {
                            "type": "integer",
                            "description": (
                                "product to buy; omit for the primary product"
                            ),
                        },
                    },
                },
                "sla_minutes": DELIVERY_SLA_MINUTES,
            },
        ],
        "payment": {
            "protocol": "x402-v2",
            "network": NETWORK,
            "asset": ASSET,
        },
        "registrations": [
            {"standard": "ERC-8004", "agentId": AGENT_ID, "chain": NETWORK}
        ],
        "discovery": {
            "resources": "/discovery/resources",
            "search": "/discovery/search",
            "mcp": "/mcp",
        },
    }
    return JSONResponse(body, headers=_AGENT_HEADERS)


# ============================================================================
# Discovery: Tilla-wide index of live stores (no merchant wallets leaked)
# ============================================================================
# Graduation-style trust ladder (Phase 1.5). Thresholds sit below Virtuals' 10-sale
# graduation, matched to Tilla's volume. It is computed ONLY from terminal,
# un-fakeable outcomes: delivered count = earned volume; success_rate (delivered vs
# refunded) = quality. A refund-heavy store is demoted to 'watch' regardless of
# volume — the auto-demote. Checkout expiries are deliberately NOT an input: an
# expired checkout is buyer abandonment, not store fault (the same reason they are
# excluded from success_rate), so they can never unfairly demote a store.
TRUST_TIERS = ("new", "watch", "emerging", "established", "trusted")


def _trust_tier(sold: int, success_rate: float | None) -> str:
    if sold == 0:
        return "new"
    # sold >= 1 => at least one delivery => success_rate is a real float here.
    if success_rate is not None and success_rate < 0.5:
        return "watch"
    if sold >= 8 and success_rate is not None and success_rate >= 0.95:
        return "trusted"
    if sold >= 3 and success_rate is not None and success_rate >= 0.9:
        return "established"
    return "emerging"


def _discovery_row(store: Store, pmin, pmax, sold, buyers, last_sale, refunded) -> dict:
    # Reputation signals an agent buyer can rank on, computed from terminal orders:
    # sold = delivered count; success_rate = delivered / (delivered + refunded), the
    # clean-delivery rate (None until a sale completes); unique_buyer_count = distinct
    # payer addresses; last_sale_at = most recent delivery; trust_tier = the graduation
    # ladder above. Abandoned/expired checkouts are the buyer's doing, not the store's,
    # so they are excluded from the rate.
    sold = sold or 0
    refunded = refunded or 0
    completed = sold + refunded
    success_rate = round(sold / completed, 4) if completed else None
    return {
        "slug": store.slug,
        "name": _store_name(store),
        "description": _store_description(store),
        "url": f"/s/{store.slug}/",
        "feed": f"/s/{store.slug}/feed.json",
        "llms_txt": f"/s/{store.slug}/llms.txt",
        "mcp": f"/s/{store.slug}/mcp",
        "buy": f"/s/{store.slug}/buy",
        "price_min_micro": pmin,
        "price_max_micro": pmax,
        "currency": CURRENCY,
        "network": NETWORK,
        "sold_count": sold,
        "success_rate": success_rate,
        "unique_buyer_count": buyers or 0,
        "last_sale_at": (last_sale.isoformat() + "Z") if last_sale else None,
        "trust_tier": _trust_tier(sold, success_rate),
        "created_at": store.created_at.isoformat() + "Z",
    }


# Discovery sort keys an agent buyer can pass. 'sold' is the default and preserves
# the prior order (most-sold first); an unknown value falls back to it.
DISCOVERY_SORTS = ("sold", "success", "buyers", "recent", "new")


def _discovery_rows(
    session: Session, where_clauses, limit: int, offset: int, sort: str = "sold"
):
    delivered = Order.status.in_(checkout.TERMINAL_DELIVERED)
    metrics_sq = (
        select(
            Order.store_id.label("sid"),
            func.count(case((delivered, Order.id))).label("sold"),
            func.count(func.distinct(case((delivered, Order.from_addr)))).label(
                "buyers"
            ),
            func.max(case((delivered, Order.paid_at))).label("last_sale"),
            func.count(case((Order.status == "refunded", Order.id))).label("refunded"),
        )
        .group_by(Order.store_id)
        .subquery()
    )
    price_sq = (
        select(
            Product.store_id.label("sid"),
            func.min(Product.price_micro).label("pmin"),
            func.max(Product.price_micro).label("pmax"),
        )
        .where(Product.active.is_(True))
        .group_by(Product.store_id)
        .subquery()
    )
    sold_c = func.coalesce(metrics_sq.c.sold, 0)
    completed_c = sold_c + func.coalesce(metrics_sq.c.refunded, 0)
    # Untried stores (no completed sale) sort last on 'success' via a -1 sentinel.
    success_c = case((completed_c > 0, sold_c * 1.0 / completed_c), else_=-1.0)
    sorts = {
        "sold": [sold_c.desc(), Store.created_at.desc(), Store.id.desc()],
        "success": [success_c.desc(), sold_c.desc(), Store.id.desc()],
        "buyers": [
            func.coalesce(metrics_sq.c.buyers, 0).desc(),
            sold_c.desc(),
            Store.id.desc(),
        ],
        "recent": [metrics_sq.c.last_sale.desc(), Store.id.desc()],
        "new": [Store.created_at.desc(), Store.id.desc()],
    }
    order_by = sorts.get(sort) or sorts["sold"]
    stmt = (
        select(
            Store,
            price_sq.c.pmin,
            price_sq.c.pmax,
            metrics_sq.c.sold,
            metrics_sq.c.buyers,
            metrics_sq.c.last_sale,
            metrics_sq.c.refunded,
        )
        .outerjoin(price_sq, price_sq.c.sid == Store.id)
        .outerjoin(metrics_sq, metrics_sq.c.sid == Store.id)
        .where(Store.status == "live", *where_clauses)
        .order_by(*order_by)
        .limit(limit)
        .offset(offset)
    )
    return [
        _discovery_row(s, pmin, pmax, sold, buyers, last_sale, refunded)
        for s, pmin, pmax, sold, buyers, last_sale, refunded in session.execute(
            stmt
        ).all()
    ]


@router.get("/discovery/resources")
@limiter.limit("30/minute")
def discovery_resources(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    sort: str = "sold",
    session: Session = Depends(get_session),
):
    limit = max(1, min(limit, 50))
    offset = max(0, min(offset, 100_000))
    sort = sort if sort in DISCOVERY_SORTS else "sold"
    total = session.scalar(
        select(func.count()).select_from(Store).where(Store.status == "live")
    )
    resources = _discovery_rows(session, [], limit, offset, sort)
    return JSONResponse(
        {
            "service": SERVICE,
            "agent_card": "/.well-known/agent-card.json",
            "total": total,
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "sorts": list(DISCOVERY_SORTS),
            "resources": resources,
        },
        headers=_AGENT_HEADERS,
    )


def _search_discovery(session: Session, q: str, limit: int) -> list[dict]:
    """Keyword search over live stores (slug + merchant description). LIKE
    metacharacters are escaped so a '%'/'_' in the query is a literal, not a
    wildcard. Shared by /discovery/search and the root MCP search_stores tool."""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{esc}%"
    clause = or_(
        Store.slug.like(like, escape="\\"),
        Store.description.like(like, escape="\\"),
    )
    return _discovery_rows(session, [clause], limit, 0)


@router.get("/discovery/search")
@limiter.limit("30/minute")
def discovery_search(
    request: Request,
    q: str = "",
    limit: int = 20,
    session: Session = Depends(get_session),
):
    q = q.strip()
    if len(q) < 2 or len(q) > 100:
        raise HTTPException(422, "q must be 2 to 100 characters")
    resources = _search_discovery(session, q, max(1, min(limit, 50)))
    return JSONResponse(
        {
            "service": SERVICE,
            "agent_card": "/.well-known/agent-card.json",
            "query": q,
            "count": len(resources),
            "resources": resources,
        },
        headers=_AGENT_HEADERS,
    )
