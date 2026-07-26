"""M8 subscriptions (x402 ``period`` scheme) — a thin FastAPI proxy in front of the
always-on localhost Node sidecar (the JS SDK is the only implementation of the
period scheme; there is no Python one, so it is NOT wired into the x402 middleware).

DORMANT-SAFE: ``POST /s/{slug}/subscribe`` is always mounted but fail-closes to 503
unless ``config.SUBSCRIPTIONS_ENABLED`` is set. Even then, the sidecar itself only
contacts the OKX facilitator when OKX creds are present, so a real subscribe/charge
needs creds + a USER-funded Permit2 buyer. NEVER a false 402/200: if the sidecar is
unreachable at any step the proxy 503s, and ONLY a facilitator settle carrying a real
on-chain settlement tx hash ever creates an Order + delivers (see
:func:`_settle_tx_hash`) — a settle failure, a rejection served as HTTP 200, or a 503
delivers nothing and is audited as ``subscription.settle_failed``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Path, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.responses import JSONResponse

from app import agentic, chain, checkout, config, payment, webhooks
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


def _payer_from_terms(sig: str) -> str | None:
    """``terms.payer`` from the buyer's decoded PAYMENT-SIGNATURE, lowercased — used
    only to RECORD from_addr at first settle, where the on-chain facilitator settle
    has already bound termsSignature -> terms.payer (EIP-712 SubscriptionTerms
    includes ``payer``), so the recorded value is authentic. This field is NOT an
    authenticator on replay: it is plaintext in a public envelope and anyone can
    resubmit it, so replay never trusts it (goods are gated behind the wallet
    session instead — see ``_subscription_body(include_gated=...)``)."""
    try:
        decoded = json.loads(base64.b64decode(sig))
        inner = decoded.get("payload") if isinstance(decoded, dict) else None
        terms = inner.get("terms") if isinstance(inner, dict) else None
        payer = terms.get("payer") if isinstance(terms, dict) else None
        return payer.lower() if isinstance(payer, str) and payer else None
    except Exception:
        return None


_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _settle_tx_hash(reference) -> str:
    """The on-chain settlement tx hash from the facilitator's settle result, or fail
    closed.

    The OKX facilitator answers HTTP 200 for a REJECTED subscribe, carrying the
    rejection INSIDE the body (observed in prod: ``{"code": 30001, "data": {},
    "error_code": "30001", "error_message": "permit_spender_mismatch", ...}``), so the
    sidecar's ``settled`` flag is not on its own evidence that funds moved. A settlement
    is recognised ONLY by a clean success code AND a real 32-byte tx hash; anything else
    raises, so no Order is created and nothing is delivered."""
    if not isinstance(reference, dict):
        raise HTTPException(502, "subscription settle returned no facilitator result")
    for key in ("code", "error_code"):
        value = reference.get(key)
        if value is not None and str(value) != "0":
            raise HTTPException(402, "subscription settle rejected by the facilitator")
    if reference.get("error_message"):
        raise HTTPException(402, "subscription settle rejected by the facilitator")
    data = reference.get("data")
    tx_hash = data.get("txHash") if isinstance(data, dict) else None
    if not isinstance(tx_hash, str) or not _TX_HASH_RE.match(tx_hash):
        raise HTTPException(502, "subscription settle returned no settlement tx")
    return tx_hash


def _log_settle_failed(ctx: dict, reference, reason: str) -> None:
    """Audit a settle that did NOT deliver, under its OWN event name. A failure must
    never be recorded as ``subscription.settled`` — prod filed two facilitator
    rejections under that name, so the audit log read as though funds had moved."""
    with SessionLocal() as session:
        log_event(
            session,
            "subscriptions",
            "subscription.settle_failed",
            store_id=ctx["store_id"],
            data={
                "reason": reason,
                "reference": reference if isinstance(reference, dict) else None,
            },
        )
        session.commit()


async def _verify_and_settle(ctx: dict, sig: str) -> JSONResponse:
    """With a PAYMENT-SIGNATURE: rebuild the requirements SERVER-SIDE from ctx (so
    verify/settle bind the buyer's signature to the STORE's payTo/amount/period,
    never a buyer-supplied copy), run the sidecar's LOCAL verify, and only on
    localVerify.ok is True settle through the facilitator. A replayed signature
    returns the existing order (no re-settle). localVerify not ok -> 402 (no Order);
    a facilitator settle failure / 503 / a 200 whose result carries an error or no
    settlement tx -> NO Order, audited as ``subscription.settle_failed``; only a
    settlement tx hash creates the Order + delivers."""
    idem_key = _subscription_idem_key(sig)
    payer = _payer_from_terms(sig)
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
    sbody = _safe_json(sresp)
    reference = sbody.get("facilitator")
    try:
        if sresp.status_code != 200 or not sbody.get("settled"):
            # Settle failure or the sidecar's own 503 delivers NOTHING.
            raise HTTPException(
                sresp.status_code if sresp.status_code >= 400 else 502,
                "subscription settle failed",
            )
        tx_hash = _settle_tx_hash(reference)
    except HTTPException as exc:
        await asyncio.to_thread(_log_settle_failed, ctx, reference, exc.detail)
        raise
    body_out = await asyncio.to_thread(
        _fulfill_subscription, ctx, reference, tx_hash, idem_key, payer
    )
    return JSONResponse(body_out, headers=_SUB_HEADERS)


def _subscription_body(session, order: Order, ctx: dict, include_gated: bool) -> dict:
    """Build the subscribe response body, delivering through the exact M3/M4
    ``checkout.deliver`` path (idempotent).

    ``include_gated`` mirrors ``main._order_response``: the FIRST settle is
    authenticated by the on-chain facilitator settle (termsSignature bound to the
    payer), so it may return the gated goods inline. A REPLAY carries only a public,
    re-submittable envelope — it is NOT re-authenticated, so for an entitlement-
    backed deliverable the payload is withheld and replaced by the neutral claim
    message; the real payer retrieves the goods via the wallet-session ``/api/library``
    (personal_sign as ``from_addr``), which an envelope-replayer cannot forge. A
    legacy text order (no entitlement) passes through unchanged."""
    from app.models import Entitlement

    product = session.get(Product, ctx["product_id"])
    delivery_row = checkout.deliver(session, order)
    body = {
        "order_id": order.id,
        "product": product.name if product else None,
        "subscription": True,
        "subscription_id": order.settle_ref,
        "amount_micro": ctx["amount_per_period_micro"],
        "period_sec": ctx["period_sec"],
        "kind": delivery_row.kind if delivery_row else "text",
    }
    ent = session.scalar(select(Entitlement).where(Entitlement.order_id == order.id))
    if include_gated:
        body["delivery"] = (
            delivery_row.payload if delivery_row else checkout.DEFAULT_DELIVERY
        )
        agentic._augment_agent_gated(session, order, body)
    elif ent is not None:
        # Unauthenticated replay + entitlement-backed goods: gate behind the wallet
        # session. Delivery.payload IS the license key / text secret, so never echo
        # it and never mint a download token here.
        from app.main import CLAIM_DELIVERY_MESSAGE

        body["delivery"] = CLAIM_DELIVERY_MESSAGE
        body["claim"] = True
    else:
        body["delivery"] = (
            delivery_row.payload if delivery_row else checkout.DEFAULT_DELIVERY
        )
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
        # A replay is NOT re-authenticated (the envelope is public and re-submittable),
        # so entitlement-backed goods are withheld here — the real payer claims them via
        # the wallet-session /api/library. include_gated=False.
        body = _subscription_body(session, order, ctx, include_gated=False)
        session.commit()
        return body


def _fulfill_subscription(
    ctx: dict, reference, tx_hash: str, idem_key: str, payer: str | None
) -> dict:
    """Record the settled subscription as an agent Order and deliver through the
    exact M3/M4 ``checkout.deliver`` path. Runs ONLY after a facilitator settle whose
    result carried a real settlement tx (``_settle_tx_hash`` already validated it) —
    never on a mocked/failed settle. The terms-signature idempotency key is stored on
    ``x402_nonce`` (unique), so a settle that raced a replay reconciles to the winner
    instead of minting a duplicate."""
    # The facilitator settle result also carries the on-chain subscription id (needed
    # to cancel). Persist both: subId on settle_ref, the settle tx on tx_hash.
    data = reference.get("data") if isinstance(reference, dict) else None
    sub_id = data.get("subId") if isinstance(data, dict) else None
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
            from_addr=checkout.norm_addr(payer),
            paid_at=checkout._now(),
            # paid_micro is what refunds._amount_due subtracts from. Only the checkout
            # sweeper used to write it, so an agent/subscription order — delivered and
            # genuinely paid — computed due=0 and 409'd "nothing left to refund",
            # making every subscription sale permanently unrefundable.
            paid_micro=ctx["amount_per_period_micro"],
            settle_ref=sub_id if isinstance(sub_id, str) else None,
            tx_hash=tx_hash,
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
            # Lost the settle race to a concurrent request: the winner already
            # delivered. Treat like a replay — withhold gated goods (claim via
            # wallet session). include_gated=False.
            body = _subscription_body(session, existing, ctx, include_gated=False)
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
        # First settle: the on-chain facilitator settle authenticated the payer, so
        # the gated goods may be returned inline. include_gated=True.
        body = _subscription_body(session, order, ctx, include_gated=True)
        # A subscription sale is a sale: fire the same merchant webhooks and queue the
        # same on-chain receipt as every other rail. Without this a merchant's
        # integration never learned a subscriber had paid, and the sale carried no EAS
        # attestation — invisible where every other settled order is evidenced.
        store = session.get(Store, ctx["store_id"])
        if store is not None:
            if config.ATTEST_ENABLED:
                order.attest_status = "pending"
            webhooks.enqueue(session, store.merchant_id, "order.paid", order)
            webhooks.enqueue(session, store.merchant_id, "order.delivered", order)
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


_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Permit2 AllowanceTransfer.allowance(owner, token, spender) selector; returns
# (uint160 amount, uint48 expiration, uint48 nonce) as three 32-byte words.
_PERMIT2_ALLOWANCE_SELECTOR = "0x927da105"


def _permit2_nonce(payer: str, token: str, spender: str, permit2: str) -> int:
    """The buyer's current Permit2 nonce for (payer, token, spender) — the value the
    PermitSingle must carry or the on-chain permit reverts. Reads
    Permit2.allowance(owner, token, spender).nonce (the third returned word).
    Raises 503 on an RPC failure (fail-closed: never guess a nonce)."""
    data = (
        _PERMIT2_ALLOWANCE_SELECTOR
        + chain.pad_address(payer)[2:]
        + chain.pad_address(token)[2:]
        + chain.pad_address(spender)[2:]
    )
    try:
        result = chain.eth_call(payment.CANONICAL_CHAIN, permit2, data)
    except (chain.ChainError, httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "could not read the Permit2 nonce") from exc
    raw = (result or "").removeprefix("0x")
    if len(raw) < 192:
        raise HTTPException(502, "unexpected Permit2 allowance response")
    return int(raw[128:192], 16)


def _selected_addrs(selected: dict) -> tuple[str, str, str]:
    """(token, subscription-contract/spender, permit2-contract) from the sidecar's
    server-owned challenge — never buyer-supplied, so terms bind to the store's asset
    and the facilitator's real contracts."""
    extra = selected.get("extra") if isinstance(selected, dict) else None
    contracts = extra.get("contracts") if isinstance(extra, dict) else None
    if not isinstance(contracts, dict):
        raise HTTPException(502, "subscription challenge missing contracts")
    token = selected.get("asset")
    spender = contracts.get("subscription")
    permit2 = contracts.get("permit2")
    if not (token and spender and permit2):
        raise HTTPException(502, "subscription challenge missing asset/contracts")
    return token, spender, permit2


