#!/usr/bin/env python3
"""Tilla backend.
- ASP endpoint: POST/GET /create-store  (x402-gated; an agent pays Tilla to spin up a live store)
- Store checkout: /api/checkout/*  (buyer pays the merchant in USDT on X Layer; balanceOf verification)
Run: uvicorn app.main:app --host 127.0.0.1 --port 8040   (EnvironmentFile=/opt/tilla/.env)
"""

import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, HTTPException, Path, Request
from pydantic import BaseModel, Field, ValidationError, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from app import config
from app.engine import create_store as gen_store
from app.engine import resume_pending
from app.screening import ScreeningBlocked

logger = logging.getLogger("tilla")

RPC = os.environ.get("TILLA_RPC", "https://rpc.xlayer.tech")
USDT = "0x779ded0c9e1022225f8e0630b35a9b54be713736"  # USDT0 on X Layer, 6dp
STORES = config.STORES_DIR
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Retry any stores held in pending_screening from before this process
    # started, so a restart flips recovered ones live instead of stranding them.
    resume_pending()
    yield


app = FastAPI(
    title="Tilla",
    description="Storefronts + crypto checkout on X Layer",
    lifespan=lifespan,
)
CHECKOUTS: dict = {}

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


def balance_of(addr: str) -> float:
    data = "0x70a08231" + "0" * 24 + addr.lower().replace("0x", "")
    body = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": USDT, "data": data}, "latest"],
        "id": 1,
    }
    res = requests.post(RPC, json=body, timeout=12).json().get("result")
    return int(res, 16) / 1e6 if res and res != "0x" else 0.0


def load_store(slug: str) -> dict:
    p = STORES / slug / "store.json"
    if not p.exists():
        raise HTTPException(404, "store not found")
    return json.loads(p.read_text(encoding="utf-8"))


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


# ---------- store checkout: buyer pays the merchant ----------
@app.post("/api/checkout/{slug}")
@limiter.limit("20/minute")
def create_checkout(
    request: Request, slug: str = Path(..., pattern=config.SLUG_PATTERN)
):
    s = load_store(slug)
    if s.get("status") == "pending_screening":
        raise HTTPException(409, "store is not yet live (pending content screening)")
    baseline = balance_of(s["pay_to"])
    cid = uuid.uuid4().hex[:16]
    CHECKOUTS[cid] = {
        "slug": slug,
        "pay_to": s["pay_to"],
        "amount": float(s["amount_usdt"]),
        "baseline": baseline,
        "status": "pending",
        "created": time.time(),
    }
    return {
        "id": cid,
        "pay_to": s["pay_to"],
        "amount": s["amount_usdt"],
        "network": "X Layer (chainId 196)",
        "token": "USDT",
    }


@app.get("/api/checkout/{cid}")
@limiter.limit("40/minute")
def checkout_status(request: Request, cid: str):
    c = CHECKOUTS.get(cid)
    if not c:
        raise HTTPException(404, "checkout not found")
    if c["status"] != "paid":
        bal = balance_of(c["pay_to"])
        if bal >= c["baseline"] + c["amount"] - 1e-6:
            c["status"] = "paid"
            c["paid_at"] = time.time()
    out = {"id": cid, "status": c["status"], "amount": c["amount"]}
    if c["status"] == "paid":
        out["delivery"] = load_store(c["slug"]).get(
            "delivery", "Payment received — thank you!"
        )
    return out


# Test-only checkout confirmation shim. Registered ONLY when TILLA_TEST=1 so the
# route cannot exist in production at all, not merely be gated at call time.
if os.environ.get("TILLA_TEST") == "1":

    @app.post("/api/_test/mark/{cid}")
    def _test_mark(cid: str):
        c = CHECKOUTS.get(cid)
        if not c:
            raise HTTPException(404, "no checkout")
        c["baseline"] = balance_of(c["pay_to"]) - c["amount"] - 0.001
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
