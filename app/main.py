#!/usr/bin/env python3
"""Tilla backend.
- ASP endpoint: POST/GET /create-store  (x402-gated; an agent pays Tilla to spin up a live store)
- Store checkout: /api/checkout/*  (buyer pays the merchant in USDT on X Layer; balanceOf verification)
Run: uvicorn app.main:app --host 127.0.0.1 --port 8040   (EnvironmentFile=/opt/tilla/.env)
"""

import os
import json
import time
import uuid
import pathlib
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.engine import create_store as gen_store

RPC = os.environ.get("TILLA_RPC", "https://rpc.xlayer.tech")
USDT = "0x779ded0c9e1022225f8e0630b35a9b54be713736"  # USDT0 on X Layer, 6dp
STORES = pathlib.Path(os.environ.get("TILLA_STORES_DIR", "/opt/tilla/stores"))

app = FastAPI(title="Tilla", description="Storefronts + crypto checkout on X Layer")
CHECKOUTS: dict = {}


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
    return json.loads(p.read_text())


@app.get("/health")
def health():
    return {"ok": True, "service": "tilla", "chain": "X Layer (196)"}


# ---------- ASP endpoint: create a store (x402-paid) ----------
class CreateStoreBody(BaseModel):
    description: str
    receive_address: str | None = None


@app.post("/create-store")
def create_store_post(body: CreateStoreBody):
    if not os.environ.get("TILLA_LLM_KEY"):
        raise HTTPException(503, "generation unavailable")
    return gen_store(body.description, body.receive_address)


@app.get("/create-store")
def create_store_get(description: str = "", receive_address: str = ""):
    # unpaid GET is intercepted by the x402 paywall (402). A paid GET reaches here.
    if not description:
        return {
            "service": "Tilla · create-store",
            "how": "POST {description, receive_address} (x402-paid) → returns a live store URL",
            "network": "eip155:196",
        }
    return gen_store(description, receive_address or None)


# ---------- store checkout: buyer pays the merchant ----------
@app.post("/api/checkout/{slug}")
def create_checkout(slug: str):
    s = load_store(slug)
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
def checkout_status(cid: str):
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


@app.post("/api/_test/mark/{cid}")
def _test_mark(cid: str):
    if os.environ.get("TILLA_TEST") != "1":
        raise HTTPException(403, "test mode off")
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
        load_payment_rail,
        build_payment_option,
        NoRedirectOKXFacilitatorClient,
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