async def _challenge_selected(ctx: dict) -> dict:
    """Fetch the sidecar's period challenge and return its single `accepts[0]`
    requirements (the server-owned subscription terms)."""
    cresp = await _sidecar_post("/subscriptions/challenge", json=_challenge_req(ctx))
    accepts = _safe_json(cresp).get("accepts")
    if not isinstance(accepts, list) or not accepts or not isinstance(accepts[0], dict):
        raise HTTPException(502, "subscription challenge returned no requirements")
    return accepts[0]


@router.post("/s/{slug}/subscribe/prepare")
@limiter.limit("30/minute")
async def subscribe_prepare(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    """Browser-signing step 1: return the two EIP-712 envelopes (Permit2 +
    SubscriptionTerms) for the buyer to sign, built from the STORE's server-owned
    terms (never buyer-supplied) with the buyer's current on-chain Permit2 nonce.
    Body: {"payer": "0x..."}."""
    if not config.SUBSCRIPTIONS_ENABLED:
        raise HTTPException(503, "subscriptions are not configured")
    ctx = await asyncio.to_thread(_subscription_ctx, slug)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "invalid JSON body") from None
    payer = (body or {}).get("payer")
    if not isinstance(payer, str) or not _ADDR_RE.match(payer):
        raise HTTPException(422, "payer must be a 0x-prefixed 20-byte EVM address")
    selected = await _challenge_selected(ctx)
    token, spender, permit2 = _selected_addrs(selected)
    nonce = await asyncio.to_thread(_permit2_nonce, payer, token, spender, permit2)
    presp = await _sidecar_post(
        "/subscriptions/prepare",
        json={"selected": selected, "payer": payer, "nonce": nonce},
    )
    if presp.status_code != 200:
        raise HTTPException(502, "subscription prepare failed")
    out = _safe_json(presp)
    out["approval"] = {
        "permit2": permit2,
        "token": token,
        "spender": spender,
        "amount_per_period_micro": ctx["amount_per_period_micro"],
    }
    return JSONResponse(out, headers=_SUB_HEADERS)


