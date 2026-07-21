#!/usr/bin/env python3
"""Tilla backend.
- ASP endpoint: POST/GET /create-store  (x402-gated; an agent pays Tilla to spin up a live store)
- Store checkout: /api/checkout/*  (buyer pays the merchant in USDT on X Layer; balanceOf verification)
Run: uvicorn app.main:app --host 127.0.0.1 --port 8040   (EnvironmentFile=/opt/tilla/.env)
"""

import asyncio
import contextlib
import json
import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Path, Request
from itsdangerous import BadSignature, SignatureExpired
from pydantic import BaseModel, Field, ValidationError, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import FileResponse, JSONResponse

from app import chain, checkout, config, delivery
from app.checkout import DEFAULT_DELIVERY
from app.db import get_session
from app.engine import create_store as gen_store
from app.engine import rerender_stores, resume_pending
from app.models import (
    Deliverable,
    Delivery,
    Entitlement,
    Order,
    Product,
    Store,
    log_event,
)
from app.screening import ScreeningBlocked

logger = logging.getLogger("tilla")

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
# The one route exempted from the 64KB body cap (streamed upload, capped at
# config.MAX_UPLOAD_BYTES instead). The route's own Path validator enforces the
# real slug pattern; here we only need to recognise the shape.
_UPLOAD_PATH_RE = re.compile(r"^/api/stores/[^/]+/deliverable$")


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    if config.SWEEP_ENABLED:
        sweeper = asyncio.create_task(checkout.sweeper_loop())
    try:
        yield
    finally:
        if sweeper is not None:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper


app = FastAPI(
    title="Tilla",
    description="Storefronts + crypto checkout on X Layer",
    lifespan=lifespan,
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


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


# ---------- ASP endpoint: create a store (x402-paid) ----------
class CreateStoreBody(BaseModel):
    description: str = Field(min_length=1, max_length=config.MAX_DESCRIPTION_LEN)
    receive_address: str | None = None

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


def _run_create_store(description: str, receive_address: str | None):
    try:
        return gen_store(description, receive_address)
    except ScreeningBlocked as exc:
        logger.warning(
            "store creation blocked by screening: risk_level=%s",
            exc.verdict.get("risk_level"),
        )
        raise HTTPException(422, "content did not pass safety screening") from exc


@app.post("/create-store")
@limiter.limit("6/minute")
def create_store_post(request: Request, body: CreateStoreBody):
    if not os.environ.get("TILLA_LLM_KEY"):
        raise HTTPException(503, "generation unavailable")
    return _run_create_store(body.description, body.receive_address)


@app.get("/create-store")
@limiter.limit("6/minute")
def create_store_get(
    request: Request, description: str = "", receive_address: str = ""
):
    # unpaid GET is intercepted by the x402 paywall (402). A paid GET reaches here.
    if not description:
        return {
            "service": "Tilla · create-store",
            "how": "POST {description, receive_address} (x402-paid) → returns a live store URL",
            "network": "eip155:196",
        }
    try:
        body = CreateStoreBody(description=description, receive_address=receive_address)
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json())) from exc
    return _run_create_store(body.description, body.receive_address)


class TxBody(BaseModel):
    tx_hash: str

    @field_validator("tx_hash")
    @classmethod
    def _validate_tx_hash(cls, v):
        if not _TX_HASH.fullmatch(v or ""):
            raise ValueError("tx_hash must be a 0x-prefixed 32-byte hash")
        return v.lower()


def _order_response(session: Session, order: Order) -> dict:
    # Surface terminal (delivered / legacy paid) as "paid" so every already-
    # rendered store's poll (`d.status === 'paid'`) keeps working unchanged.
    terminal = order.status in checkout.TERMINAL_DELIVERED
    out = {
        "id": order.id,
        "status": "paid" if terminal else order.status,
        "amount": order.expected_micro / 1e6,
        "pay_to": order.pay_to,
    }
    if terminal:
        delivery_row = session.scalar(
            select(Delivery).where(Delivery.order_id == order.id)
        )
        out["delivery"] = delivery_row.payload if delivery_row else DEFAULT_DELIVERY
        _augment_gated(session, order, out)
    return out


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
    if deliverable.kind == "license" and ent.license_key:
        out["license_key"] = ent.license_key
    elif deliverable.kind == "file" and ent.download_count < deliverable.max_downloads:
        with contextlib.suppress(delivery.SigningUnavailable):
            token = delivery.mint_download_token(ent.id, deliverable.id)
            out["download_url"] = delivery.download_url(token)


