"""M9 merchant platform: wallet-session + API-key auth, the multi-store read
surface, order detail, non-custodial refunds, CSV export, and webhook config.

Merchant identity is the wallet address, reusing the M4 nonce/session machinery
verbatim with a purpose-separated message (delivery.build_merchant_signin_message)
and a distinct token salt (delivery._MERCHANT_SALT). Every merchant route resolves
the caller through the single :func:`resolve_merchant` helper (session token or raw
API key), and every store/order/export query is filtered on ``merchant_id`` — the
one canonical IDOR gate. Non-owned or unknown resources uniformly 404.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from itsdangerous import BadSignature, SignatureExpired
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, StreamingResponse

from app import (
    affiliates,
    chain,
    checkout,
    config,
    delivery,
    domains,
    engine,
    payment,
    refunds,
    render,
    self_serve,
    webhooks,
)
from app.db import SessionLocal, get_session
from app.limiter import limiter
from app.screening import ScreeningBlocked, screen
from app.models import (
    Deliverable,
    EmailSubscriber,
    EventLog,
    Merchant,
    Order,
    Product,
    Refund,
    ScreeningReceipt,
    Store,
    get_or_create_merchant,
    log_event,
)

router = APIRouter()

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

API_KEY_PREFIX = "tilla_sk_"


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


class _MerchantAddressBody(BaseModel):
    address: str

    @field_validator("address")
    @classmethod
    def _v_addr(cls, v):
        if not _EVM_ADDRESS.fullmatch(v or ""):
            raise ValueError("address must be a 0x-prefixed 20-byte EVM address")
        return v.lower()


class MerchantNonceBody(_MerchantAddressBody):
    pass


class MerchantVerifyBody(_MerchantAddressBody):
    signature: str = Field(min_length=1, max_length=300)


# --------------------------------------------------------------- auth resolver
def resolve_merchant(session: Session, bearer: str) -> Merchant | None:
    """Resolve the merchant behind a bearer credential, or None. Two interchangeable
    surfaces: a merchant session token (itsdangerous, merchant salt) → wallet →
    merchant row; else a raw API key looked up by sha256-hex equality against
    merchants.api_key_hash (high-entropy key, hash-equality is the M4 pattern). The
    ONE place credentials are turned into an identity — the IDOR gate builds on it.
    """
    if not bearer:
        return None
    if config.SIGNING_KEY:
        with contextlib.suppress(BadSignature, SignatureExpired):
            wallet = delivery.load_merchant_token(bearer)
            merchant = session.scalar(
                select(Merchant).where(Merchant.wallet_address == wallet)
            )
            if merchant is not None:
                return merchant
    key_hash = hashlib.sha256(bearer.encode()).hexdigest()
    return session.scalar(select(Merchant).where(Merchant.api_key_hash == key_hash))


def _require_merchant(request: Request, session: Session) -> Merchant:
    merchant = resolve_merchant(session, _bearer(request))
    if merchant is None:
        raise HTTPException(401, "merchant authentication required")
    return merchant


# ------------------------------------------------------------------ auth routes
@router.post("/api/merchant/auth/nonce")
@limiter.limit("10/minute")
def merchant_nonce(
    request: Request,
    body: MerchantNonceBody,
    session: Session = Depends(get_session),
):
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    row = delivery.issue_nonce(session, body.address)
    message = delivery.build_merchant_signin_message(
        row.address, row.nonce, row.issued_at.isoformat(), row.expires_at.isoformat()
    )
    session.commit()
    return {
        "nonce": row.nonce,
        "message": message,
        "expires_at": row.expires_at.isoformat(),
    }


@router.post("/api/merchant/auth/verify")
@limiter.limit("10/minute")
def merchant_verify(
    request: Request,
    body: MerchantVerifyBody,
    session: Session = Depends(get_session),
):
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    token = delivery.verify_signin(
        session, body.address, body.signature, purpose="merchant"
    )
    if token is not None:
        # Lazily create the merchants row at first sign-in if create-store did not
        # already make it, so the receive wallet owns any stores it created.
        get_or_create_merchant(session, body.address)
    session.commit()
    if token is None:
        raise HTTPException(401, "signature verification failed")
    return {"session_token": token, "expires_in": config.MERCHANT_SESSION_TTL}


@router.post("/api/merchant/api-key")
@limiter.limit("10/minute")
def merchant_api_key(request: Request, session: Session = Depends(get_session)):
    """Mint (or rotate) the caller's API key. Returns the plaintext exactly once;
    only its sha256 hex is persisted (mirrors hash_manage_key). Re-calling
    overwrites the hash — the previous key is dead."""
    merchant = _require_merchant(request, session)
    plaintext = API_KEY_PREFIX + secrets.token_urlsafe(32)
    merchant.api_key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    log_event(session, "merchant", "api_key.minted", data={"merchant_id": merchant.id})
    session.commit()
    return {"api_key": plaintext}


# ------------------------------------------------------------- money + ownership
def usdt(micro) -> str:
    """Exact 6dp USDT0 display string for an integer micro amount (no float)."""
    micro = int(micro or 0)
    sign = "-" if micro < 0 else ""
    micro = abs(micro)
    return f"{sign}{micro // 1_000_000}.{micro % 1_000_000:06d}"


def fee_usdt(micro) -> str:
    """Trimmed USDT display for round platform fees ("0.05", never "0.050000").
    Order amounts keep the exact 6dp usdt() form — their micro-offsets are
    load-bearing — but the create-store fee is a marketing number and must read
    the same everywhere (www copy, agent card, dashboard)."""
    s = usdt(micro)
    return s.rstrip("0").rstrip(".") if "." in s else s


def _owned_store(session: Session, merchant: Merchant, slug: str) -> Store:
    """The store with `slug` IFF it belongs to `merchant`; 404 otherwise. The one
    IDOR gate for store-scoped routes — a non-owned or unknown slug is
    indistinguishable (uniform 404, no existence oracle)."""
    store = session.scalar(
        select(Store).where(Store.slug == slug, Store.merchant_id == merchant.id)
    )
    if store is None:
        raise HTTPException(404, "store not found")
    return store


def _owned_order(
    session: Session, merchant: Merchant, order_id: str
) -> tuple[Order, Store]:
    """The order and its store IFF the store belongs to `merchant`; 404 otherwise."""
    order = session.get(Order, order_id)
    if order is not None:
        store = session.get(Store, order.store_id)
        if store is not None and store.merchant_id == merchant.id:
            return order, store
    raise HTTPException(404, "order not found")


def _store_stats(session: Session, store_ids: list[int]) -> dict[int, dict]:
    """Per-store {counts_by_status, revenue_micro, refunded_micro} in one grouped
    query over ix_orders_store_status. revenue_micro = SUM(paid_micro - refunded_micro)
    over delivered/paid orders (a full refund flips status out of that set → 0)."""
    stats: dict[int, dict] = {
        sid: {"counts": {}, "revenue_micro": 0, "refunded_micro": 0}
        for sid in store_ids
    }
    if not store_ids:
        return stats
    rows = session.execute(
        select(
            Order.store_id,
            Order.status,
            func.count(),
            func.coalesce(func.sum(Order.paid_micro), 0),
            func.coalesce(func.sum(Order.refunded_micro), 0),
        )
        .where(Order.store_id.in_(store_ids))
        .group_by(Order.store_id, Order.status)
    ).all()
    for store_id, status, count, paid, refunded in rows:
        st = stats[store_id]
        st["counts"][status] = count
        st["refunded_micro"] += int(refunded)
        if status in checkout.TERMINAL_DELIVERED:
            st["revenue_micro"] += int(paid) - int(refunded)
    return stats


# --------------------------------------------------------------- read endpoints
@router.get("/api/merchant/stores")
@limiter.limit("60/minute")
def merchant_stores(request: Request, session: Session = Depends(get_session)):
    """Every store owned by the caller, with order counts by status and net
    revenue. A second store is created simply by calling /create-store again with
    the same receive_address — the merchant_id association is automatic."""
    merchant = _require_merchant(request, session)
    stores = session.scalars(
        select(Store)
        .where(Store.merchant_id == merchant.id)
        .order_by(Store.created_at.desc())
    ).all()
    stats = _store_stats(session, [s.id for s in stores])
    out = []
    for s in stores:
        st = stats[s.id]
        out.append(
            {
                "slug": s.slug,
                "status": s.status,
                "visibility": s.visibility,
                "theme": s.theme,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "order_counts": st["counts"],
                "revenue_micro": st["revenue_micro"],
                "revenue_usdt": usdt(st["revenue_micro"]),
                "refunded_micro": st["refunded_micro"],
            }
        )
    return {"merchant": merchant.wallet_address, "stores": out}


class VisibilityBody(BaseModel):
    visibility: Literal["public", "hidden"]


@router.post("/api/merchant/stores/{slug}/visibility")
@limiter.limit("30/minute")
def merchant_store_visibility(
    request: Request,
    body: VisibilityBody,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Owner toggle for a store's Phase 1.7 discovery visibility. 'hidden' keeps a live
    store out of discovery / the aggregate feed / the sitemap (still reachable by direct
    link, for setup and agent preview); 'public' restores it. A hidden store also
    auto-graduates to public on its first delivered sale. Owning-merchant only (404)."""
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    store.visibility = body.visibility
    session.commit()
    return {"slug": store.slug, "visibility": store.visibility}


