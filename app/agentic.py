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
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, PlainTextResponse, Response
from x402.http.types import HTTPResponseBody
from x402.http.utils import (
    decode_payment_response_header,
    decode_payment_signature_header,
)
from x402.schemas import AssetAmount

from app import chain, checkout, config, delivery
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
)

logger = logging.getLogger("tilla")

router = APIRouter()

CURRENCY = "USDT"
NETWORK = PAYMENT_NETWORK  # "eip155:196"
ASSET = PAYMENT_ASSET  # USDT0 on X Layer
SERVICE = "tilla"
AGENT_ID = 6961  # Tilla's ERC-8004 agent id (OKX ASP #6961)
TILLA_VERSION = "0.1.0"
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
_BUY_PATH_RE = re.compile(r"^/s/([a-z0-9][a-z0-9-]{0,39})/buy/?$")

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
            product = _active_product(session, store.id)
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
    session: Session, store: Store, product: Product, payer: str, nonce: str
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


@router.post("/s/{slug}/buy")
@limiter.limit("60/minute")
def agent_buy(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    # FAIL CLOSED: no verified payment (middleware absent — OKX_API_KEY unset — or
    # payment not provided) → 402. Goods are never served free.
    payload = getattr(request.state, "payment_payload", None)
    if payload is None:
        raise HTTPException(402, "payment required")
    # RACE re-check: if the store flipped non-live between challenge and retry (or
    # a sentinel challenge got paid), return >=400 so the middleware SKIPS
    # settlement — the signed authorization is never executed, zero funds move.
    store = _require_live_store(session, slug)
    product = _active_product(session, store.id)
    if product is None:
        raise HTTPException(409, "store has no active product")
    auth = (
        payload.payload.get("authorization")
        if isinstance(payload.payload, dict)
        else None
    )
    if not isinstance(auth, dict) or not auth.get("nonce"):
        raise HTTPException(400, "missing authorization nonce")
    order, body = fulfill_agent_order(
        session, store, product, auth.get("from") or "", auth["nonce"]
    )
    session.commit()
    # Shared scope["state"] hands the order id to the outer agent-guard middleware
    # for settle-success tx_hash bookkeeping.
    request.state.agent_order_id = order.id
    return JSONResponse(body)


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


def record_settlement(order_id: str, payment_response_header: str) -> None:
    """A served 200 with a PAYMENT-RESPONSE header IS the settle-success signal, so
    flip the provisional 'settling' order to 'delivered' — exposing the goods to
    the out-of-band claim paths — and record the settle tx_hash when the header
    decodes. The flip does NOT depend on the header decoding: an undecodable/empty
    transaction still delivers (logged for reconciliation) instead of leaving a
    genuinely-paid order in 'settling' for the reaper to void. Idempotent (the
    conditional transition is a no-op once the order has left 'settling')."""
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
    if response.status_code == 200:
        pr = response.headers.get("PAYMENT-RESPONSE")
        order_id = getattr(request.state, "agent_order_id", None)
        if pr and order_id:
            await asyncio.to_thread(record_settlement, order_id, pr)
    return response


# ============================================================================
# MCP: hand-rolled stateless JSON-RPC 2.0 (Streamable HTTP, application/json)
# ============================================================================
_MCP_NO_CONTENT = object()


class _GetProductArgs(BaseModel):
    product_id: int


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
                "merchant themselves and submit the tx hash via `pay`)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
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
        "deliverable_kind": deliverable.kind if deliverable else "text",
        "pricing": _pricing_block(product),
        "x402": {
            "endpoint": f"/s/{slug}/buy",
            "network": NETWORK,
            "asset": ASSET,
            "schemes": enabled_schemes(product),
        },
    }


def _tool_create_checkout(session: Session, store: Store) -> dict:
    product = _active_product(session, store.id)
    if product is None:
        raise _ToolError("store has no active product")
    try:
        order = checkout.create_order(session, store, product)
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
            result = _tool_create_checkout(session, store)
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
# Feeds: feed.json (ACP product-feed shape) + llms.txt
# ============================================================================
def _feed_product(store: Store, slug: str, product: Product, store_url: str) -> dict:
    return {
        "id": str(product.id),
        "title": product.name,
        "description": _product_description(store),
        "link": store_url,
        "price": {"amount": _usdt_str(product.price_micro), "currency": CURRENCY},
        "availability": "in_stock",
        "pricing": _pricing_block(product),
        "x402": {
            "endpoint": f"/s/{slug}/buy",
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
            },
            {
                "id": "buy",
                "name": "Buy from a store",
                "description": "Purchase a store's product; payTo is the merchant.",
                "x402": {"endpoint": "/s/{slug}/buy"},
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
        },
    }
    return JSONResponse(body, headers=_AGENT_HEADERS)


# ============================================================================
# Discovery: Tilla-wide index of live stores (no merchant wallets leaked)
# ============================================================================
def _discovery_row(store: Store, pmin, pmax, sold) -> dict:
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
        "sold_count": sold or 0,
        "created_at": store.created_at.isoformat() + "Z",
    }


def _discovery_rows(session: Session, where_clauses, limit: int, offset: int):
    sold_sq = (
        select(
            Order.store_id.label("sid"),
            func.count(Order.id).label("sold"),
        )
        .where(Order.status.in_(checkout.TERMINAL_DELIVERED))
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
    stmt = (
        select(Store, price_sq.c.pmin, price_sq.c.pmax, sold_sq.c.sold)
        .outerjoin(price_sq, price_sq.c.sid == Store.id)
        .outerjoin(sold_sq, sold_sq.c.sid == Store.id)
        .where(Store.status == "live", *where_clauses)
        .order_by(
            func.coalesce(sold_sq.c.sold, 0).desc(),
            Store.created_at.desc(),
            Store.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return [
        _discovery_row(s, pmin, pmax, sold)
        for s, pmin, pmax, sold in session.execute(stmt).all()
    ]


@router.get("/discovery/resources")
@limiter.limit("30/minute")
def discovery_resources(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    session: Session = Depends(get_session),
):
    limit = max(1, min(limit, 50))
    offset = max(0, min(offset, 100_000))
    total = session.scalar(
        select(func.count()).select_from(Store).where(Store.status == "live")
    )
    resources = _discovery_rows(session, [], limit, offset)
    return JSONResponse(
        {
            "service": SERVICE,
            "agent_card": "/.well-known/agent-card.json",
            "total": total,
            "limit": limit,
            "offset": offset,
            "resources": resources,
        },
        headers=_AGENT_HEADERS,
    )


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
    limit = max(1, min(limit, 50))
    # Escape LIKE metacharacters so a '%'/'_' in the query is a literal, not a
    # wildcard (ESCAPE clause), then match slug or merchant description.
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{esc}%"
    clause = or_(
        Store.slug.like(like, escape="\\"),
        Store.description.like(like, escape="\\"),
    )
    resources = _discovery_rows(session, [clause], limit, 0)
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
