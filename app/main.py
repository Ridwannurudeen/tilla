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
from pydantic import BaseModel, Field, ValidationError, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app import chain, checkout, config
from app.checkout import DEFAULT_DELIVERY
from app.db import get_session
from app.engine import create_store as gen_store
from app.engine import resume_pending
from app.models import Delivery, Order, Product, Store, log_event
from app.screening import ScreeningBlocked

logger = logging.getLogger("tilla")

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Retry any stores held in pending_screening from before this process
    # started, so a restart flips recovered ones live instead of stranding them.
    resume_pending()
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
    can't be buffered past the limit before we notice."""
    if request.method in ("POST", "PUT", "PATCH"):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                return JSONResponse(
                    {"detail": "invalid content-length header"}, status_code=400
                )
            if declared > config.MAX_BODY_BYTES:
                return JSONResponse(
                    {"detail": "request body too large"}, status_code=413
                )
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
        delivery = session.scalar(select(Delivery).where(Delivery.order_id == order.id))
        out["delivery"] = delivery.payload if delivery else DEFAULT_DELIVERY
    return out


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
    session.commit()
    session.expire(order)
    return _order_response(session, order)


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
