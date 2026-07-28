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
import os
import re
import uuid
from datetime import timedelta
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
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

from app import (
    affiliates,
    b2b,
    chain,
    checkout,
    config,
    delivery,
    federation,
    imagery,
    webhooks,
)
from app.db import SessionLocal, get_session
from app.limiter import limiter
from app.models import (
    Deliverable,
    Delivery,
    Entitlement,
    Order,
    Product,
    Review,
    Store,
    log_event,
)
from app.payment import (
    PAYMENT_AMOUNT,
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
# Phase 1.6: a delivered sale is provisional for this window — the buyer may confirm
# or dispute it; if they do neither, it auto-confirms (ACP 'skip' mode) and counts
# toward success_rate. Short, because digital delivery is validated fast.
EVAL_WINDOW_DAYS = 3
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
# The aggr_deferred rail needs its own, far longer deadline. A live OKX deferred settle
# returns NO tx ref, so settlement only becomes observable when the facilitator relayer's
# transfer lands on chain — 15 min was well inside that latency and voided six
# facilitator-accepted orders in prod on 2026-07-23. A deferred order is therefore held
# for hours AND may only be voided on the back of a chain scan that actually completed
# (see ``_reap_tick`` / ``reconcile.LAST_CHAIN_SCAN_MONO``), never on a blind timer.
DEFERRED_REAP_AFTER = timedelta(hours=6)
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


def _deferred_deliverable_ok(session: Session, store_id: int) -> bool:
    """True iff this store's active deliverable is a server-gated FILE — the one
    shape the deferred rail may be offered on.

    aggr_deferred delivers on a facilitator-VERIFIED authorization and settles
    on-chain later, so every claimable good has to stay revocable until the settle
    lands: a file download is gated on TERMINAL_DELIVERED and the void path revokes
    the entitlement. A ``text`` deliverable — and an ABSENT one, which falls back to
    the ``store.delivery`` text — rides in the immediate response body, and knowledge
    cannot be taken back. ``POST /api/stores/{slug}/pricing`` refuses that
    configuration at write time (``app/main.py``); this is the same invariant at
    SERVE time, so a row that predates the guard, or one written around it, still
    cannot advertise or take payment on the rail.
    """
    active = session.scalar(
        select(Deliverable)
        .where(Deliverable.store_id == store_id, Deliverable.active.is_(True))
        .order_by(Deliverable.id.desc())
        .limit(1)
    )
    return active is not None and active.kind == "file"


def _is_batch_path(path: str) -> bool:
    """True iff the product THIS /buy path targets declares pricing_model='batch'
    AND the store's deliverable is revocable (see :func:`_deferred_deliverable_ok`).
    The agent-guard uses it to decide whether the aggr_deferred accepts-entry stays
    in the 402 challenge. It keys on the path, not the store, because a multi-product
    store can mix rails: keying on the store's primary product advertised aggr on a
    one_time /buy/<id> (and stripped it from a batch one) whenever the two disagreed.
    Any unknown/non-live store, unknown product, or DB error returns False, so the
    aggr entry is stripped (fail-safe: never over-advertise a rail)."""
    slug = _slug_from_path(path)
    if slug is None:
        return False
    try:
        with SessionLocal() as session:
            store = _live_store(session, slug)
            if store is None:
                return False
            product = _product_for_path(session, store, path)
            return (
                product is not None
                and (product.pricing_model or "one_time") == "batch"
                and _deferred_deliverable_ok(session, store.id)
            )
    except Exception:
        logger.exception("aggr batch-check failed for %s", path)
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
    a batch product, only when TILLA_AGGR_DEFERRED is on, and only when the store's
    deliverable is revocable (:func:`_deferred_deliverable_ok`) — so feeds/MCP never
    advertise a rail the 402 challenge and the pay-time gate would then refuse.
    Metered (MPP) and subscription (period) are separate endpoints, not x402 buy
    schemes, so they do not appear here.

    The deliverable lookup opens its own short read session — the same shape
    :func:`_is_batch_path` uses — because the feed/MCP item builders are pure
    (store, product) functions with no session to thread. It runs only for a batch
    product, and any DB error strips the entry (fail-safe, never over-advertise)."""
    schemes = ["exact"]
    if _pricing_model(product) == "batch" and config.AGGR_DEFERRED_ENABLED:
        try:
            with SessionLocal() as session:
                if _deferred_deliverable_ok(session, product.store_id):
                    schemes.append("aggr_deferred")
        except Exception:
            logger.exception("aggr deliverable-check failed for product %s", product.id)
    return schemes


def _tier_quote(product: Product, agent_id: int) -> dict | None:
    """The advisory wholesale quote fields for ``agent_id`` on ``product``, or None
    when no tier matches or the agent owner is not verifiable (fail-to-base). The
    tier price is disclosed ONLY per-request against a presented id (never in a
    public feed); INV-1 still governs the actual grant at settle."""
    tier_price = b2b.match_tier(product, agent_id)
    if tier_price is None:
        return None
    owner = b2b.verify_agent_owner(agent_id)
    if owner is None:
        return None
    return {
        "tier_price_micro": tier_price,
        "tier_price": _usdt_str(tier_price),
        "owner": owner,
        "requires": (
            "settle payer wallet must equal the agent owner wallet; "
            "otherwise the base price applies"
        ),
    }


def _pricing_block(product: Product) -> dict:
    raw = product.pricing_params if isinstance(product.pricing_params, dict) else {}
    # M16: wholesale tier tables NEVER leak on a public surface (feed/MCP) — only an
    # opt-in ``wholesale: true`` flag. Per-buyer prices come from /quote, per-request.
    params = {k: v for k, v in raw.items() if k != "tiers"}
    block: dict = {"model": _pricing_model(product), "params": params}
    if raw.get("tiers"):
        block["wholesale"] = True
    return block


# ============================================================================
# Offering envelope: the two agent-buyer fields every purchasable offering carries.
# `requiredFunds` = the exact x402 charge; `nextActions` = how to transact. Additive
# (existing consumers ignore both keys); shared verbatim by feed.json, get_product,
# and the agent card so an agent buyer sees one consistent shape everywhere.
# ============================================================================
def _required_funds(store: Store, product: Product) -> dict:
    """The exact funds an agent must transfer to buy ``product`` — byte-identical to
    what the x402 challenge on ``/s/{slug}/buy/{id}`` actually demands. The amount is
    the base ``product.price_micro`` that :func:`resolve_price` returns for a public
    request (per-buyer wholesale tiers are NEVER quoted on a public surface — M16
    INV-B — so the advertised amount is always the one that settles); asset/network
    are the canonical X Layer USDT0 rail; ``pay_to`` is the merchant wallet
    :func:`resolve_pay_to` returns. NON-CUSTODIAL: pay_to is the merchant, never
    Tilla."""
    return {
        "amount_micro": product.price_micro,
        "amount": _usdt_str(product.price_micro),
        "currency": CURRENCY,
        "asset": ASSET,
        "network": NETWORK,
        "pay_to": store.pay_to,
    }


def _next_actions(slug: str, product: Product) -> list[dict]:
    """The concrete steps an agent can take to acquire ``product``, most-direct
    first: the x402 buy POST (pay + receive the deliverable in one call) and the MCP
    confirm-before-pay path. Each names a real, reachable endpoint/tool on THIS
    store."""
    return [
        {
            "type": "x402_buy",
            "method": "POST",
            "endpoint": f"/s/{slug}/buy/{product.id}",
            "protocol": "x402-v2",
            "description": (
                "POST an x402 payment to buy and receive the deliverable in-band."
            ),
        },
        {
            "type": "mcp",
            "endpoint": f"/s/{slug}/mcp",
            "tools": ["preview_order", "create_checkout", "pay"],
            "description": (
                "Confirm-before-pay: preview_order (read-only), then create_checkout "
                "(reserve an exact amount), then pay (submit the tx hash)."
            ),
        },
    ]


def _offering_envelope(store: Store, slug: str, product: Product) -> dict:
    """The offering envelope every agent-facing surface embeds per purchasable
    product: ``requiredFunds`` (the exact x402 charge, non-custodial payTo=merchant)
    and ``nextActions`` (how to transact)."""
    return {
        "requiredFunds": _required_funds(store, product),
        "nextActions": _next_actions(slug, product),
    }


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


def payer_from_payment_header(header: str | None) -> str | None:
    """The EIP-3009 ``from`` (payer) wallet carried in a PAYMENT-SIGNATURE /
    X-PAYMENT header, lowercased. None when the header is absent or undecodable —
    the price hook then resolves the base price (no verifiable payer ⇒ no tier)."""
    if not header:
        return None
    try:
        payload = decode_payment_signature_header(header)
        auth = (
            payload.payload.get("authorization")
            if isinstance(payload.payload, dict)
            else None
        )
        frm = auth.get("from") if isinstance(auth, dict) else None
        return frm.lower() if isinstance(frm, str) and frm else None
    except Exception:
        return None


def resolve_price(
    path: str, agent_id: str | None = None, payer: str | None = None
) -> AssetAmount:
    """Exact product price as an AssetAmount for the store in ``path``. Any
    unknown/non-live store, missing product, or DB error returns the sentinel
    (amount '1') rather than raising.

    M16 B2B: when the request presents an ``agent_id`` (query param) AND a payer
    wallet is recoverable from the payment header, the price is the wholesale
    TIER price — but only when :func:`b2b.effective_price_micro` verifies the
    payer is the on-chain owner of that agent id (INV-1). Absent either, or on any
    mismatch/RPC outage, this is byte-identical to the base-price path (fail-to-
    base). The settle seam (:func:`record_settlement`) re-derives the same gate
    with a fresh ownership read, so a tier never rides a stale positive cache."""
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
            price_micro = product.price_micro
            aid = b2b.parse_agent_id(agent_id) if agent_id else None
            if aid is not None and payer:
                price_micro, _ = b2b.effective_price_micro(product, aid, payer)
            return AssetAmount(
                amount=str(price_micro),
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
    agent_id: int | None = None,
    signed_micro: int | None = None,
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
        # Nonce-scoped idempotency: a replay (even carrying a DIFFERENT agent_id)
        # returns the stored order at its ORIGINAL price — the tier is never
        # re-derived on a replayed authorization, so a replayed nonce cannot be
        # re-quoted into a discount.
        _assert_nonce_owner(existing, store, payer)
        return existing, _agent_buy_body(session, existing, store, product)
    # M16 B2B: the wholesale tier is granted only when the payer is the verified
    # on-chain owner of the presented agent id (INV-1, capped <= base at write).
    # Every other case returns product.price_micro — the order is born at the
    # exact amount the 402 challenge demanded and that settles.
    #
    # Prefer the SIGNED authorization value (the amount the middleware matched to a
    # verified requirement and actually settled) so the DB record can never drift
    # from reality if the ownership cache expires between the price hook and here
    # and an RPC read flips. Fall back to re-deriving only when no signed value is
    # threaded through (internal callers / tests).
    if signed_micro is not None and signed_micro > 0:
        price_micro = signed_micro
    else:
        price_micro, _ = b2b.effective_price_micro(product, agent_id, payer)
    order = Order(
        id=uuid.uuid4().hex[:16],
        store_id=store.id,
        product_id=product.id,
        pay_to=store.pay_to,
        amount_micro=price_micro,
        expected_micro=price_micro,
        status="confirmed",
        channel="agent",
        x402_nonce=nonce,
        from_addr=checkout.norm_addr(payer),
        referrer_addr=referrer_addr,
        created_block=checkout._current_head(),
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
    request: Request,
    session: Session,
    slug: str,
    ref: str | None,
    agent_id: str | None,
) -> JSONResponse:
    """Shared body for POST /s/{slug}/buy and /s/{slug}/buy/{product_id}. The
    product is resolved from the request PATH (bare /buy = primary; /buy/{id} =
    that product), the SAME resolution ``resolve_price`` used to build the 402
    challenge — so the price the agent paid always equals what is charged here.
    ``agent_id`` (M16 B2B) is the optional caller-presented agent identity, priced
    off the SETTLED payer, never a client-asserted field."""
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
    # Pay-time HARD GATE: an aggr_deferred payment against a non-batch product, or
    # against one whose deliverable is not revocable, is refused BEFORE settlement
    # (same funds-safe >=400-skips-settle pattern). The deliverable half is the
    # last of the three layers — challenge, feed, here — so even a challenge that
    # was minted before a deliverable changed cannot be paid against.
    if _payment_scheme(request) == PAYMENT_SCHEME_AGGR_DEFERRED:
        if (product.pricing_model or "one_time") != "batch":
            raise HTTPException(
                409, "aggr_deferred is only available for batch products"
            )
        if not _deferred_deliverable_ok(session, store.id):
            raise HTTPException(
                409,
                "aggr_deferred requires a file deliverable: text is released in the "
                "response body, before a deferred settle confirms on-chain",
            )
    auth = (
        payload.payload.get("authorization")
        if isinstance(payload.payload, dict)
        else None
    )
    if not isinstance(auth, dict) or not auth.get("nonce"):
        raise HTTPException(400, "missing authorization nonce")
    # M16 B2B: the tier is priced off the SETTLED payer (the EIP-3009 `from`), the
    # same wallet the challenge's price hook verified — never a client-asserted
    # field. A malformed agent_id parses to None → base price, no discount.
    aid = b2b.parse_agent_id(agent_id) if agent_id else None
    # The signed EIP-3009 `value` is the amount the middleware verified + settled;
    # record the order at exactly that so the DB never drifts from what was paid.
    try:
        signed_micro = int(auth.get("value"))
    except (TypeError, ValueError):
        signed_micro = None
    order, body = fulfill_agent_order(
        session,
        store,
        product,
        auth.get("from") or "",
        auth["nonce"],
        referrer_addr,
        aid,
        signed_micro,
    )
    session.commit()
    # Shared scope["state"] hands the order id (and the presented agent id) to the
    # outer agent-guard middleware for settle-success bookkeeping + tier re-verify.
    request.state.agent_order_id = order.id
    request.state.agent_buy_agent_id = aid
    return JSONResponse(body)


@router.post("/s/{slug}/buy")
@limiter.limit("60/minute")
def agent_buy(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
    ref: str | None = None,
    agent_id: str | None = None,
):
    return _do_agent_buy(request, session, slug, ref, agent_id)


@router.post("/s/{slug}/buy/{product_id}")
@limiter.limit("60/minute")
def agent_buy_product(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    product_id: int = Path(..., ge=1),
    session: Session = Depends(get_session),
    ref: str | None = None,
    agent_id: str | None = None,
):
    return _do_agent_buy(request, session, slug, ref, agent_id)


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
def _void_settling(session: Session, order: Order, event: str) -> bool:
    """Conditionally void a provisional 'settling' agent order (→ canceled), revoking
    its entitlement (killing the minted download token / license key) and enqueuing an
    ``order.voided`` webhook. The winning ``settling -> canceled`` transition is a
    conditional UPDATE, so a settled order (already 'delivered') and a concurrent
    voider are both no-ops. Does NOT commit — the caller owns the transaction.
    Returns True on the winning void."""
    if not checkout.transition(session, order.id, ("settling",), "canceled"):
        return False
    delivery.revoke_entitlement(session, order)
    log_event(
        session,
        "agentic",
        event,
        store_id=order.store_id,
        order_id=order.id,
    )
    store = session.get(Store, order.store_id)
    if store is not None:
        session.refresh(order)
        webhooks.enqueue(session, store.merchant_id, "order.voided", order)
    return True


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
        if _void_settling(session, order, "agent_order.settle_failed"):
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


def unpaid_schema_body(schema: dict, summary: str, sample: dict | None = None):
    """Build an x402 ``unpaid_response_body`` hook publishing a route's request
    schema in its 402 body.

    The agent card carries the same schemas, but a buyer following the plain x402
    flow (POST -> 402 -> pay -> replay) never fetches the card: it saw an empty
    ``{}`` and had to guess the parameters before paying. The SDK invokes this
    hook ONLY when no payment was supplied, so the schema rides the unpaid
    challenge alone and is never echoed into a payment payload. Callers pass the
    same request models the handlers validate against, so a published schema
    cannot drift from what the endpoint actually accepts."""
    body: dict = {
        "error": "payment_required",
        "summary": summary,
        "input_schema": schema,
    }
    if sample is not None:
        body["sample_request"] = sample

    def _hook(context) -> HTTPResponseBody:
        return HTTPResponseBody(content_type="application/json", body=body)

    return _hook


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


def reap_agent_orders(
    session: Session,
    now=None,
    *,
    chain_scanned: bool = False,
    scanned_ids: set[str] | None = None,
) -> int:
    """Void channel='agent' orders stuck in the provisional 'settling' status older
    than the applicable deadline (a crash or lost settle between the deliver-commit and
    the settle-confirm). A settled order (flipped to 'delivered' by record_settlement,
    even when its PAYMENT-RESPONSE header was undecodable and tx_hash is NULL) and
    every human order are never touched — the reaper keys on 'settling', never on
    tx_hash, so a genuinely-paid order can never be clawed back.

    An unsettled order is indistinguishable from a paid one still waiting on the
    relayer (the facilitator can return no tx ref at serve time, and since the
    unparsed-header fallback was removed ANY rail can leave a paid order provisional),
    so voiding requires ``chain_scanned`` — a completed reconcile chain scan that found
    no transfer. An eth_getLogs blackout must never read as 'never paid'."""
    if not chain_scanned:
        return 0
    now = now or checkout._now()
    cutoff = now - (DEFERRED_REAP_AFTER if config.AGGR_DEFERRED_ENABLED else REAP_AFTER)
    reaped = 0
    # aggr_deferred orders (settle_ref set) are EXEMPT: their aggregated tx can
    # confirm after the reap window and the M8 reconciliation poller owns their
    # lifecycle — reaping them could void a genuinely-paid-but-slow order.
    orders = session.scalars(
        select(Order).where(
            Order.channel == "agent",
            Order.status == "settling",
            Order.settle_ref.is_(None),
        )
    ).all()
    for o in orders:
        if scanned_ids is not None and o.id not in scanned_ids:
            # This order's pair was never walked to head this tick — the scan is capped
            # at RECONCILE_MAX_PER_TICK candidates and filters on from_addr/network, so
            # a completed scan of OTHER pairs is not evidence that THIS order is unpaid.
            continue
        reached = checkout._naive(o.paid_at) or checkout._naive(o.created_at)
        if reached is not None and reached <= cutoff:
            if _void_settling(session, o, "agent_order.reaped"):
                reaped += 1
    return reaped


def _reap_tick() -> None:
    # A settle can carry NO tx hash at serve time (the live OKX aggr_deferred case, and
    # any rail whose PAYMENT-RESPONSE header does not decode), so those orders sit
    # 'settling' with settle_ref NULL until the chain reconciler finds the transfer —
    # the settle_ref exemption in reap_agent_orders does NOT protect them. The reaper
    # needs no RPC but the reconciler does, so a silently-failing RPC would make every
    # slow-but-paid order look unpaid. Fail-closed on every rail: skip entirely if the
    # chain is unreachable, and only let the reaper void when this tick's chain scan
    # actually COMPLETED (LAST_CHAIN_SCAN_MONO advanced).
    from app import reconcile

    if not reconcile.chain_reachable():
        return
    before = reconcile.LAST_CHAIN_SCAN_MONO
    reconcile.reconcile_chain_tick()
    chain_scanned = reconcile.LAST_CHAIN_SCAN_MONO > before
    # Void ONLY the orders whose own pair walked to head this tick.
    scanned_ids = set(reconcile.LAST_SCANNED_ORDER_IDS)
    with SessionLocal() as session:
        if reap_agent_orders(
            session, chain_scanned=chain_scanned, scanned_ids=scanned_ids
        ):
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


def _settle_tier_data(session: Session, order: Order, agent_id: int | None) -> dict:
    """Re-derive the wholesale tier at the SETTLE seam with a FRESH ownership read
    (never the positive cache — INV-1 defense-in-depth). Returns the log payload
    recording whether the tier is certified for this settled payer. When the order
    settled BELOW base but a fresh read does NOT confirm the payer owns the agent
    (e.g. a stale-cache exploit or an ownership transfer inside the 300s window),
    the settlement is flagged: the funds already moved, so the honest record is the
    price actually paid with ``tier_applied=False`` — never a certified discount."""
    base = None
    tier_applied = False
    if agent_id is not None and order.product_id is not None:
        product = session.get(Product, order.product_id)
        if product is not None:
            base = product.price_micro
            _, tier_applied = b2b.effective_price_micro(
                product, agent_id, order.from_addr, fresh=True
            )
    data: dict = {"agent_id": agent_id, "tier_applied": tier_applied}
    if base is not None and order.expected_micro < base and not tier_applied:
        data["tier_discrepancy"] = True
        data["base_micro"] = base
        data["paid_micro"] = order.expected_micro
    return data


def _finalize_settled(
    session: Session, order: Order, tx: str | None, settle_data: dict | None
) -> bool:
    """Flip a provisional 'settling' agent order to terminal 'delivered' on a confirmed
    settle — exposing the goods to the out-of-band claim paths — recording the settle
    ``tx_hash`` when present, then firing the paid/delivered webhooks (suppressed in
    ``checkout.deliver`` for agent orders), queueing the M11 EAS attestation, and
    accruing the M13 affiliate rev-share. Every downstream effect is written ONLY on the
    winning ``settling -> delivered`` transition, so a voided/reaped order never reaches
    them. Idempotent: the conditional UPDATE is a no-op once the order has left
    'settling'. Does NOT commit — the caller owns the transaction. Returns True on the
    winning flip."""
    # paid_micro is what refunds._amount_due subtracts from, and only the checkout
    # sweeper used to write it — so an agent order that was delivered and genuinely
    # paid computed due=0 and 409'd "order has nothing left to refund", making every
    # agent sale permanently unrefundable. A settled order paid exactly expected_micro.
    fields = {"paid_micro": order.expected_micro}
    if tx:
        fields["tx_hash"] = tx
    if not checkout.transition(session, order.id, ("settling",), "delivered", **fields):
        return False
    log_event(
        session,
        "agentic",
        "agent_order.settled",
        store_id=order.store_id,
        order_id=order.id,
        data=settle_data,
    )
    store = session.get(Store, order.store_id)
    if store is not None:
        session.refresh(order)
        if config.ATTEST_ENABLED:
            order.attest_status = "pending"
        webhooks.enqueue(session, store.merchant_id, "order.paid", order)
        webhooks.enqueue(session, store.merchant_id, "order.delivered", order)
        affiliates.accrue(session, order, store)
    return True


def record_settlement(
    order_id: str,
    payment_response_header: str,
    scheme: str | None = None,
    agent_id: int | None = None,
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
    status = None
    try:
        settle = decode_payment_response_header(payment_response_header)
        tx = settle.transaction or None
        status = settle.status
    except Exception:
        logger.exception("record_settlement: undecodable PAYMENT-RESPONSE")
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        if order is None or order.channel != "agent":
            return
        # NO decoded tx (any rail) or a 'pending' aggregated tx means settlement is
        # UNCONFIRMED at serve time, so we do NOT flip to a terminal 'delivered' or log
        # 'agent_order.settled' (which would claim settlement with no evidence). The
        # order stays provisional 'settling' with the 'agent_order.settle_pending'
        # marker; when a pending aggregated ref IS present we persist it so the
        # reconciliation poller (app.reconcile) can later finalize on its confirmed tx.
        #
        # The exact rail USED to deliver on an undecodable header, on the reasoning
        # that a served 200 proves payment and holding risked the reaper voiding a
        # paid order. Production disproved the premise: OKX's listing validators drove
        # three served 200s whose headers carried no tx, and a 900-block scan around
        # them found NO transfer to any of the three merchants — goods delivered, zero
        # settled. Holding is now the safe side: reconcile_chain_tick scans EVERY
        # settling agent order (not just deferred), and the reaper cannot void one
        # without a completed chain scan, so a genuinely-paid order is finalized with
        # its real tx within a tick instead of being taken on faith.
        if tx is None or (
            scheme == PAYMENT_SCHEME_AGGR_DEFERRED and status == "pending"
        ):
            if order.status == "settling":
                if tx is not None and order.settle_ref != tx:
                    order.settle_ref = tx
                log_event(
                    session,
                    "agentic",
                    "agent_order.settle_pending",
                    store_id=order.store_id,
                    order_id=order_id,
                )
                session.commit()
            return
        # INV-1 settle re-verify: for a tiered buy re-derive the tier with a FRESH
        # ownership read (never the positive cache) BEFORE the terminal flip, so a
        # discount can never ride a stale cache entry. tier_applied is recorded on
        # the settled event; a below-base settle a fresh read cannot certify is
        # flagged. No agent id ⇒ the log payload is byte-identical to the pre-M16
        # exact-buy path.
        # tx is non-None past the guard above, so there is no "unparsed" fallback here.
        settle_data = _settle_tier_data(session, order, agent_id) if agent_id else None
        if _finalize_settled(session, order, tx, settle_data):
            session.commit()


async def agent_guard_dispatch(request: Request, call_next):
    """Outermost middleware (registered AFTER the x402 middleware, so it runs
    first). For POST /s/{slug}/buy it 404/409s a dead store BEFORE any payable 402
    is emitted; on the way back it records the settle tx_hash for a served buy."""
    if request.method not in ("GET", "POST") or not _BUY_PATH_RE.match(
        request.url.path
    ):
        return await call_next(request)
    slug = _slug_from_path(request.url.path)
    # The dead-store guard and the settle bookkeeping below are POST-only concerns;
    # GET /s/{slug}/buy is the discovery probe agents use to READ the challenge. It
    # still emits a 402, so it must get the same honest accepts list — the filter was
    # POST-gated, so a GET on a non-batch store advertised an aggr_deferred scheme its
    # own handler would 409.
    if request.method == "POST":
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
        if not await asyncio.to_thread(_is_batch_path, request.url.path):
            header = response.headers.get("PAYMENT-REQUIRED")
            if header:
                filtered = _filter_aggr_from_challenge(header)
                if filtered is not None:
                    response.headers["PAYMENT-REQUIRED"] = filtered
        return response
    if response.status_code == 200 and request.method == "POST":
        pr = response.headers.get("PAYMENT-RESPONSE")
        order_id = getattr(request.state, "agent_order_id", None)
        if pr and order_id:
            scheme = _payment_scheme(request)
            aid = getattr(request.state, "agent_buy_agent_id", None)
            await asyncio.to_thread(record_settlement, order_id, pr, scheme, aid)
    return response


# ============================================================================
# MCP: hand-rolled stateless JSON-RPC 2.0 (Streamable HTTP, application/json)
# ============================================================================
_MCP_NO_CONTENT = object()


class _GetProductArgs(BaseModel):
    product_id: int
    # M16 B2B: an optional ERC-8004 agent id to receive an advisory wholesale
    # quote (same gate as GET /s/{slug}/quote — INV-1 governs at settle).
    agent_id: str | None = None


class _PreviewOrderArgs(BaseModel):
    # Optional: which active product to preview. Omitted -> the store's primary
    # product. Read-only, so no `ref` attribution (nothing is reserved to attribute).
    product_id: int | None = None


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


# Phase 4 butler flow: the two-step "summarize -> then pay" contract, stated in-band
# so a concierge agent can show a human what will happen before any settlement. The
# summary is read-only; funds move ONLY on the explicit pay step (or the x402 buy
# POST), and always settle to the merchant's payTo (non-custodial).
_PREVIEW_NEXT_STEP = (
    "Preview only — nothing is reserved and nothing is charged. To buy, call "
    "create_checkout to lock an exact amount, then pay with the on-chain tx hash "
    "(x402 agents can POST the store's buy endpoint instead). Funds move only on "
    "that explicit pay step and settle to the merchant's payTo (non-custodial)."
)
_CHECKOUT_NEXT_STEP = (
    "Checkout reserved but UNPAID — nothing has been charged yet. Send exactly "
    "amount_micro of USDT to pay_to on X Layer, then call pay with the tx hash "
    "(x402 agents can POST the store's buy endpoint instead). Funds move only on "
    "that explicit pay step and settle to the merchant (non-custodial)."
)


def _order_summary(store: Store, product: Product) -> dict:
    """The confirm-before-pay summary a concierge agent shows a human before any
    settlement: what is bought, the unit price + quantity + total (USDT), the
    delivery ETA (sla_minutes), the store, the payment rail, and a one-line human
    string. Side-effect-free — the price of the goods, not a payment instruction; the
    exact on-chain amount (with its unique matching offset) is create_checkout's
    amount_micro. Quantity is 1 (one checkout buys one unit)."""
    unit_micro = product.price_micro
    quantity = 1
    total_micro = unit_micro * quantity
    sla = _effective_sla(product)
    store_name = _store_name(store)
    return {
        "store": {"name": store_name, "slug": store.slug},
        "product": {"id": product.id, "name": product.name},
        "quantity": quantity,
        "unit_price": _usdt_str(unit_micro),
        "unit_price_micro": unit_micro,
        "total": _usdt_str(total_micro),
        "total_micro": total_micro,
        "currency": CURRENCY,
        "network": NETWORK,
        "asset": ASSET,
        "pay_to": store.pay_to,
        "sla_minutes": sla,
        "settlement": "non_custodial",
        "line": (
            f"{quantity} x {product.name} from {store_name} for "
            f"{_usdt_str(total_micro)} {CURRENCY}, delivered within ~{sla} min "
            f"(paid direct to the merchant on X Layer)."
        ),
    }


def _resolve_active_product(
    session: Session, store: Store, product_id: int | None
) -> Product:
    """The active product a buy targets: the given ``product_id`` (validated to be an
    active product of THIS store) or the store's primary product. Raises _ToolError
    exactly like the callers did inline, so tool behaviour is unchanged."""
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
        return product
    product = _active_product(session, store.id)
    if product is None:
        raise _ToolError("store has no active product")
    return product


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
                "endpoint (x402-capable agents can POST straight to /s/{slug}/buy). "
                "Pass an optional ERC-8004 `agent_id` to receive an advisory "
                "wholesale quote; buy at that tier with POST /s/{slug}/buy?agent_id="
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer"},
                    "agent_id": {"type": "string"},
                },
                "required": ["product_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "preview_order",
            "description": (
                "Read-only confirmation summary for a would-be buy — product, unit "
                "price + quantity + total (USDT), delivery ETA (sla_minutes), store, "
                "payment rail, and a one-line human string — WITHOUT reserving or "
                "charging anything. Use it to show a human what will happen before "
                "paying: nothing settles until create_checkout + pay (or the x402 buy "
                "POST). Pass an optional product_id; omit for the store's primary "
                "product."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "create_checkout",
            "description": (
                "Reserve a unique-amount on-chain checkout (for agents that pay the "
                "merchant themselves and submit the tx hash via `pay`). Returns a "
                "confirmation `summary` plus the exact amount to send — but charges "
                "nothing: money moves only on the explicit `pay` step. Pass an "
                "optional product_id to check out a specific product; omit it for the "
                "store's primary product. Pass an optional `ref` (a 0x EVM address) to "
                "attribute the sale to a referring agent's payout wallet."
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
    session: Session,
    store: Store,
    slug: str,
    product_id: int,
    agent_id: str | None = None,
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
    result = {
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
        **_offering_envelope(store, slug, product),
    }
    # M16 B2B: echo the advisory wholesale quote for a presented agent id. Pass it
    # to /buy as ?agent_id=<id> to have the tier priced into the 402 (INV-1 grants
    # it at settle only if the payer wallet is that agent's on-chain owner).
    aid = b2b.parse_agent_id(agent_id) if agent_id else None
    if aid is not None:
        quote_fields = _tier_quote(product, aid)
        if quote_fields is not None:
            result["quote"] = {"agent_id": aid, **quote_fields}
    return result


def _tool_preview_order(
    session: Session, store: Store, product_id: int | None = None
) -> dict:
    """Read-only confirm-before-pay preview: the order summary and the next step, with
    NO order created and NO charge. The whole point of the two-step butler flow —
    show the human first, settle only on the explicit pay."""
    product = _resolve_active_product(session, store, product_id)
    return {"summary": _order_summary(store, product), "next_step": _PREVIEW_NEXT_STEP}


def _tool_create_checkout(
    session: Session,
    store: Store,
    product_id: int | None = None,
    referrer_addr: str | None = None,
) -> dict:
    product = _resolve_active_product(session, store, product_id)
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
        "summary": _order_summary(store, product),
        "next_step": _CHECKOUT_NEXT_STEP,
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
            result = _tool_get_product(
                session, store, slug, args.product_id, args.agent_id
            )
        elif name == "preview_order":
            pargs = _PreviewOrderArgs.model_validate(raw_args)
            result = _tool_preview_order(session, store, pargs.product_id)
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
# DEPLOYED AND PUBLIC since the deploy/nginx-m15.snippet location landed: POST
# https://tilla.gudman.xyz/mcp answers tools/list from the open internet. The note
# that used to sit here ("until then it is reachable in-app / via tests") outlived
# the deploy and led an external reviewer to report the surface as unreachable —
# a stale caveat reads as a live limitation, so re-check this line when the route
# moves rather than leaving it to rot.
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


def product_image_url(store: Store, product: Product, store_url: str) -> str | None:
    """This product's photograph as an absolute URL, or None.

    Matched by product ID rather than by position: ``content['products']`` is rebuilt
    from the live Product rows on every catalog edit, so an index would eventually
    hand an agent a picture of a different item than the one it is being offered.
    """
    content = store.content if isinstance(store.content, dict) else {}
    for item in content.get("products") or []:
        if isinstance(item, dict) and item.get("id") == product.id:
            return imagery.product_image_url(item, store_url)
    return None


def _feed_product(store: Store, slug: str, product: Product, store_url: str) -> dict:
    image_url = product_image_url(store, product, store_url)
    return {
        "id": str(product.id),
        "title": product.name,
        "description": _product_description(store),
        "link": store_url,
        # Additive and omitted entirely when the product has no photograph, so an
        # agent never has to distinguish "no picture" from "broken picture".
        **({"image_url": image_url} if image_url else {}),
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
        **_offering_envelope(store, slug, product),
        # Phase 1.2: the typed contract an agent supplies to the x402 buy endpoint.
        # The product is fixed by the x402 endpoint path and payment rides the x402
        # header, so the only request-shaping inputs are the buy route's two optional
        # query params (ref/agent_id) — mirrors the agent-card 'buy' input_schema.
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": (
                        "optional 0x affiliate wallet to attribute the sale to"
                    ),
                },
                "agent_id": {
                    "type": "string",
                    "description": (
                        "optional ERC-8004 agent id to price a wholesale tier; "
                        "granted at settle only if the payer wallet is its owner"
                    ),
                },
            },
        },
    }


@router.get("/s/{slug}/quote")
@limiter.limit("30/minute")
def quote(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    agent_id: str = Query(..., max_length=40),
    session: Session = Depends(get_session),
):
    """M16 B2B: an advisory wholesale quote for a presented ERC-8004 agent id. The
    tier price is returned ONLY when a tier matches AND the agent id's on-chain
    owner is verifiable (read-only registry eth_call) — the quote also states that
    the discount is GRANTED at settle only if the payer wallet equals that owner
    (INV-1). Any unverifiable claim / RPC outage / no matching tier => base price
    only (fail-to-base). Public + rate-limited; reveals no other buyer's terms."""
    store = _live_store(session, slug)
    if store is None:
        raise HTTPException(404, "store not found")
    product = _active_product(session, store.id)
    if product is None:
        raise HTTPException(404, "no active product")
    aid = b2b.parse_agent_id(agent_id)
    if aid is None:
        raise HTTPException(422, "agent_id must be a numeric ERC-8004 id")
    base = product.price_micro
    body: dict = {
        "agent_id": aid,
        "base_price_micro": base,
        "base_price": _usdt_str(base),
        "currency": CURRENCY,
    }
    quote_fields = _tier_quote(product, aid)
    if quote_fields is not None:
        body.update(quote_fields)
    return JSONResponse(body, headers=_AGENT_HEADERS)


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


@router.get("/s/{slug}/reviews")
@limiter.limit("60/minute")
def store_reviews(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    limit: int = 20,
    session: Session = Depends(get_session),
):
    """Public, screened verified-buyer reviews for a store, newest first, plus the
    aggregate (avg rating + count). Every listed body was Warden-screened before it
    was stored, so nothing unscreened is ever served; the reviewer wallet is surfaced
    only in an abbreviated 0x1234…cdef form (no full-wallet dump). JSON-only surface
    (no HTML context to escape), 404 for any non-live store."""
    store = _live_store(session, slug)
    if store is None:  # 404 for ANY non-live store (never leak pending)
        raise HTTPException(404, "store not found")
    limit = max(1, min(limit, 100))
    ravg, rcount = session.execute(
        select(func.avg(Review.rating), func.count(Review.id)).where(
            Review.store_id == store.id
        )
    ).one()
    rows = session.scalars(
        select(Review)
        .where(Review.store_id == store.id)
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(limit)
    ).all()
    body = {
        "slug": store.slug,
        "review_avg": round(float(ravg), 2) if ravg is not None else None,
        "review_count": rcount or 0,
        "reviews": [
            {
                "rating": r.rating,
                "body": r.body,
                "buyer": f"{r.from_addr[:6]}…{r.from_addr[-4:]}",
                "created_at": r.created_at.isoformat() + "Z",
            }
            for r in rows
        ],
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

    # create-store's requiredFunds is the fixed platform charge — amount derived from
    # the SAME PAYMENT_AMOUNT constant build_payment_option feeds the /create-store
    # x402 route, so it can never drift from the real 402. payTo is Tilla's own rail
    # wallet (Tilla earning its service fee); omitted when PAY_TO_ADDRESS is unset
    # (no middleware — e.g. tests) rather than advertising a wallet we cannot honor.
    create_store_funds: dict = {
        "amount_micro": int(PAYMENT_AMOUNT),
        "amount": _usdt_str(int(PAYMENT_AMOUNT)),
        "currency": CURRENCY,
        "asset": ASSET,
        "network": NETWORK,
    }
    tilla_pay_to = os.environ.get("PAY_TO_ADDRESS")
    if tilla_pay_to:
        create_store_funds["pay_to"] = tilla_pay_to

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
                "x402": {
                    "endpoint": "/create-store",
                    "price": f"{create_store_funds['amount']} {CURRENCY}",
                },
                "input_schema": CreateStoreBody.model_json_schema(),
                "sample_request": {
                    "description": "single-origin coffee beans, roasted to order",
                    "theme": "original",
                },
                "sla_minutes": DELIVERY_SLA_MINUTES,
                "requiredFunds": create_store_funds,
                "nextActions": [
                    {
                        "type": "x402_buy",
                        "method": "POST",
                        "endpoint": "/create-store",
                        "protocol": "x402-v2",
                        "description": (
                            "POST {description, theme} with an x402 payment "
                            f"({create_store_funds['amount']} {CURRENCY}) "
                            "to spin up a live store."
                        ),
                    }
                ],
            },
            {
                "id": "buy",
                "name": "Buy from a store",
                "description": (
                    "Purchase a store's product; payTo is the merchant. Confirm "
                    "before pay: the store's MCP preview_order returns a read-only "
                    "summary, create_checkout reserves an exact amount, and money "
                    "moves only on the explicit pay step."
                ),
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
                # requiredFunds is per-offering (varies by store/product), so it is
                # resolved from the store's own feed.json / get_product envelope —
                # nextActions names exactly how to reach it and then pay.
                "nextActions": [
                    {
                        "type": "discover",
                        "method": "GET",
                        "endpoint": "/discovery/resources",
                        "description": "List live stores to pick a slug.",
                    },
                    {
                        "type": "feed",
                        "method": "GET",
                        "endpoint": "/s/{slug}/feed.json",
                        "description": (
                            "Fetch a store's per-product requiredFunds + buy endpoints."
                        ),
                    },
                    {
                        "type": "x402_buy",
                        "method": "POST",
                        "endpoint": "/s/{slug}/buy",
                        "protocol": "x402-v2",
                        "description": (
                            "POST an x402 payment; payTo is the merchant "
                            "(non-custodial)."
                        ),
                    },
                ],
            },
            {
                "id": "verify-delivery",
                "name": "Verify a delivery",
                "description": (
                    "Signed evidence for one delivered order. The chain already "
                    "proves the payment; this proves what was delivered against "
                    "it — the content hash of the payload, whether the buyer "
                    "accepted or disputed it, and any refund. On-chain anchors "
                    "(settlement tx, EAS attestation) ride along so the verifiable "
                    "half needs no trust in Tilla."
                ),
                "x402": {
                    "endpoint": "/verify-delivery",
                    "price": f"0.01 {CURRENCY}",
                },
                "input_schema": {
                    "type": "object",
                    "description": "supply exactly one handle",
                    "properties": {
                        "order_id": {"type": "string"},
                        "tx_hash": {"type": "string"},
                        "attestation_uid": {"type": "string"},
                    },
                },
                "sample_request": {"tx_hash": "0x" + "ab" * 32},
                "nextActions": [
                    {
                        "type": "x402_buy",
                        "method": "POST",
                        "endpoint": "/verify-delivery",
                        "protocol": "x402-v2",
                        "description": (
                            "POST one of {order_id, tx_hash, attestation_uid} with "
                            "an x402 payment to get the signed bundle."
                        ),
                    },
                    {
                        "type": "verify",
                        "method": "POST",
                        "endpoint": "/api/verify-evidence",
                        "description": (
                            "Re-check any bundle's signature. Free, no payment."
                        ),
                    },
                ],
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
# refunded) = quality; unique buyers = independence, so the upper tiers cannot be
# reached by one wallet buying repeatedly (see _trust_tier). A refund-heavy store is
# demoted to 'watch' regardless of volume — the auto-demote. Checkout expiries are
# deliberately NOT an input: an expired checkout is buyer abandonment, not store
# fault (the same reason they are excluded from success_rate), so they can never
# unfairly demote a store.
TRUST_TIERS = ("new", "watch", "emerging", "established", "trusted")


def _trust_tier(sold: int, success_rate: float | None, buyers: int = 0) -> str:
    """The graduation ladder. Volume and quality are necessary but not sufficient:
    the upper tiers also require the sales to come from SEVERAL buyers.

    Without that, repeat purchases by one wallet read as demand. They are not — and
    the wallet doing the repeating can be the store's own operator, which is how
    `invoice-flow` sat in discovery as 'established' on four orders Tilla paid
    itself. A tier an agent buyer ranks on must not be reachable by a merchant
    buying from themselves.

    Buyer diversity is used rather than an allowlist of Tilla-owned wallets on
    purpose: an allowlist is a constant that goes stale silently, and the payer
    census has been mislabelled in both directions before. RESIDUAL LIMITATION,
    stated plainly: one party controlling several wallets can still manufacture
    diversity. This ladder makes that harder, not impossible — the external/self
    split lives in scripts/merchant_funnel.py, which prints every buyer raw."""
    if sold == 0:
        return "new"
    # sold >= 1 => at least one delivery => success_rate is a real float here.
    if success_rate is not None and success_rate < 0.5:
        return "watch"
    if sold >= 8 and buyers >= 3 and success_rate is not None and success_rate >= 0.95:
        return "trusted"
    if sold >= 3 and buyers >= 2 and success_rate is not None and success_rate >= 0.9:
        return "established"
    return "emerging"


def _discovery_row(
    store: Store,
    pmin,
    pmax,
    sold,
    buyers,
    last_sale,
    refunded,
    rejected,
    pending,
    review_avg,
    review_count,
) -> dict:
    # Reputation signals an agent buyer can rank on, computed from terminal orders:
    # sold_count = delivered count; success_rate = good / (good + rejected + refunded)
    # where good = delivered minus buyer-rejected minus still-in-window (Phase 1.6);
    # unique_buyer_count = distinct payer addresses; last_sale_at = most recent delivery;
    # pending_eval_count = fresh sales inside the dispute window (not yet counted);
    # disputed_count = buyer-rejected deliveries; trust_tier = the graduation ladder above.
    # review_avg/review_count = Phase 3 verified-buyer reviews (one per delivered order).
    # Abandoned/expired checkouts are the buyer's doing, not the store's, so excluded.
    sold = sold or 0
    refunded = refunded or 0
    rejected = rejected or 0
    pending = pending or 0
    good = sold - rejected - pending
    settled = good + rejected + refunded
    success_rate = round(good / settled, 4) if settled else None
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
        "pending_eval_count": pending,
        "disputed_count": rejected,
        "trust_tier": _trust_tier(good, success_rate, buyers or 0),
        "review_avg": round(float(review_avg), 2) if review_avg is not None else None,
        "review_count": review_count or 0,
        "created_at": store.created_at.isoformat() + "Z",
        # nextActions routes an agent to the per-store surfaces that carry each
        # offering's exact requiredFunds (feed.json / MCP get_product). A store-level
        # requiredFunds cannot be exact here (a store may list several products) and
        # the merchant wallet is deliberately never bulk-exported in discovery, so
        # the envelope's funds half is resolved one hop deeper, per offering.
        "nextActions": [
            {
                "type": "feed",
                "method": "GET",
                "endpoint": f"/s/{store.slug}/feed.json",
                "description": (
                    "Fetch per-product requiredFunds + x402 buy endpoints."
                ),
            },
            {
                "type": "mcp",
                "method": "POST",
                "endpoint": f"/s/{store.slug}/mcp",
                # Must stay in step with the tools `tools/list` actually serves
                # and with docs/specs/schemas/mcp-tools-list.yaml — this advert
                # had gone stale at four, omitting preview_order, so discovery
                # under-sold the catalogue it was pointing agents at.
                "tools": [
                    "list_products",
                    "get_product",
                    "preview_order",
                    "create_checkout",
                    "pay",
                ],
                "description": "Browse products and check out via JSON-RPC.",
            },
            {
                "type": "x402_buy",
                "method": "POST",
                "endpoint": f"/s/{store.slug}/buy",
                "protocol": "x402-v2",
                "description": "Buy the primary product; payTo is the merchant.",
            },
        ],
    }


# Discovery sort keys an agent buyer can pass. 'sold' is the default and preserves
# the prior order (most-sold first); an unknown value falls back to it.
DISCOVERY_SORTS = ("sold", "success", "buyers", "recent", "new")


def _discovery_rows(
    session: Session, where_clauses, limit: int, offset: int, sort: str = "sold"
):
    delivered = Order.status.in_(checkout.TERMINAL_DELIVERED)
    # Phase 1.6: a delivery counts toward the rate only once it is settled-evaluated —
    # rejected is a failure; still 'pending' AND inside the dispute window (or with an
    # unknown delivery time) is not yet counted. 'none'/'confirmed'/window-elapsed all
    # fall through to good. cutoff = now - window, so the auto-confirm is a read-time
    # comparison (no poller).
    cutoff = checkout._now() - timedelta(days=EVAL_WINDOW_DAYS)
    rejected_case = delivered & (Order.eval_status == "rejected")
    pending_case = (
        delivered
        & (Order.eval_status == "pending")
        & or_(Order.paid_at.is_(None), Order.paid_at >= cutoff)
    )
    metrics_sq = (
        select(
            Order.store_id.label("sid"),
            func.count(case((delivered, Order.id))).label("sold"),
            func.count(func.distinct(case((delivered, Order.from_addr)))).label(
                "buyers"
            ),
            func.max(case((delivered, Order.paid_at))).label("last_sale"),
            func.count(case((Order.status == "refunded", Order.id))).label("refunded"),
            func.count(case((rejected_case, Order.id))).label("rejected"),
            func.count(case((pending_case, Order.id))).label("pending"),
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
    # Phase 3 verified-buyer reviews: avg rating + count per store. Un-fakeable — a
    # review row only exists for a delivered order paid by the reviewer wallet.
    reviews_sq = (
        select(
            Review.store_id.label("sid"),
            func.avg(Review.rating).label("ravg"),
            func.count(Review.id).label("rcount"),
        )
        .group_by(Review.store_id)
        .subquery()
    )
    sold_c = func.coalesce(metrics_sq.c.sold, 0)
    rejected_c = func.coalesce(metrics_sq.c.rejected, 0)
    pending_c = func.coalesce(metrics_sq.c.pending, 0)
    refunded_c = func.coalesce(metrics_sq.c.refunded, 0)
    # good = delivered, not rejected, not still-in-window; settled excludes in-window
    # pending, so a store's rate reflects only buyer-validated (or window-elapsed) sales.
    good_c = sold_c - rejected_c - pending_c
    settled_c = good_c + rejected_c + refunded_c
    # Untried stores (nothing settled) sort last on 'success' via a -1 sentinel.
    success_c = case((settled_c > 0, good_c * 1.0 / settled_c), else_=-1.0)
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
            metrics_sq.c.rejected,
            metrics_sq.c.pending,
            reviews_sq.c.ravg,
            reviews_sq.c.rcount,
        )
        .outerjoin(price_sq, price_sq.c.sid == Store.id)
        .outerjoin(metrics_sq, metrics_sq.c.sid == Store.id)
        .outerjoin(reviews_sq, reviews_sq.c.sid == Store.id)
        # Phase 1.7: hidden (sandbox) stores stay out of bulk discovery.
        .where(Store.status == "live", Store.visibility == "public", *where_clauses)
        .order_by(*order_by)
        .limit(limit)
        .offset(offset)
    )
    # The SELECT column order matches _discovery_row's parameter order, so each Row
    # (row[0]=Store, then the scalar aggregates) unpacks straight into it.
    return [_discovery_row(*row) for row in session.execute(stmt).all()]


@router.get("/discovery/resources")
@limiter.limit("30/minute")
def discovery_resources(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    include: str = "",
    sort: str = "sold",
    session: Session = Depends(get_session),
):
    limit = max(1, min(limit, 50))
    offset = max(0, min(offset, 100_000))
    sort = sort if sort in DISCOVERY_SORTS else "sold"
    total = session.scalar(
        select(func.count())
        .select_from(Store)
        # Must mirror _discovery_rows' filter: it excludes hidden stores, so counting
        # them here reported a total larger than any page could ever return (live: 18
        # vs 15 rows), which reads to a paging client as missing results.
        .where(Store.status == "live", Store.visibility == "public")
    )
    resources = _discovery_rows(session, [], limit, offset, sort)
    body: dict = {
        "service": SERVICE,
        "agent_card": "/.well-known/agent-card.json",
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "sorts": list(DISCOVERY_SORTS),
        "resources": resources,
    }
    # M16.4: opt-in federated peer listings. Each row is labeled
    # {"origin", "federated": true} and links OUT to the peer's own checkout —
    # Tilla never proxies, quotes, or settles a peer's sale. Dormant (empty) when
    # no peers are configured. Peer content is re-emitted json-encoded, never
    # rendered, so there is no HTML context to escape.
    if include == "federated":
        body["federated"] = federation.federated_rows(session, limit)
    return JSONResponse(body, headers=_AGENT_HEADERS)


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
