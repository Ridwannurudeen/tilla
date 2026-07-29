"""M13 ACP (Agentic Commerce Protocol) checkout sessions.

Implements the five ACP-standard per-store endpoints (spec 2026-04-17) on top of the
proven M3 order machinery, so the same store is buyable via the OpenAI/Stripe
agentic-commerce standard in addition to the OKX x402 rail:

    POST /s/{slug}/checkout_sessions              create
    GET  /s/{slug}/checkout_sessions/{id}         retrieve
    POST /s/{slug}/checkout_sessions/{id}         update (buyer info only)
    POST /s/{slug}/checkout_sessions/{id}/complete
    POST /s/{slug}/checkout_sessions/{id}/cancel

DORMANT by default: every endpoint 503s until TILLA_ACP_ENABLED (the MPP/subscription
mount pattern), flipped ON only after a live smoke of the tx-hash complete path. The
complete handler ships ONE mode: a custom x402/USDT0 handler where the agent submits
{provider:"onchain_usdt0", tx_hash} and Tilla verifies it through the exact M3
``checkout.verify_txhash`` (exact-amount, one-tx-one-order, quarantine rules all
inherited). The x402-middleware complete mode is spike-gated and PARKED — it is NOT
registered here, so tx-hash mode alone is what is claimed. NON-CUSTODIAL throughout:
no card/token payment data is ever accepted or stored, and no funds move.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets

import httpx
from fastapi import APIRouter, HTTPException, Path, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app import affiliates, agentic, chain, checkout, config
from app.buyer_inputs import validate_buyer_inputs
from app.db import SessionLocal
from app.limiter import limiter
from app.models import AcpSession, Order, Product, Store, log_event

logger = logging.getLogger("tilla")

router = APIRouter()

_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
# No caching on a mutable session resource; nosniff on the JSON body.
_ACP_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}


# ------------------------------------------------------------ gates + parsing
def _require_enabled() -> None:
    if not config.ACP_ENABLED:
        raise HTTPException(503, "ACP checkout is not enabled")


def _verify_signature(request: Request, raw: bytes) -> None:
    """Optional HMAC request-signature verification: active ONLY when a platform
    secret is set (TILLA_ACP_SIGNING_SECRET). The client signs the raw request body
    with HMAC-SHA256; a missing/mismatched signature 401s. Unset -> skipped."""
    secret = config.TILLA_ACP_SIGNING_SECRET
    if not secret:
        return
    provided = request.headers.get("ACP-Signature", "") or request.headers.get(
        "Signature", ""
    )
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(401, "invalid request signature")


def _parse_json(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, "invalid JSON body") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "body must be a JSON object")
    return data


def _headers(api_version: str | None) -> dict:
    h = dict(_ACP_HEADERS)
    if api_version:
        h["API-Version"] = api_version  # spec: echo the requested API version
    return h


def _new_id() -> str:
    return "acp_" + secrets.token_urlsafe(24)


# ------------------------------------------------------------ session helpers
def _active_products(session: Session, store_id: int) -> list[Product]:
    return list(
        session.scalars(
            select(Product)
            .where(Product.store_id == store_id, Product.active.is_(True))
            .order_by(Product.id)
        )
    )


def _get_acp(session: Session, store: Store, session_id: str) -> AcpSession:
    acp = session.get(AcpSession, session_id)
    if acp is None or acp.store_id != store.id:
        raise HTTPException(404, "checkout session not found")
    return acp


def _validate_items(raw_items, session: Session, store: Store) -> Product:
    """Validate the ACP line items against the store's active products. This is a
    single-line-item digital store, so exactly one item of quantity 1 is accepted;
    an absent items list defaults to the primary product."""
    products = _active_products(session, store.id)
    if not products:
        raise HTTPException(409, "store has no active product")
    if not raw_items:
        return products[0]
    if not isinstance(raw_items, list) or len(raw_items) != 1:
        raise HTTPException(422, "this store accepts exactly one line item")
    item = raw_items[0]
    if not isinstance(item, dict):
        raise HTTPException(422, "invalid line item")
    qty = item.get("quantity", 1)
    if qty not in (1, "1"):
        raise HTTPException(422, "quantity must be 1 for a digital product")
    pid = item.get("id")
    if pid is None:
        return products[0]
    for p in products:
        if str(p.id) == str(pid):
            return p
    raise HTTPException(422, "line item does not match an active product")


def _extract_affiliate(body: dict) -> str | None:
    """Map the ACP-shape ``affiliate`` object to a validated referrer address."""
    aff = body.get("affiliate")
    if not isinstance(aff, dict):
        return None
    raw = aff.get("address") or aff.get("code") or aff.get("id")
    try:
        return affiliates.normalize_ref(raw if isinstance(raw, str) else None)
    except affiliates.RefRejected as exc:
        raise HTTPException(422, str(exc)) from exc


def _effective_status(acp: AcpSession, order: Order | None) -> str:
    """The session status reconciled against the underlying order: a delivered order
    is completed; an expired/canceled/reorged/refunded order surfaces as a canceled
    session; otherwise the stored status stands."""
    if acp.status in ("completed", "canceled"):
        return acp.status
    if order is None:
        return acp.status
    if order.status in checkout.TERMINAL_DELIVERED:
        return "completed"
    if order.status in ("expired", "canceled", "reorged", "refunded"):
        return "canceled"
    return "ready_for_payment"


def _session_shape(
    session: Session, acp: AcpSession, store: Store, order: Order | None
):
    product = (
        session.get(Product, order.product_id)
        if order is not None and order.product_id
        else None
    )
    amount_micro = order.expected_micro if order is not None else 0
    amount = agentic._usdt_str(amount_micro)
    line_items = []
    if product is not None:
        line_items.append(
            {
                "id": str(product.id),
                "item": {"id": str(product.id), "quantity": 1},
                "base_amount": agentic._usdt_str(product.price_micro),
                "amount": amount,
            }
        )
    shape = {
        "id": acp.id,
        "object": "checkout_session",
        "status": acp.status,
        "currency": "usdt",
        "line_items": line_items,
        "totals": [
            {"type": "subtotal", "display_text": "Subtotal", "amount": amount},
            {"type": "total", "display_text": "Total", "amount": amount},
        ],
        "fulfillment": {"type": "digital"},
        "buyer": acp.buyer,
        "buyer_inputs": order.buyer_inputs if order is not None else None,
        # The custom x402 handler (for x402-native agents) + the on-chain USDT0
        # fallback the tx-hash complete path settles. pay_to is the MERCHANT wallet
        # (non-custodial) — the buyer needs it to send funds, exactly as the human
        # /api/checkout response surfaces it.
        "payment": {
            "custom": {
                "provider": "x402",
                "network": agentic.NETWORK,
                "asset": agentic.ASSET,
                "endpoint": f"/s/{store.slug}/buy",
            },
            "fallback": {
                "provider": "onchain_usdt0",
                "pay_to": store.pay_to,
                "amount": amount,
                "amount_micro": amount_micro,
                "expires_at": (
                    order.expires_at.isoformat() + "Z"
                    if order is not None and order.expires_at
                    else None
                ),
            },
        },
        "messages": [],
        "order": None,
    }
    if acp.status == "completed" and order is not None:
        from app.main import _order_response

        shape["order"] = _order_response(session, order, include_gated=False)
    return shape


# ------------------------------------------------------------ cores (off-loop)
def _create_core(slug, body, api_version, idem):
    with SessionLocal() as session:
        store = agentic._require_live_store(session, slug)
        if idem:
            existing = session.scalar(
                select(AcpSession).where(
                    AcpSession.store_id == store.id,
                    AcpSession.idempotency_key == idem,
                )
            )
            if existing is not None:
                order = (
                    session.get(Order, existing.order_id) if existing.order_id else None
                )
                return _session_shape(session, existing, store, order), 200
        product = _validate_items(body.get("items"), session, store)
        ref = _extract_affiliate(body)
        buyer_inputs = validate_buyer_inputs(product, body.get("inputs"))
        try:
            order = checkout.create_order(
                session, store, product, ref, buyer_inputs=buyer_inputs
            )
        except checkout.AmountUnavailable as exc:
            raise HTTPException(503, "checkout busy, retry") from exc
        acp = AcpSession(
            id=_new_id(),
            store_id=store.id,
            order_id=order.id,
            status="ready_for_payment",
            idempotency_key=idem,
            buyer=body.get("buyer") if isinstance(body.get("buyer"), dict) else None,
            items=[{"id": str(product.id), "quantity": 1}],
            api_version=api_version,
        )
        session.add(acp)
        try:
            session.flush()
        except IntegrityError:
            # A concurrent create with the same Idempotency-Key won; reconcile to it.
            session.rollback()
            existing = session.scalar(
                select(AcpSession).where(
                    AcpSession.store_id == store.id,
                    AcpSession.idempotency_key == idem,
                )
            )
            if existing is None:
                raise HTTPException(409, "idempotency conflict") from None
            order = session.get(Order, existing.order_id) if existing.order_id else None
            return _session_shape(session, existing, store, order), 200
        log_event(
            session, "acp", "acp.session_created", store_id=store.id, order_id=order.id
        )
        session.commit()
        return _session_shape(session, acp, store, order), 201


def _retrieve_core(slug, session_id):
    with SessionLocal() as session:
        store = agentic._require_live_store(session, slug)
        acp = _get_acp(session, store, session_id)
        order = session.get(Order, acp.order_id) if acp.order_id else None
        if (
            order is not None
            and order.status not in checkout.TERMINAL_DELIVERED
            and order.status != "refunded"
        ):
            checkout.refresh_order(session, order)
            session.expire(order)
            order = session.get(Order, acp.order_id)
        eff = _effective_status(acp, order)
        if eff != acp.status and acp.status not in ("completed", "canceled"):
            acp.status = eff
            session.commit()
        return _session_shape(session, acp, store, order), 200


def _update_core(slug, session_id, body):
    with SessionLocal() as session:
        store = agentic._require_live_store(session, slug)
        acp = _get_acp(session, store, session_id)
        if acp.status in ("completed", "canceled"):
            raise HTTPException(409, f"session is {acp.status}")
        buyer = body.get("buyer")
        if isinstance(buyer, dict):
            acp.buyer = buyer
        session.commit()
        order = session.get(Order, acp.order_id) if acp.order_id else None
        return _session_shape(session, acp, store, order), 200


def _complete_core(slug, session_id, body):
    with SessionLocal() as session:
        store = agentic._require_live_store(session, slug)
        acp = _get_acp(session, store, session_id)
        order = session.get(Order, acp.order_id) if acp.order_id else None
        if order is None:
            raise HTTPException(409, "session has no payable order")
        if acp.status == "canceled":
            raise HTTPException(409, "session is canceled")
        if acp.status == "completed":
            return _session_shape(session, acp, store, order), 200  # idempotent
        payment = body.get("payment_data") or body.get("payment") or {}
        if not isinstance(payment, dict):
            raise HTTPException(422, "payment_data must be an object")
        provider = payment.get("provider")
        if provider not in (None, "onchain_usdt0"):
            raise HTTPException(422, "unsupported payment provider; use onchain_usdt0")
        tx_hash = payment.get("tx_hash") or payment.get("transaction") or ""
        if not _TX_HASH.fullmatch(tx_hash):
            raise HTTPException(422, "payment_data.tx_hash is required")
        try:
            checkout.verify_txhash(session, order, tx_hash.lower())
        except checkout.TxAlreadyUsed as exc:
            raise HTTPException(409, "tx already used") from exc
        except checkout.TxVerificationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (httpx.HTTPError, chain.ChainError) as exc:
            raise HTTPException(502, "chain verification unavailable") from exc
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        session.expire(order)
        order = session.get(Order, acp.order_id)
        if order.status in checkout.TERMINAL_DELIVERED:
            acp.status = "completed"
            log_event(
                session,
                "acp",
                "acp.session_completed",
                store_id=store.id,
                order_id=order.id,
            )
            session.commit()
        return _session_shape(session, acp, store, order), 200


def _cancel_core(slug, session_id):
    with SessionLocal() as session:
        store = agentic._require_live_store(session, slug)
        acp = _get_acp(session, store, session_id)
        if acp.status == "completed":
            raise HTTPException(409, "session already completed")
        order = session.get(Order, acp.order_id) if acp.order_id else None
        # Release a still-unpaid order (pending/expired only): a detected/underpaid
        # order has funds in flight and is never force-canceled here.
        if order is not None and order.status in ("pending", "expired"):
            checkout.transition(session, order.id, ("pending", "expired"), "canceled")
        acp.status = "canceled"
        log_event(
            session,
            "acp",
            "acp.session_canceled",
            store_id=store.id,
            order_id=acp.order_id,
        )
        session.commit()
        if order is not None:
            session.expire(order)
        order = session.get(Order, acp.order_id) if acp.order_id else None
        return _session_shape(session, acp, store, order), 200


# ------------------------------------------------------------------- routes
@router.post("/s/{slug}/checkout_sessions")
@limiter.limit("30/minute")
async def acp_create(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    _require_enabled()
    raw = await request.body()
    _verify_signature(request, raw)
    body = _parse_json(raw)
    api_version = request.headers.get("API-Version")
    idem = request.headers.get("Idempotency-Key")
    shape, status = await asyncio.to_thread(_create_core, slug, body, api_version, idem)
    return JSONResponse(shape, status_code=status, headers=_headers(api_version))


@router.get("/s/{slug}/checkout_sessions/{session_id}")
@limiter.limit("60/minute")
async def acp_retrieve(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session_id: str = Path(..., min_length=1, max_length=64),
):
    _require_enabled()
    api_version = request.headers.get("API-Version")
    shape, status = await asyncio.to_thread(_retrieve_core, slug, session_id)
    return JSONResponse(shape, status_code=status, headers=_headers(api_version))


@router.post("/s/{slug}/checkout_sessions/{session_id}")
@limiter.limit("30/minute")
async def acp_update(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session_id: str = Path(..., min_length=1, max_length=64),
):
    _require_enabled()
    raw = await request.body()
    _verify_signature(request, raw)
    body = _parse_json(raw)
    api_version = request.headers.get("API-Version")
    shape, status = await asyncio.to_thread(_update_core, slug, session_id, body)
    return JSONResponse(shape, status_code=status, headers=_headers(api_version))


@router.post("/s/{slug}/checkout_sessions/{session_id}/complete")
@limiter.limit("30/minute")
async def acp_complete(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session_id: str = Path(..., min_length=1, max_length=64),
):
    _require_enabled()
    raw = await request.body()
    _verify_signature(request, raw)
    body = _parse_json(raw)
    api_version = request.headers.get("API-Version")
    shape, status = await asyncio.to_thread(_complete_core, slug, session_id, body)
    return JSONResponse(shape, status_code=status, headers=_headers(api_version))


@router.post("/s/{slug}/checkout_sessions/{session_id}/cancel")
@limiter.limit("30/minute")
async def acp_cancel(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session_id: str = Path(..., min_length=1, max_length=64),
):
    _require_enabled()
    raw = await request.body()
    _verify_signature(request, raw)
    api_version = request.headers.get("API-Version")
    shape, status = await asyncio.to_thread(_cancel_core, slug, session_id)
    return JSONResponse(shape, status_code=status, headers=_headers(api_version))