@router.post("/s/{slug}/subscribe/encode")
@limiter.limit("30/minute")
async def subscribe_encode(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    """Browser-signing step 2: assemble the base64 PAYMENT-SIGNATURE header from the
    buyer's two signatures, then the client POSTs it back to /s/{slug}/subscribe.
    Body: {permitSingle, permitSingleSignature, terms, termsSignature}."""
    if not config.SUBSCRIPTIONS_ENABLED:
        raise HTTPException(503, "subscriptions are not configured")
    ctx = await asyncio.to_thread(_subscription_ctx, slug)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "invalid JSON body") from None
    fields = ("permitSingle", "permitSingleSignature", "terms", "termsSignature")
    if not isinstance(body, dict) or any(not body.get(f) for f in fields):
        raise HTTPException(422, f"required: {', '.join(fields)}")
    selected = await _challenge_selected(ctx)
    eresp = await _sidecar_post(
        "/subscriptions/encode",
        json={"selected": selected, **{f: body[f] for f in fields}},
    )
    if eresp.status_code != 200:
        raise HTTPException(502, "subscription encode failed")
    payment_signature = _safe_json(eresp).get("paymentSignature")
    if not payment_signature:
        raise HTTPException(502, "subscription encode returned no signature")
    return JSONResponse({"paymentSignature": payment_signature}, headers=_SUB_HEADERS)


