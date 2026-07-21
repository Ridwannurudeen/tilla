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
import re
import secrets
from datetime import datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from itsdangerous import BadSignature, SignatureExpired
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, StreamingResponse

from app import chain, checkout, config, delivery, refunds, render
from app.db import SessionLocal, get_session
from app.limiter import limiter
from app.models import (
    EventLog,
    Merchant,
    Order,
    Product,
    Refund,
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
                "theme": s.theme,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "order_counts": st["counts"],
                "revenue_micro": st["revenue_micro"],
                "revenue_usdt": usdt(st["revenue_micro"]),
                "refunded_micro": st["refunded_micro"],
            }
        )
    return {"merchant": merchant.wallet_address, "stores": out}


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
    }


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
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, "from/to must be ISO 8601 timestamps") from exc


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
        rows = session.execute(
            select(
                Order.from_addr,
                func.count(),
                func.coalesce(func.sum(Order.paid_micro), 0),
                func.min(Order.created_at),
                func.max(Order.created_at),
            )
            .where(Order.store_id.in_(store_ids), Order.from_addr.isnot(None))
            .group_by(Order.from_addr)
            .order_by(func.max(Order.created_at).desc())
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


@router.get("/dashboard")
def dashboard_shell():
    """The merchant dashboard SPA shell. Autoescaped, carries NO merchant data:
    its inline JS does connect → nonce → personal_sign → verify (token kept in
    memory only) and renders every API string via textContent, so hostile
    store/product/order text can never become markup."""
    return HTMLResponse(render.render_shell("_dashboard.html"))
