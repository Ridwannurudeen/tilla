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
import logging
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Path, Request
from sqlalchemy import select
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


async def _serve_challenge(ctx: dict) -> JSONResponse:
    """No PAYMENT-SIGNATURE yet: ask the sidecar to build the period 402 challenge
    from the product's pricing_params and relay its body + APP-PAYMENT-REQUIRED
    header verbatim."""
    challenge_req = {
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
    resp = await _sidecar_post("/subscriptions/challenge", json=challenge_req)
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


async def _verify_and_settle(request: Request, ctx: dict, sig: str) -> JSONResponse:
    """With a PAYMENT-SIGNATURE: run the sidecar's LOCAL verify, and only on
    localVerify.ok settle through the facilitator. localVerify false -> 402 (no
    Order); a facilitator settle failure / 503 -> the same status, NO Order; only a
    facilitator success creates the Order + delivers."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    requirements = body.get("requirements") if isinstance(body, dict) else None
    sig_headers = {"PAYMENT-SIGNATURE": sig}

    verify_body = {"requirements": requirements} if requirements else {}
    vresp = await _sidecar_post(
        "/subscriptions/verify", json=verify_body, headers=sig_headers
    )
    if vresp.status_code != 200:
        raise HTTPException(402, "subscription verify rejected")
    local_verify = _safe_json(vresp).get("localVerify")
    if not isinstance(local_verify, dict) or local_verify.get("ok") is False:
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
    body_out = await asyncio.to_thread(_fulfill_subscription, ctx, reference)
    return JSONResponse(body_out, headers=_SUB_HEADERS)


def _fulfill_subscription(ctx: dict, reference) -> dict:
    """Record the settled subscription as an agent Order and deliver through the
    exact M3/M4 ``checkout.deliver`` path. Runs ONLY after a real facilitator settle
    success — never on a mocked/failed settle."""
    with SessionLocal() as session:
        product = session.get(Product, ctx["product_id"])
        order = Order(
            id=uuid.uuid4().hex[:16],
            store_id=ctx["store_id"],
            product_id=ctx["product_id"],
            pay_to=ctx["pay_to"],
            amount_micro=ctx["amount_per_period_micro"],
            expected_micro=ctx["amount_per_period_micro"],
            status="confirmed",
            channel="agent",
            paid_at=checkout._now(),
        )
        session.add(order)
        session.flush()
        log_event(
            session,
            "subscriptions",
            "subscription.settled",
            store_id=ctx["store_id"],
            order_id=order.id,
            data={"reference": reference} if isinstance(reference, dict) else None,
        )
        delivery_row = checkout.deliver(session, order)
        body = {
            "order_id": order.id,
            "product": product.name if product else None,
            "subscription": True,
            "amount_micro": ctx["amount_per_period_micro"],
            "period_sec": ctx["period_sec"],
            "kind": delivery_row.kind if delivery_row else "text",
            "delivery": delivery_row.payload
            if delivery_row
            else checkout.DEFAULT_DELIVERY,
        }
        agentic._augment_agent_gated(session, order, body)
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
    return await _verify_and_settle(request, ctx, sig)