_SUBID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


@router.post("/s/{slug}/subscribe/cancel/prepare")
@limiter.limit("30/minute")
async def subscribe_cancel_prepare(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    """Cancel step 1: return the CancelAuth typed data the buyer signs to end their
    subscription. Body: {"subId": "0x..."}. Authenticated by the buyer's signature (only
    the payer can sign a valid CancelAuth), so no store/ownership gate is needed here."""
    if not config.SUBSCRIPTIONS_ENABLED:
        raise HTTPException(503, "subscriptions are not configured")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "invalid JSON body") from None
    sub_id = (body or {}).get("subId")
    if not isinstance(sub_id, str) or not _SUBID_RE.match(sub_id):
        raise HTTPException(422, "subId must be a 0x-prefixed 32-byte hex string")
    presp = await _sidecar_post("/subscriptions/cancel-prepare", json={"subId": sub_id})
    if presp.status_code != 200:
        raise HTTPException(502, "cancel prepare failed")
    return JSONResponse(_safe_json(presp), headers=_SUB_HEADERS)


@router.post("/s/{slug}/subscribe/cancel")
@limiter.limit("30/minute")
async def subscribe_cancel(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    """Cancel step 2: submit the buyer's signed CancelAuth to the facilitator. Body:
    {"subId", "cancelAuth"} (cancelAuth = the cancel-prepare message + "signature").
    A successful cancel stops future charges; the already-delivered first period is
    unaffected. Fail-closed on any facilitator rejection."""
    if not config.SUBSCRIPTIONS_ENABLED:
        raise HTTPException(503, "subscriptions are not configured")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "invalid JSON body") from None
    sub_id = (body or {}).get("subId")
    cancel_auth = (body or {}).get("cancelAuth")
    if not isinstance(sub_id, str) or not _SUBID_RE.match(sub_id):
        raise HTTPException(422, "subId must be a 0x-prefixed 32-byte hex string")
    if not isinstance(cancel_auth, dict) or not cancel_auth.get("signature"):
        raise HTTPException(422, "a signed cancelAuth is required")
    cresp = await _sidecar_post(
        "/subscriptions/cancel", json={"subId": sub_id, "cancelAuth": cancel_auth}
    )
    if cresp.status_code != 200:
        raise HTTPException(402, "subscription cancel failed")
    return JSONResponse(_safe_json(cresp), headers=_SUB_HEADERS)