class CustomDomainBody(BaseModel):
    domain: str = Field(min_length=1, max_length=253)


def _custom_domain_view(store: Store) -> dict:
    """The owner-facing claim state + the exact DNS TXT record to publish. ``verified``
    is the fail-closed gate host resolution reads; the record is echoed so the merchant
    can copy it verbatim (rendered via textContent in the dashboard, never innerHTML)."""
    domain = store.custom_domain
    token = store.custom_domain_token
    verified = store.custom_domain_verified_at is not None
    record = None
    if domain and token and not verified:
        record = {
            "type": "TXT",
            "name": domains.challenge_name(domain),
            "value": domains.txt_record_value(token),
        }
    return {
        "slug": store.slug,
        "custom_domain": domain,
        "verified": verified,
        "verified_at": (
            store.custom_domain_verified_at.isoformat() if verified else None
        ),
        "dns_record": record,
    }


@router.get("/api/merchant/stores/{slug}/custom-domain")
@limiter.limit("60/minute")
def merchant_get_custom_domain(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """The store's custom-domain claim state and the DNS TXT record to publish (when
    unverified). Owner-gated (404 for a non-owned/unknown slug)."""
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    return _custom_domain_view(store)


@router.post("/api/merchant/stores/{slug}/custom-domain")
@limiter.limit("10/minute")
def merchant_claim_custom_domain(
    request: Request,
    body: CustomDomainBody,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Claim a custom domain for the store. The domain is validated as a strict public
    hostname (422 on anything else), a fresh DNS TXT challenge token is minted, and the
    claim is stored UNVERIFIED — host resolution serves nothing until the owner publishes
    the record and calls verify. One hostname maps to one store: a domain already held by
    another store is 409 (no hijack). Re-claiming (same or new domain) resets the token
    and clears any prior verification. Owner-gated (404)."""
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    try:
        domain = domains.normalize_domain(body.domain)
    except domains.DomainError as exc:
        raise HTTPException(422, str(exc)) from exc
    taken = session.scalar(
        select(Store).where(Store.custom_domain == domain, Store.id != store.id)
    )
    if taken is not None:
        raise HTTPException(409, "domain is already claimed by another store")
    store.custom_domain = domain
    store.custom_domain_token = domains.new_token()
    store.custom_domain_verified_at = None
    log_event(
        session,
        "merchant",
        "custom_domain.claimed",
        store_id=store.id,
        data={"merchant_id": merchant.id, "domain": domain},
    )
    try:
        session.commit()
    except IntegrityError as exc:
        # A concurrent claim of the same domain won the unique index at commit.
        session.rollback()
        raise HTTPException(409, "domain is already claimed by another store") from exc
    return _custom_domain_view(store)


@router.post("/api/merchant/stores/{slug}/custom-domain/verify")
@limiter.limit("10/minute")
def merchant_verify_custom_domain(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Check the DNS TXT challenge for the claimed domain (read-only DNS lookup, no
    connection to the domain). On a match the domain is marked verified and host
    resolution begins serving the store on it; on no match it stays unverified (422,
    fail-closed). Owner-gated (404); 409 when no domain is claimed."""
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    if not store.custom_domain or not store.custom_domain_token:
        raise HTTPException(409, "no custom domain claimed")
    if store.custom_domain_verified_at is not None:
        return _custom_domain_view(store)
    if not domains.verify_domain(store.custom_domain, store.custom_domain_token):
        raise HTTPException(
            422, "TXT challenge record not found yet — add it and retry"
        )
    store.custom_domain_verified_at = datetime.now(timezone.utc).replace(tzinfo=None)
    log_event(
        session,
        "merchant",
        "custom_domain.verified",
        store_id=store.id,
        data={"merchant_id": merchant.id, "domain": store.custom_domain},
    )
    session.commit()
    return _custom_domain_view(store)


@router.delete("/api/merchant/stores/{slug}/custom-domain")
@limiter.limit("10/minute")
def merchant_delete_custom_domain(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Release the store's custom domain (clears the claim, token, and verification, so
    host resolution stops serving it). Idempotent. Owner-gated (404)."""
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    if store.custom_domain is not None:
        store.custom_domain = None
        store.custom_domain_token = None
        store.custom_domain_verified_at = None
        log_event(
            session,
            "merchant",
            "custom_domain.released",
            store_id=store.id,
            data={"merchant_id": merchant.id},
        )
        session.commit()
    return {"slug": store.slug, "custom_domain": None, "verified": False}


class StoreCopyBody(BaseModel):
    """A plain-language copy edit: the storefront tagline and/or the one-line
    description (``hero_subcopy``). Both optional so a merchant can update either;
    the route rejects a fully-empty edit (422)."""

    tagline: str | None = Field(default=None, max_length=120)
    hero_subcopy: str | None = Field(default=None, max_length=280)


def _screen_copy(text: str) -> None:
    """Fail-closed screening for merchant-edited store copy: BLOCK->422, screening
    unavailable->503 (never a silent pass) — the same contract as _screen_name."""
    try:
        outcome = screen(text)
    except ScreeningBlocked as exc:
        raise HTTPException(422, "store copy did not pass safety screening") from exc
    if outcome.status != "allow":
        raise HTTPException(503, "content screening temporarily unavailable")


@router.get("/api/merchant/stores/{slug}/description")
@limiter.limit("60/minute")
def merchant_get_store_copy(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """The store's editable plain-language copy — the tagline and one-line
    description the owner re-words to re-render the storefront (no LLM
    regeneration). Owner-gated (404 for a non-owned/unknown slug)."""
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    content = store.content if isinstance(store.content, dict) else {}
    return {
        "slug": store.slug,
        "tagline": str(content.get("tagline", "")),
        "hero_subcopy": str(content.get("hero_subcopy", "")),
    }


@router.post("/api/merchant/stores/{slug}/description")
@limiter.limit("20/minute")
def merchant_set_store_copy(
    request: Request,
    body: StoreCopyBody,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Owner edit of a store's plain-language copy: the storefront tagline and/or
    the one-line description. The submitted text is content-screened FAIL-CLOSED
    (BLOCK->422, screening unavailable->503) BEFORE any write; only an explicit
    ALLOW updates stores.content and re-renders the static page — the catalog,
    prices, palette, and theme are untouched (no LLM regeneration). Owner-gated
    (404 for a non-owned/unknown slug); a not-yet-live store is 409."""
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    if store.status != "live":
        raise HTTPException(409, "store is not live")
    updates: dict[str, str] = {}
    if body.tagline is not None:
        updates["tagline"] = body.tagline.strip()
    if body.hero_subcopy is not None:
        updates["hero_subcopy"] = body.hero_subcopy.strip()
    if not any(updates.values()):
        raise HTTPException(422, "provide a tagline or one-liner to update")
    _screen_copy("\n".join(v for v in updates.values() if v))
    engine.update_store_copy(session, store, updates)
    log_event(
        session,
        "merchant",
        "store.copy_edited",
        store_id=store.id,
        data={"merchant_id": merchant.id, "fields": sorted(updates)},
    )
    session.commit()
    content = store.content or {}
    return {
        "slug": store.slug,
        "tagline": str(content.get("tagline", "")),
        "hero_subcopy": str(content.get("hero_subcopy", "")),
    }


@router.get("/api/merchant/stores/{slug}/agent-view")
@limiter.limit("30/minute")
def merchant_store_agent_view(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Phase 1.8: show the owner their store EXACTLY as a buyer-agent consumes it — the
    machine endpoints, the feed products (with SLA + x402), the MCP tools, and how it
    currently appears in agent discovery (reputation + trust tier, or not-listed when
    hidden/not-live). Owner-gated (404 otherwise); reuses the same agentic outputs the
    public agent surfaces emit, so the preview can never drift from what agents see."""
    from app import agentic

    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    base = config.PUBLIC_BASE_URL.rstrip("/")
    store_url = f"{base}/s/{store.slug}/"
    products = session.scalars(
        select(Product)
        .where(Product.store_id == store.id, Product.active.is_(True))
        .order_by(Product.id)
    ).all()
    rows = agentic._discovery_rows(session, [Store.slug == store.slug], 1, 0)
    return {
        "slug": store.slug,
        "machine_endpoints": {
            "feed": f"/s/{store.slug}/feed.json",
            "mcp": f"/s/{store.slug}/mcp",
            "buy": f"/s/{store.slug}/buy",
            "llms_txt": f"/s/{store.slug}/llms.txt",
            "agent_card": "/.well-known/agent-card.json",
        },
        "feed_products": [
            agentic._feed_product(store, store.slug, p, store_url) for p in products
        ],
        "mcp_tools": [
            {"name": t["name"], "description": t["description"]}
            for t in agentic._mcp_tools()
        ],
        "discovery": rows[0]
        if rows
        else {
            "listed": False,
            "reason": "hidden" if store.visibility != "public" else "not_live",
        },
    }


@router.get("/api/merchant/summary")
@limiter.limit("60/minute")
def merchant_summary(request: Request, session: Session = Depends(get_session)):
    """Cross-store totals for the caller: net revenue, counts per status, a
    per-product breakdown, the outstanding (un-refunded) overpaid total, and the
    underpaid orders still needing an M9 refund resolution."""
    merchant = _require_merchant(request, session)
    store_ids = session.scalars(
        select(Store.id).where(Store.merchant_id == merchant.id)
    ).all()
    store_ids = list(store_ids)
    stats = _store_stats(session, store_ids)
    counts: dict[str, int] = {}
    revenue_micro = 0
    for st in stats.values():
        revenue_micro += st["revenue_micro"]
        for status, n in st["counts"].items():
            counts[status] = counts.get(status, 0) + n

    slug_by_id = (
        dict(
            session.execute(
                select(Store.id, Store.slug).where(Store.id.in_(store_ids))
            ).all()
        )
        if store_ids
        else {}
    )

    products = []
    if store_ids:
        prod_rows = session.execute(
            select(
                Product.id,
                Product.name,
                Product.store_id,
                func.count(Order.id),
                func.coalesce(func.sum(Order.paid_micro), 0),
                func.coalesce(func.sum(Order.refunded_micro), 0),
            )
            .join(Order, Order.product_id == Product.id)
            .where(
                Product.store_id.in_(store_ids),
                Order.status.in_(checkout.TERMINAL_DELIVERED),
            )
            .group_by(Product.id)
        ).all()
        for pid, name, store_id, n, paid, refunded in prod_rows:
            products.append(
                {
                    "product_id": pid,
                    "name": name,
                    "store_slug": slug_by_id.get(store_id),
                    "orders": n,
                    "revenue_micro": int(paid) - int(refunded),
                    "revenue_usdt": usdt(int(paid) - int(refunded)),
                }
            )

    outstanding_overpaid_micro = 0
    underpaid = []
    if store_ids:
        for o in session.scalars(
            select(Order).where(
                Order.store_id.in_(store_ids),
                Order.status.in_(checkout.TERMINAL_DELIVERED),
                Order.overpaid_micro > 0,
            )
        ):
            outstanding_overpaid_micro += max(
                (o.overpaid_micro or 0) - (o.refunded_micro or 0), 0
            )
        for o in session.scalars(
            select(Order)
            .where(Order.store_id.in_(store_ids), Order.status == "underpaid")
            .order_by(Order.created_at.desc())
        ):
            underpaid.append(
                {
                    "order_id": o.id,
                    "store_slug": slug_by_id.get(o.store_id),
                    "expected_micro": o.expected_micro,
                    "paid_micro": o.paid_micro,
                    "expected_usdt": usdt(o.expected_micro),
                    "paid_usdt": usdt(o.paid_micro),
                    "from_addr": o.from_addr,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                }
            )

    affiliate_owed_micro = affiliates.merchant_owed_micro(session, merchant.id)

    return {
        "merchant": merchant.wallet_address,
        "store_count": len(store_ids),
        "revenue_micro": revenue_micro,
        "revenue_usdt": usdt(revenue_micro),
        "counts": counts,
        "products": products,
        "outstanding_overpaid_micro": outstanding_overpaid_micro,
        "outstanding_overpaid_usdt": usdt(outstanding_overpaid_micro),
        "underpaid": underpaid,
        # M13: outstanding (un-refunded, un-paid) affiliate rev-share the merchant
        # owes referring agents. A number only — no funds move from this figure.
        "affiliate_owed_micro": affiliate_owed_micro,
        "affiliate_owed_usdt": usdt(affiliate_owed_micro),
    }


@router.get("/api/merchant/marketplace")
@limiter.limit("60/minute")
def merchant_marketplace(request: Request, session: Session = Depends(get_session)):
    """Read-only M10 marketplace surface for the caller's stores: listing status,
    per-channel sold counts (web vs agent), and each store's screening receipts
    (demo + paid Warden hires) with an OKLink tx link when a paid receipt settled.
    Behind the same _require_merchant + merchant_id IDOR gate as every M9 route —
    non-owned/unknown stores are simply absent. Strictly read-only: nothing here can
    trigger an on-chain action or a paid scan; listing state changes only via the
    runbook's server-side mark_listed command."""
    merchant = _require_merchant(request, session)
    stores = session.scalars(
        select(Store)
        .where(Store.merchant_id == merchant.id)
        .order_by(Store.created_at.desc())
    ).all()
    store_ids = [s.id for s in stores]

    sold: dict[int, dict[str, int]] = {sid: {"web": 0, "agent": 0} for sid in store_ids}
    receipts: dict[int, list[dict]] = {sid: [] for sid in store_ids}
    if store_ids:
        rows = session.execute(
            select(Order.store_id, Order.channel, func.count())
            .where(
                Order.store_id.in_(store_ids),
                Order.status.in_(checkout.TERMINAL_DELIVERED),
            )
            .group_by(Order.store_id, Order.channel)
        ).all()
        for sid, channel, n in rows:
            bucket = sold.setdefault(sid, {"web": 0, "agent": 0})
            bucket[channel] = bucket.get(channel, 0) + int(n)
        for r in session.scalars(
            select(ScreeningReceipt)
            .where(ScreeningReceipt.store_id.in_(store_ids))
            .order_by(ScreeningReceipt.id.desc())
        ):
            receipts[r.store_id].append(
                {
                    "mode": r.mode,
                    "verdict": r.verdict,
                    "risk_level": r.risk_level,
                    "amount_usdt": usdt(r.amount_micro) if r.amount_micro else None,
                    "tx_hash": r.tx_hash,
                    "tx_url": config.OKLINK_TX_BASE + r.tx_hash if r.tx_hash else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )

    out = []
    for s in stores:
        counts = sold.get(s.id, {"web": 0, "agent": 0})
        out.append(
            {
                "slug": s.slug,
                "marketplace_status": s.marketplace_status,
                "marketplace_listed_at": (
                    s.marketplace_listed_at.isoformat()
                    if s.marketplace_listed_at
                    else None
                ),
                "sold_count": {
                    "web": counts.get("web", 0),
                    "agent": counts.get("agent", 0),
                    "total": counts.get("web", 0) + counts.get("agent", 0),
                },
                "screening": receipts.get(s.id, []),
            }
        )
    return {"merchant": merchant.wallet_address, "stores": out}


def _order_row(order: Order, store: Store) -> dict:
    return {
        "order_id": order.id,
        "store_slug": store.slug,
        "status": order.status,
        "channel": order.channel,
        "amount_micro": order.amount_micro,
        "expected_micro": order.expected_micro,
        "paid_micro": order.paid_micro,
        "overpaid_micro": order.overpaid_micro,
        "refunded_micro": order.refunded_micro,
        "expected_usdt": usdt(order.expected_micro),
        "paid_usdt": usdt(order.paid_micro),
        "refunded_usdt": usdt(order.refunded_micro),
        "from_addr": order.from_addr,
        "tx_hash": order.tx_hash,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
    }


@router.get("/api/merchant/stores/{slug}/orders")
@limiter.limit("60/minute")
def merchant_store_orders(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    status: str | None = Query(None, max_length=20),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    stmt = select(Order).where(Order.store_id == store.id)
    if status:
        stmt = stmt.where(Order.status == status)
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    orders = session.scalars(
        stmt.order_by(Order.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "slug": store.slug,
        "total": total,
        "limit": limit,
        "offset": offset,
        "orders": [_order_row(o, store) for o in orders],
    }


@router.get("/api/merchant/orders/{order_id}")
@limiter.limit("60/minute")
def merchant_order_detail(
    request: Request,
    order_id: str = Path(..., min_length=1, max_length=32),
    session: Session = Depends(get_session),
):
    merchant = _require_merchant(request, session)
    order, store = _owned_order(session, merchant, order_id)
    product = session.get(Product, order.product_id) if order.product_id else None
    detail = _order_row(order, store)
    detail["product_name"] = product.name if product else None
    detail["buyer_email"] = order.buyer_email
    detail["tx_url"] = config.OKLINK_TX_BASE + order.tx_hash if order.tx_hash else None
    # M11 on-chain receipt (additive, NULL until an attestation exists).
    detail["attest_status"] = order.attest_status
    detail["attestation_uid"] = order.attestation_uid
    detail["attestation_tx_url"] = (
        config.OKLINK_TX_BASE + order.attest_tx if order.attest_tx else None
    )
    refunds = session.scalars(
        select(Refund).where(Refund.order_id == order.id).order_by(Refund.id)
    ).all()
    detail["refunds"] = [
        {
            "kind": r.kind,
            "amount_micro": r.amount_micro,
            "amount_usdt": usdt(r.amount_micro),
            "tx_hash": r.tx_hash,
            "tx_url": config.OKLINK_TX_BASE + r.tx_hash,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in refunds
    ]
    timeline = session.scalars(
        select(EventLog).where(EventLog.order_id == order.id).order_by(EventLog.id)
    ).all()
    detail["timeline"] = [
        {
            "source": e.source,
            "event": e.event,
            "ts": e.ts.isoformat() if e.ts else None,
        }
        for e in timeline
    ]
    return detail


class RefundBody(BaseModel):
    tx_hash: str
    kind: Literal["full", "overage"]

    @field_validator("tx_hash")
    @classmethod
    def _v_tx(cls, v):
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", v or ""):
            raise ValueError("tx_hash must be a 0x-prefixed 32-byte hash")
        return v.lower()


@router.post("/api/merchant/orders/{order_id}/refund")
@limiter.limit("10/minute")
def merchant_refund(
    request: Request,
    body: RefundBody,
    order_id: str = Path(..., min_length=1, max_length=32),
    session: Session = Depends(get_session),
):
    """Verify a merchant-sent USDT0 refund on X Layer and apply it (non-custodial:
    Tilla verifies, never sends). Owning-merchant only (404 otherwise); idempotent
    on the refund tx; the amount is server-computed, never trusted from the body."""
    merchant = _require_merchant(request, session)
    order, store = _owned_order(session, merchant, order_id)
    try:
        result = refunds.apply_refund(session, order, store, body.tx_hash, body.kind)
    except refunds.RefundError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except (httpx.HTTPError, chain.ChainError) as exc:
        raise HTTPException(502, "chain verification unavailable") from exc
    try:
        session.commit()
    except IntegrityError:
        # a raced concurrent refund won at commit; reconcile to the committed state
        session.rollback()
        session.refresh(order)
        result = {
            "order_id": order.id,
            "status": order.status,
            "applied_micro": 0,
            "refunded_micro": order.refunded_micro,
        }
    return {
        "order_id": result["order_id"],
        "status": result["status"],
        "kind": body.kind,
        "applied_micro": result["applied_micro"],
        "refunded_micro": result["refunded_micro"],
        "refunded_usdt": usdt(result["refunded_micro"]),
    }


# ------------------------------------------------------------ M13 affiliates
class AffiliatePayoutBody(BaseModel):
    tx_hash: str

    @field_validator("tx_hash")
    @classmethod
    def _v_tx(cls, v):
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", v or ""):
            raise ValueError("tx_hash must be a 0x-prefixed 32-byte hash")
        return v.lower()


@router.get("/api/merchant/affiliates")
@limiter.limit("60/minute")
def merchant_affiliates(request: Request, session: Session = Depends(get_session)):
    """Per-referrer accrued/void/paid totals + per-order rows (with OKLink sale +
    payout links) for the caller's stores. Behind the _require_merchant IDOR gate;
    accruals are scoped to the merchant, so no other merchant's ledger is visible."""
    merchant = _require_merchant(request, session)
    data = affiliates.merchant_summary(session, merchant.id)
    for r in data["referrers"]:
        r["accrued_usdt"] = usdt(r["accrued_micro"])
        r["void_usdt"] = usdt(r["void_micro"])
        r["paid_usdt"] = usdt(r["paid_micro"])
    return {
        "merchant": merchant.wallet_address,
        "referrers": data["referrers"],
        "orders": data["orders"],
        "owed_micro": data["owed_micro"],
        "owed_usdt": usdt(data["owed_micro"]),
    }


@router.post("/api/merchant/affiliates/{address}/payout")
@limiter.limit("10/minute")
def merchant_affiliate_payout(
    request: Request,
    body: AffiliatePayoutBody,
    address: str = Path(..., min_length=42, max_length=42),
    session: Session = Depends(get_session),
):
    """Record a manual on-chain USDT0 payout the merchant/operator sent to a referring
    agent from their OWN wallet. VERIFY-AND-RECORD ONLY: Tilla verifies the transfer
    and flips covered accruals to 'paid' — it constructs no signer and moves no funds
    (byte-identical to the M9 non-custodial refund). Owning-merchant only; idempotent
    on (tx_hash, log_index); the applied amount is capped at the outstanding owed."""
    merchant = _require_merchant(request, session)
    try:
        referrer = affiliates.normalize_ref(address)
    except affiliates.RefRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    if referrer is None:
        raise HTTPException(422, "address is required")
    try:
        result = affiliates.verify_and_record_payout(
            session, merchant.id, referrer, body.tx_hash
        )
    except affiliates.PayoutError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except (httpx.HTTPError, chain.ChainError) as exc:
        raise HTTPException(502, "chain verification unavailable") from exc
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "payout transfer already recorded") from exc
    return {
        "referrer": result["referrer"],
        "recorded_micro": result["recorded_micro"],
        "recorded_usdt": usdt(result["recorded_micro"]),
        "covered_micro": result["covered_micro"],
        "owed_after_micro": result["owed_after_micro"],
        "owed_after_usdt": usdt(result["owed_after_micro"]),
    }


# ------------------------------------------------------- M13 email subscribers
class SubscriberDeleteBody(BaseModel):
    email: str = Field(min_length=3, max_length=255)


@router.get("/api/merchant/stores/{slug}/subscribers")
@limiter.limit("60/minute")
def merchant_store_subscribers(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    """The caller's store subscribers (merchant IDOR gate). Emails appear ONLY here,
    behind auth — never in feeds, discovery, logs, or any unauthenticated body."""
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    stmt = select(EmailSubscriber).where(
        EmailSubscriber.store_id == store.id, EmailSubscriber.removed_at.is_(None)
    )
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.scalars(
        stmt.order_by(EmailSubscriber.id.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "slug": store.slug,
        "total": total,
        "limit": limit,
        "offset": offset,
        "subscribers": [
            {
                "email": s.email,
                "source": s.source,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in rows
        ],
    }


@router.delete("/api/merchant/stores/{slug}/subscribers")
@limiter.limit("20/minute")
def merchant_delete_subscriber(
    request: Request,
    body: SubscriberDeleteBody,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Soft-delete a subscriber (removal request). Idempotent {ok:true} whether or
    not the email was present (no membership oracle)."""
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    email = body.email.replace("\r", "").replace("\n", "").strip().lower()
    row = session.scalar(
        select(EmailSubscriber).where(
            EmailSubscriber.store_id == store.id,
            EmailSubscriber.email == email,
            EmailSubscriber.removed_at.is_(None),
        )
    )
    if row is not None:
        row.removed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        log_event(session, "merchant", "subscriber.removed", store_id=store.id)
        session.commit()
    return {"ok": True}


# ------------------------------------------------------------------ CSV export
_CSV_INJECT = ("=", "+", "-", "@")


def _csv_cell(value) -> str:
    """Formula-injection defense: a cell that Excel/Sheets would treat as a formula
    (leading = + - @) is prefixed with a single quote. Product names are merchant/
    LLM text the merchant opens in a spreadsheet, so this runs on every data cell."""
    s = "" if value is None else str(value)
    if s and s[0] in _CSV_INJECT:
        return "'" + s
    return s


def _iso(dt) -> str:
    return dt.isoformat() if dt else ""


def _parse_iso(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, "from/to must be ISO 8601 timestamps") from exc
    # SQLAlchemy's SQLite DateTime drops tzinfo without converting, so a tz-aware
    # bound would compare as offset-local against naive-UTC rows. Normalize to UTC
    # and strip tzinfo so the export window matches the stored naive-UTC timestamps.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _export_scope(session: Session, merchant: Merchant, store: str | None) -> list[int]:
    """Store ids in scope for an export: one owned store (404 if not owned) or all
    of the caller's stores. The export IDOR gate."""
    if store:
        return [_owned_store(session, merchant, store).id]
    return list(
        session.scalars(select(Store.id).where(Store.merchant_id == merchant.id))
    )


def _csv_response(generator, filename: str) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _orders_csv_gen(store_ids, from_dt, to_dt):
    """Stream orders.csv through its own session (a StreamingResponse body runs
    after the route returns, so it cannot use the request-scoped session)."""
    header = [
        "order_id",
        "store_slug",
        "product_name",
        "status",
        "channel",
        "amount_usdt",
        "paid_usdt",
        "overpaid_usdt",
        "refunded_usdt",
        "tx_hash",
        "from_addr",
        "buyer_email",
        "created_at",
        "paid_at",
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)

    def flush():
        val = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return val

    writer.writerow(header)
    yield flush()
    if not store_ids:
        return
    with SessionLocal() as session:
        slug_by_id = dict(
            session.execute(
                select(Store.id, Store.slug).where(Store.id.in_(store_ids))
            ).all()
        )
        name_by_pid = dict(
            session.execute(
                select(Product.id, Product.name).where(Product.store_id.in_(store_ids))
            ).all()
        )
        stmt = select(Order).where(Order.store_id.in_(store_ids))
        if from_dt is not None:
            stmt = stmt.where(Order.created_at >= from_dt)
        if to_dt is not None:
            stmt = stmt.where(Order.created_at <= to_dt)
        for o in session.scalars(
            stmt.order_by(Order.created_at).execution_options(yield_per=500)
        ):
            writer.writerow(
                _csv_cell(c)
                for c in [
                    o.id,
                    slug_by_id.get(o.store_id),
                    name_by_pid.get(o.product_id),
                    o.status,
                    o.channel,
                    usdt(o.amount_micro),
                    usdt(o.paid_micro),
                    usdt(o.overpaid_micro),
                    usdt(o.refunded_micro),
                    o.tx_hash or "",
                    o.from_addr or "",
                    o.buyer_email or "",
                    _iso(o.created_at),
                    _iso(o.paid_at),
                ]
            )
            yield flush()


def _customers_csv_gen(store_ids):
    header = [
        "from_addr",
        "orders_count",
        "total_paid_usdt",
        "first_purchase",
        "last_purchase",
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)

    def flush():
        val = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return val

    writer.writerow(header)
    yield flush()
    if not store_ids:
        return
    with SessionLocal() as session:
        # Only orders that actually settled. An abandoned or underpaid checkout
        # still records a from_addr, so filtering on from_addr alone exported
        # non-buyers as customers — with the timestamp of a checkout they never
        # paid for reported as their "purchase" date. Same TERMINAL_DELIVERED
        # gate `_store_stats` and `merchant_summary` already use.
        paid_ts = func.coalesce(Order.paid_at, Order.created_at)
        # Group case-insensitively. from_addr is recorded as it arrived, so the
        # same wallet appears both checksummed and lowercased across orders — in
        # production one buyer was split into two "customers" with their order
        # count and revenue divided between the rows. An EVM address is
        # case-insensitive, so lowercase is the one true key here (it is also
        # what the rest of the app stores, e.g. pay_to).
        buyer = func.lower(Order.from_addr)
        rows = session.execute(
            select(
                buyer,
                func.count(),
                func.coalesce(func.sum(Order.paid_micro), 0),
                func.min(paid_ts),
                func.max(paid_ts),
            )
            .where(
                Order.store_id.in_(store_ids),
                Order.from_addr.isnot(None),
                Order.status.in_(checkout.TERMINAL_DELIVERED),
            )
            .group_by(buyer)
            .order_by(func.max(paid_ts).desc())
        ).all()
        for addr, count, paid, first, last in rows:
            writer.writerow(
                _csv_cell(c) for c in [addr, count, usdt(paid), _iso(first), _iso(last)]
            )
            yield flush()


@router.get("/api/merchant/export/orders.csv")
@limiter.limit("20/minute")
def export_orders_csv(
    request: Request,
    store: str | None = Query(None, max_length=config.SLUG_MAX_LEN),
    from_: str | None = Query(None, alias="from", max_length=40),
    to: str | None = Query(None, max_length=40),
    session: Session = Depends(get_session),
):
    merchant = _require_merchant(request, session)
    store_ids = _export_scope(session, merchant, store)
    from_dt, to_dt = _parse_iso(from_), _parse_iso(to)
    return _csv_response(_orders_csv_gen(store_ids, from_dt, to_dt), "orders.csv")


@router.get("/api/merchant/export/customers.csv")
@limiter.limit("20/minute")
def export_customers_csv(
    request: Request,
    store: str | None = Query(None, max_length=config.SLUG_MAX_LEN),
    session: Session = Depends(get_session),
):
    merchant = _require_merchant(request, session)
    store_ids = _export_scope(session, merchant, store)
    return _csv_response(_customers_csv_gen(store_ids), "customers.csv")


def _subscribers_csv_gen(store_ids):
    header = ["store_slug", "email", "source", "created_at"]
    buf = io.StringIO()
    writer = csv.writer(buf)

    def flush():
        val = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return val

    writer.writerow(header)
    yield flush()
    if not store_ids:
        return
    with SessionLocal() as session:
        slug_by_id = dict(
            session.execute(
                select(Store.id, Store.slug).where(Store.id.in_(store_ids))
            ).all()
        )
        for s in session.scalars(
            select(EmailSubscriber)
            .where(
                EmailSubscriber.store_id.in_(store_ids),
                EmailSubscriber.removed_at.is_(None),
            )
            .order_by(EmailSubscriber.created_at)
            .execution_options(yield_per=500)
        ):
            writer.writerow(
                _csv_cell(c)
                for c in [
                    slug_by_id.get(s.store_id),
                    s.email,
                    s.source,
                    _iso(s.created_at),
                ]
            )
            yield flush()


@router.get("/api/merchant/export/subscribers.csv")
@limiter.limit("20/minute")
def export_subscribers_csv(
    request: Request,
    store: str | None = Query(None, max_length=config.SLUG_MAX_LEN),
    session: Session = Depends(get_session),
):
    merchant = _require_merchant(request, session)
    store_ids = _export_scope(session, merchant, store)
    return _csv_response(_subscribers_csv_gen(store_ids), "subscribers.csv")


# ------------------------------------------------------------------- webhooks
class WebhookBody(BaseModel):
    url: str = Field(min_length=1, max_length=500)


@router.get("/api/merchant/webhook")
@limiter.limit("60/minute")
def get_webhook(request: Request, session: Session = Depends(get_session)):
    """The caller's registered webhook URL (never the secret — it is write-only,
    shown once at registration and only used server-side to sign deliveries)."""
    merchant = _require_merchant(request, session)
    return {"url": merchant.webhook_url}


@router.post("/api/merchant/webhook")
@limiter.limit("10/minute")
def set_webhook(
    request: Request,
    body: WebhookBody,
    session: Session = Depends(get_session),
):
    """Register (or rotate) the webhook. SSRF-validates the URL, mints a signing
    secret, returns it exactly once. POSTing again rotates the secret."""
    merchant = _require_merchant(request, session)
    try:
        url = webhooks.validate_webhook_url(body.url)
    except webhooks.WebhookURLError as exc:
        raise HTTPException(422, str(exc)) from exc
    secret = "whsec_" + secrets.token_urlsafe(32)
    merchant.webhook_url = url
    merchant.webhook_secret = secret
    log_event(
        session, "merchant", "webhook.registered", data={"merchant_id": merchant.id}
    )
    session.commit()
    return {"url": url, "secret": secret}


@router.delete("/api/merchant/webhook")
@limiter.limit("10/minute")
def delete_webhook(request: Request, session: Session = Depends(get_session)):
    merchant = _require_merchant(request, session)
    merchant.webhook_url = None
    merchant.webhook_secret = None
    log_event(session, "merchant", "webhook.removed", data={"merchant_id": merchant.id})
    session.commit()
    return {"url": None}


# ---------------------------------------------------- merchant product catalog
class ProductAddBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    price_usdt: float = Field(ge=0.01, le=10000)
    blurb: str = Field(default="", max_length=400)
    cta_text: str = Field(default="Buy now", max_length=40)


class ProductPatchBody(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    price_usdt: float | None = Field(default=None, ge=0.01, le=10000)
    blurb: str | None = Field(default=None, max_length=400)
    cta_text: str | None = Field(default=None, max_length=40)
    active: bool | None = None


def _screen_name(name: str) -> None:
    """Fail-closed name screening for a merchant-supplied product name: BLOCK->422,
    screening unavailable->503 (never a silent pass)."""
    try:
        outcome = screen(name)
    except ScreeningBlocked as exc:
        raise HTTPException(422, "product name did not pass safety screening") from exc
    if outcome.status != "allow":
        raise HTTPException(503, "content screening temporarily unavailable")


@router.get("/api/merchant/stores/{slug}/products")
@limiter.limit("30/minute")
def merchant_list_products(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    rows = session.scalars(
        select(Product).where(Product.store_id == store.id).order_by(Product.id)
    ).all()
    return {
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "price_usdt": p.price_micro / 1e6,
                "active": p.active,
                "pricing_model": p.pricing_model or "one_time",
                "pricing_params": (
                    p.pricing_params if isinstance(p.pricing_params, dict) else {}
                ),
            }
            for p in rows
        ]
    }


@router.post("/api/merchant/stores/{slug}/products")
@limiter.limit("20/minute")
def merchant_add_product(
    request: Request,
    body: ProductAddBody,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    if store.status != "live":
        raise HTTPException(409, "store is not live")
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name cannot be empty")
    _screen_name(name)
    product = Product(
        store_id=store.id,
        name=name,
        price_micro=int(round(body.price_usdt * 1e6)),
        active=True,
    )
    session.add(product)
    session.flush()
    engine.resync_catalog(
        session,
        store,
        extras_override={product.id: (body.blurb.strip(), body.cta_text)},
    )
    log_event(
        session,
        "merchant",
        "product.added",
        store_id=store.id,
        data={"merchant_id": merchant.id, "product_id": product.id},
    )
    session.commit()
    return {
        "id": product.id,
        "name": name,
        "price_usdt": body.price_usdt,
        "active": True,
    }


@router.patch("/api/merchant/stores/{slug}/products/{pid}")
@limiter.limit("20/minute")
def merchant_edit_product(
    request: Request,
    body: ProductPatchBody,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    pid: int = Path(..., ge=1),
    session: Session = Depends(get_session),
):
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    product = session.scalar(
        select(Product).where(Product.id == pid, Product.store_id == store.id)
    )
    if product is None:
        raise HTTPException(404, "product not found")
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "name cannot be empty")
        _screen_name(name)
        product.name = name
    if body.price_usdt is not None:
        product.price_micro = int(round(body.price_usdt * 1e6))
    if body.active is not None and body.active != product.active:
        if not body.active:
            # never leave a live store with zero active products — checkout would
            # 409 and the storefront would render an empty catalog.
            active_count = session.scalar(
                select(func.count())
                .select_from(Product)
                .where(Product.store_id == store.id, Product.active.is_(True))
            )
            if active_count <= 1:
                raise HTTPException(409, "cannot deactivate the last active product")
        product.active = body.active
    # blurb/cta live only in content; override just the provided field, keeping the
    # other from the current catalog entry.
    extras_override = None
    if body.blurb is not None or body.cta_text is not None:
        cur = next(
            (
                it
                for it in (store.content or {}).get("products", [])
                if isinstance(it, dict) and it.get("id") == product.id
            ),
            {},
        )
        blurb = body.blurb if body.blurb is not None else str(cur.get("blurb", ""))
        cta = (
            body.cta_text
            if body.cta_text is not None
            else str(cur.get("cta_text", "Buy now"))
        )
        extras_override = {product.id: (blurb.strip(), cta or "Buy now")}
    engine.resync_catalog(session, store, extras_override=extras_override)
    log_event(
        session,
        "merchant",
        "product.edited",
        store_id=store.id,
        data={"merchant_id": merchant.id, "product_id": product.id},
    )
    session.commit()
    return {
        "id": product.id,
        "name": product.name,
        "price_usdt": product.price_micro / 1e6,
        "active": product.active,
    }


@router.get("/api/merchant/stores/{slug}/deliverables")
@limiter.limit("30/minute")
def merchant_list_deliverables(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """The store's active deliverables as METADATA only — never the text secret
    (`payload`) or file bytes. product_id NULL is the store-level default; a set
    product_id is that product's deliverable. Lets the dashboard show what a buyer
    receives per product and which products still fall back to the default."""
    merchant = _require_merchant(request, session)
    store = _owned_store(session, merchant, slug)
    rows = session.scalars(
        select(Deliverable)
        .where(Deliverable.store_id == store.id, Deliverable.active.is_(True))
        .order_by(Deliverable.id)
    ).all()
    return {
        "deliverables": [
            {
                "id": d.id,
                "product_id": d.product_id,
                "kind": d.kind,
                "file_name": d.file_name,
                "max_downloads": d.max_downloads,
                "max_activations": d.max_activations,
            }
            for d in rows
        ]
    }


# ------------------------------------------------------ self-serve create-store
class SelfServeCreateBody(BaseModel):
    description: str = Field(min_length=1, max_length=config.MAX_DESCRIPTION_LEN)
    theme: str | None = None

    @field_validator("theme")
    @classmethod
    def _validate_theme(cls, v):
        if v is None or v == "":
            return None
        if v not in ("original", "bold", "editorial"):
            raise ValueError("theme must be one of: original, bold, editorial")
        return v


class SelfServePayBody(BaseModel):
    tx_hash: str = Field(min_length=66, max_length=66)


def _creation_result(creation) -> dict:
    return {
        "id": creation.id,
        "status": creation.status,
        "slug": creation.slug,
        "url": f"/s/{creation.slug}/" if creation.slug else None,
        "amount_micro": creation.expected_micro,
        "amount_usdt": fee_usdt(creation.expected_micro),
        "pay_to": creation.pay_to,
    }


@router.get("/api/merchant/create-store/pending")
@limiter.limit("60/minute")
def merchant_create_store_pending(
    request: Request, session: Session = Depends(get_session)
):
    """The caller's still-actionable creations (newest first) plus the current
    create-store fee. The dashboard is in-memory only, so without this a reload
    stranded a pending — possibly already PAID — creation and the merchant started
    over and paid twice. Behind the same _require_merchant gate as every other
    merchant route, and scoped to the caller's own wallet: another merchant's
    creations are never listed. The fee comes from the same PAYMENT_AMOUNT constant
    the intent charges, so the panel's price copy can never drift from the real
    fee."""
    merchant = _require_merchant(request, session)
    rows = self_serve.list_open(session, merchant.wallet_address)
    return {
        "fee_micro": int(payment.PAYMENT_AMOUNT),
        "fee_usdt": fee_usdt(int(payment.PAYMENT_AMOUNT)),
        "creations": [
            {
                **_creation_result(row),
                "description": row.description,
                "theme": row.theme,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
    }


@router.post("/api/merchant/create-store")
@limiter.limit("6/minute")
def merchant_create_store(
    request: Request,
    body: SelfServeCreateBody,
    session: Session = Depends(get_session),
):
    """Screen the description and open a paid create-store intent for the signed-in
    merchant (their wallet becomes the store's receive address). Fail fast if
    generation is unconfigured, so no payment is offered against a dead generator."""
    if not os.environ.get("TILLA_LLM_KEY"):
        raise HTTPException(503, "store generation unavailable")
    merchant = _require_merchant(request, session)
    try:
        creation = self_serve.create_intent(
            session, merchant.wallet_address, body.description.strip(), body.theme
        )
    except self_serve.CreationError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    session.commit()
    return _creation_result(creation)


@router.post("/api/merchant/create-store/{creation_id}/pay")
@limiter.limit("6/minute")
def merchant_create_store_pay(
    request: Request,
    body: SelfServePayBody,
    creation_id: int = Path(..., ge=1),
    session: Session = Depends(get_session),
):
    """Verify the merchant's on-chain create-store fee payment and generate the
    store. Owner-scoped (a foreign id is 404). Safe to call repeatedly with the SAME
    hash — that is how the dashboard recovers a payment it could not confirm on the
    first POST. A reused payment is 409, a bad tx is 400."""
    merchant = _require_merchant(request, session)
    creation = self_serve.get_creation(session, creation_id, merchant.wallet_address)
    if creation is None:
        raise HTTPException(404, "creation not found")
    try:
        self_serve.pay_and_create(session, creation, body.tx_hash)
    except self_serve.CreationError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    except (httpx.HTTPError, chain.ChainError) as exc:
        raise HTTPException(502, "chain verification unavailable") from exc
    session.commit()
    return _creation_result(creation)


@router.post("/api/merchant/create-store/{creation_id}/retry")
@limiter.limit("6/minute")
def merchant_create_store_retry(
    request: Request,
    creation_id: int = Path(..., ge=1),
    session: Session = Depends(get_session),
):
    """Regenerate a paid-but-ungenerated creation — after a model outage ('paid') or
    after the generated content was blocked ('failed'). No re-charge: the payment
    already verified, and both states are only reachable through it."""
    merchant = _require_merchant(request, session)
    creation = self_serve.get_creation(session, creation_id, merchant.wallet_address)
    if creation is None:
        raise HTTPException(404, "creation not found")
    try:
        self_serve.retry(session, creation)
    except self_serve.CreationError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    session.commit()
    return _creation_result(creation)


@router.get("/dashboard")
def dashboard_shell():
    """The merchant dashboard SPA shell. Autoescaped, carries NO merchant data:
    its inline JS does connect → nonce → personal_sign → verify (token kept in
    memory only) and renders every API string via textContent, so hostile
    store/product/order text can never become markup."""
    return HTMLResponse(render.render_shell("_dashboard.html"))