# ---------- store checkout: buyer pays the merchant ----------
@app.post("/api/checkout/{slug}")
@limiter.limit("20/minute")
def create_checkout(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    store = session.scalar(select(Store).where(Store.slug == slug))
    if store is None:
        raise HTTPException(404, "store not found")
    if store.status == "pending_screening":
        raise HTTPException(409, "store is not yet live (pending content screening)")
    if store.status != "live":
        raise HTTPException(404, "store not found")
    product = session.scalar(
        select(Product)
        .where(Product.store_id == store.id, Product.active.is_(True))
        .order_by(Product.id)
    )
    try:
        order = checkout.create_order(session, store, product)
    except checkout.AmountUnavailable as exc:
        raise HTTPException(503, "checkout busy, retry") from exc
    log_event(session, "api", "order.created", store_id=store.id, order_id=order.id)
    session.commit()
    return {
        "id": order.id,
        "pay_to": store.pay_to,
        "amount": order.expected_micro / 1e6,
        "expires_at": order.expires_at.isoformat(),
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


def _require_store_key(request: Request, store: Store) -> None:
    """Per-store capability auth: Authorization: Bearer <manage_key>, verified as
    sha256 + constant-time compare against stores.manage_key_hash. The single seam
    M9 swaps for merchant-session auth."""
    if not delivery.verify_manage_key(_bearer(request), store.manage_key_hash):
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


def _deactivate_deliverables(session: Session, store_id: int) -> None:
    session.execute(
        update(Deliverable)
        .where(Deliverable.store_id == store_id, Deliverable.active.is_(True))
        .values(active=False)
    )


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
    _require_store_key(request, store)

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
        _deactivate_deliverables(session, store.id)
        deliverable = Deliverable(
            store_id=store.id,
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
            active=True,
        )
    else:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(422, "invalid JSON body") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "JSON body must be an object")
        kind = body.get("kind")
        if kind == "text":
            payload = body.get("payload")
            if not isinstance(payload, str) or not payload.strip():
                raise HTTPException(
                    422, "text deliverable requires a non-empty payload"
                )
            _deactivate_deliverables(session, store.id)
            deliverable = Deliverable(
                store_id=store.id, kind="text", payload=payload, active=True
            )
        elif kind == "license":
            _deactivate_deliverables(session, store.id)
            deliverable = Deliverable(
                store_id=store.id,
                kind="license",
                max_activations=_positive_int(
                    body.get("max_activations"), config.LICENSE_ACTIVATIONS_DEFAULT
                ),
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
    resp = {"id": deliverable.id, "kind": deliverable.kind, "active": True}
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
            "kind": delivery_row.kind if delivery_row else "text",
        }
        _augment_gated(session, order, item)
        purchases.append(item)
    return {"wallet": wallet, "purchases": purchases}


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
    return _order_response(session, order)


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


# ---------- x402 paywall on /create-store (Warden's validated config) ----------
if os.getenv("OKX_API_KEY"):
    import httpx
    from x402.http import OKXAuthConfig, OKXFacilitatorConfig
    from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    from x402.http.types import RouteConfig
    from x402.mechanisms.evm.exact.server import ExactEvmScheme
    from x402.server import x402ResourceServer

    from app.payment import (
        NoRedirectOKXFacilitatorClient,
        build_payment_option,
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
    _route = RouteConfig(
        accepts=[build_payment_option(_rail)],
        description="Tilla — create a live crypto storefront on X Layer",
        mime_type="application/json",
    )
    _paid = {"POST /create-store": _route, "GET /create-store": _route}
    app.add_middleware(PaymentMiddlewareASGI, routes=_paid, server=_srv)
