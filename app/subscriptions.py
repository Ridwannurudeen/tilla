"""M8 subscriptions (x402 ``period`` scheme) — a thin FastAPI proxy in front of the
always-on localhost Node sidecar (the JS SDK is the only implementation of the
period scheme; there is no Python one, so it is NOT wired into the x402 middleware).

DORMANT-SAFE: ``POST /s/{slug}/subscribe`` is always mounted but fail-closes to 503
unless ``config.SUBSCRIPTIONS_ENABLED`` is set. Even then, the sidecar itself only
contacts the OKX facilitator when OKX creds are present, so a real subscribe/charge
needs creds + a USER-funded Permit2 buyer. NEVER a false 402/200: if the sidecar is
unreachable at any step the proxy 503s, and ONLY a facilitator settle success ever
creates an Order + delivers — a settle failure or 503 delivers nothing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Path, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.responses import JSONResponse

from app import agentic, checkout, config
from app.db import SessionLocal
from app.limiter import limiter
from app.models import Order, Product, Store, log_event

logger = logging.getLogger("tilla")

router = APIRouter()

_SUB_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
_SIDECAR_TIMEOUT = 5.0


def _subscription_ctx(slug: str) -> dict:
    """Resolve a live store's active SUBSCRIPTION product (404 unknown/blocked, 409
    pending / non-subscription), returning the fields the challenge is built from."""
    with SessionLocal() as session:
        store = session.scalar(select(Store).where(Store.slug == slug))
        if store is None:
            raise HTTPException(404, "store not found")
        if store.status == "pending_screening":
            raise HTTPException(
                409, "store is not yet live (pending content screening)"
            )
        if store.status != "live":
            raise HTTPException(404, "store not found")
        product = session.scalar(
            select(Product)
            .where(Product.store_id == store.id, Product.active.is_(True))
            .order_by(Product.id)
        )
        if product is None:
            raise HTTPException(409, "store has no active product")
        if (product.pricing_model or "one_time") != "subscription":
            raise HTTPException(409, "product is not a subscription product")
        params = (
            product.pricing_params if isinstance(product.pricing_params, dict) else {}
        )
        return {
            "store_id": store.id,
            "product_id": product.id,
            "pay_to": store.pay_to,
            "amount_per_period_micro": int(params.get("amount_per_period_micro") or 0),
            "period_sec": int(params.get("period_sec") or 0),
            "max_periods": int(params.get("max_periods") or 0),
            "plan_id": str(params.get("plan_id") or "default"),
            "plan_tier": int(params.get("plan_tier") or 1),
            "plan_name": str(params.get("plan_name") or "Subscription"),
        }


async def _sidecar_post(path: str, *, json: dict, headers: dict | None = None):
    """POST to the localhost sidecar. Any transport failure -> 503 (never a false
    402/200): the sidecar is the only thing that speaks the period scheme."""
    url = config.SUBSCRIPTION_SIDECAR_URL.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=_SIDECAR_TIMEOUT) as client:
            return await client.post(url, json=json, headers=headers or {})
    except httpx.HTTPError:
        logger.exception("subscription sidecar unreachable at %s", path)
        raise HTTPException(503, "subscription sidecar unreachable") from None


def _challenge_req(ctx: dict) -> dict:
    """The exact body POSTed to the sidecar /subscriptions/challenge — the single
    source of truth for the subscription terms (payTo, per-period amount, period).
    Used both to serve the 402 AND to rebuild the requirements server-side at
    verify/settle, so a buyer can never substitute their own terms (e.g. pay
    themselves 1 micro)."""
    return {
        "payTo": ctx["pay_to"],
        "amount": str(ctx["amount_per_period_micro"]),
        "period": ctx["period_sec"],
        "maxPeriods": ctx["max_periods"],
        "plan": {
            "id": ctx["plan_id"],
            "tier": ctx["plan_tier"],
            "name": ctx["plan_name"],
        },
    }


async def _serve_challenge(ctx: dict) -> JSONResponse:
    """No PAYMENT-SIGNATURE yet: ask the sidecar to build the period 402 challenge
    from the product's pricing_params and relay its body + APP-PAYMENT-REQUIRED
    header verbatim."""
    resp = await _sidecar_post("/subscriptions/challenge", json=_challenge_req(ctx))
    headers = dict(_SUB_HEADERS)
    app_pr = resp.headers.get("APP-PAYMENT-REQUIRED")
    if app_pr:
        headers["APP-PAYMENT-REQUIRED"] = app_pr
    return JSONResponse(_safe_json(resp), status_code=resp.status_code, headers=headers)


def _safe_json(resp) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise HTTPException(
            502, "subscription sidecar returned a non-JSON body"
        ) from None
    return body if isinstance(body, dict) else {"data": body}


def _subscription_idem_key(sig: str) -> str:
    """A stable idempotency key for a subscription PAYMENT-SIGNATURE — the period
    scheme's analogue of the exact rail's EIP-3009 nonce. Prefers the buyer's
    EIP-712 terms signature (unique per authorization); falls back to the whole
    header when the envelope cannot be decoded. Hashed to 32 bytes so it fits the
    shared ``x402_nonce`` unique index."""
    basis = sig
    try:
        decoded = json.loads(base64.b64decode(sig))
        inner = decoded.get("payload") if isinstance(decoded, dict) else None
        terms_sig = inner.get("termsSignature") if isinstance(inner, dict) else None
        if isinstance(terms_sig, str) and terms_sig:
            basis = terms_sig
    except Exception:
        pass
    return "0x" + hashlib.sha256(basis.encode()).hexdigest()


def _recovered_payer(sig: str) -> str | None:
    """The subscription's payer wallet — ``terms.payer`` from the buyer's decoded
    PAYMENT-SIGNATURE, lowercased. This is the signer ``verifySubscribe`` binds the
    termsSignature to; it is recorded on the Order at first settle and required to
    match on every same-store replay, so a captured (public) terms-signature can
    never hand another wallet this store's goods. None when the envelope carries no
    decodable payer (the replay guard then fail-closes on any mismatch)."""
    try:
        decoded = json.loads(base64.b64decode(sig))
        inner = decoded.get("payload") if isinstance(decoded, dict) else None
        terms = inner.get("terms") if isinstance(inner, dict) else None
        payer = terms.get("payer") if isinstance(terms, dict) else None
        return payer.lower() if isinstance(payer, str) and payer else None
    except Exception:
        return None


def _require_payer(order: Order, payer: str | None) -> None:
    """Fail-closed payer binding: the replaying request's recovered signer MUST equal
    the payer recorded on the order at first settle (both lowercased, checksum-
    agnostic). Any mismatch -> 403 BEFORE any delivery, so a non-signer replaying a
    public terms-signature gets goods refused, not handed over."""
    if (order.from_addr or "").lower() != (payer or "").lower():
        raise HTTPException(403, "subscription payer mismatch")


async def _verify_and_settle(ctx: dict, sig: str) -> JSONResponse:
    """With a PAYMENT-SIGNATURE: rebuild the requirements SERVER-SIDE from ctx (so
    verify/settle bind the buyer's signature to the STORE's payTo/amount/period,
    never a buyer-supplied copy), run the sidecar's LOCAL verify, and only on
    localVerify.ok is True settle through the facilitator. A replayed signature
    returns the existing order (no re-settle). localVerify not ok -> 402 (no Order);
    a facilitator settle failure / 503 -> the same status, NO Order; only a
    facilitator success creates the Order + delivers."""
    idem_key = _subscription_idem_key(sig)
    payer = _recovered_payer(sig)
    replay = await asyncio.to_thread(_subscription_replay, ctx, idem_key, payer)
    if replay is not None:
        return JSONResponse(replay, headers=_SUB_HEADERS)

    cresp = await _sidecar_post("/subscriptions/challenge", json=_challenge_req(ctx))
    accepts = _safe_json(cresp).get("accepts")
    if not isinstance(accepts, list) or not accepts or not isinstance(accepts[0], dict):
        raise HTTPException(502, "subscription challenge returned no requirements")
    requirements = accepts[0]
    sig_headers = {"PAYMENT-SIGNATURE": sig}

    vresp = await _sidecar_post(
        "/subscriptions/verify",
        json={"requirements": requirements},
        headers=sig_headers,
    )
    if vresp.status_code != 200:
        raise HTTPException(402, "subscription verify rejected")
    local_verify = _safe_json(vresp).get("localVerify")
    if not isinstance(local_verify, dict) or local_verify.get("ok") is not True:
        raise HTTPException(402, "subscription verify rejected")

    sresp = await _sidecar_post(
        "/subscriptions/settle",
        json={"requirements": requirements},
        headers=sig_headers,
    )
    if sresp.status_code != 200 or not _safe_json(sresp).get("settled"):
        # Settle failure or the sidecar's own 503 delivers NOTHING.
        raise HTTPException(
            sresp.status_code if sresp.status_code >= 400 else 502,
            "subscription settle failed",
        )
    reference = _safe_json(sresp).get("facilitator")
    body_out = await asyncio.to_thread(
        _fulfill_subscription, ctx, reference, idem_key, payer
    )
    return JSONResponse(body_out, headers=_SUB_HEADERS)


def _subscription_body(session, order: Order, ctx: dict) -> dict:
    """Build the subscribe response body for an order, delivering through the exact
    M3/M4 ``checkout.deliver`` path (idempotent, so it is identical on a replay)."""
    product = session.get(Product, ctx["product_id"])
    delivery_row = checkout.deliver(session, order)
    body = {
        "order_id": order.id,
        "product": product.name if product else None,
        "subscription": True,
        "amount_micro": ctx["amount_per_period_micro"],
        "period_sec": ctx["period_sec"],
        "kind": delivery_row.kind if delivery_row else "text",
        "delivery": delivery_row.payload if delivery_row else checkout.DEFAULT_DELIVERY,
    }
    agentic._augment_agent_gated(session, order, body)
    return body


def _subscription_replay(ctx: dict, idem_key: str, payer: str | None) -> dict | None:
    """Return the existing order's body for a replayed PAYMENT-SIGNATURE (keyed on
    the terms signature), or None on a first submission. Prevents a replay from
    re-hitting the facilitator and minting a duplicate order + delivery."""
    with SessionLocal() as session:
        # Scope the replay lookup to this store: a terms-signature bound to store A
        # must never replay against store B and hand over another store's goods.
        order = session.scalar(
            select(Order).where(
                Order.x402_nonce == idem_key,
                Order.store_id == ctx["store_id"],
            )
        )
        if order is None:
            return None
        # Same-store payer binding: refuse to deliver unless the replaying request's
        # recovered signer equals the payer recorded at first settle. Checked BEFORE
        # _subscription_body (which delivers), so a mismatch delivers nothing.
        _require_payer(order, payer)
        body = _subscription_body(session, order, ctx)
        session.commit()
        return body


def _fulfill_subscription(
    ctx: dict, reference, idem_key: str, payer: str | None
) -> dict:
    """Record the settled subscription as an agent Order and deliver through the
    exact M3/M4 ``checkout.deliver`` path. Runs ONLY after a real facilitator settle
    success — never on a mocked/failed settle. The terms-signature idempotency key
    is stored on ``x402_nonce`` (unique), so a settle that raced a replay reconciles
    to the winner instead of minting a duplicate."""
    with SessionLocal() as session:
        order = Order(
            id=uuid.uuid4().hex[:16],
            store_id=ctx["store_id"],
            product_id=ctx["product_id"],
            pay_to=ctx["pay_to"],
            amount_micro=ctx["amount_per_period_micro"],
            expected_micro=ctx["amount_per_period_micro"],
            status="confirmed",
            channel="agent",
            x402_nonce=idem_key,
            from_addr=payer,
            paid_at=checkout._now(),
        )
        session.add(order)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(Order).where(
                    Order.x402_nonce == idem_key,
                    Order.store_id == ctx["store_id"],
                )
            )
            if existing is None:
                raise
            _require_payer(existing, payer)
            body = _subscription_body(session, existing, ctx)
            session.commit()
            return body
        log_event(
            session,
            "subscriptions",
            "subscription.settled",
            store_id=ctx["store_id"],
            order_id=order.id,
            data={"reference": reference} if isinstance(reference, dict) else None,
        )
        body = _subscription_body(session, order, ctx)
        session.commit()
        return body


@router.post("/s/{slug}/subscribe")
@limiter.limit("30/minute")
async def subscribe(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    if not config.SUBSCRIPTIONS_ENABLED:
        raise HTTPException(503, "subscriptions are not configured")
    ctx = await asyncio.to_thread(_subscription_ctx, slug)
    sig = request.headers.get("PAYMENT-SIGNATURE")
    if not sig:
        return await _serve_challenge(ctx)
    return await _verify_and_settle(ctx, sig)
