"""M13 external product feeds: OpenAI product-feed JSON + Google Merchant Center
RSS, generated live from the same Store/Product rows as the M7 ``feed.json``.

Additive and read-only: ``feed.json`` (validated by docs/openapi.feed.yaml) is
untouched; these are separate export shapes a merchant adapts for an external
surface. Every endpoint is live-status-only (404 for pending_screening/blocked so a
pending store never leaks), emits NO pay_to / merchant wallet / buyer / order data,
carries the M7 agent headers (nosniff + 5-min cache), is rate-limited, and makes zero
outbound calls. Every text node in the Google RSS is XML-escaped (the sitemap
precedent) so a hostile store name cannot inject markup into an RSS consumer.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, Response

from app import agentic, config
from app.db import get_session
from app.limiter import limiter
from app.models import Product, Store

router = APIRouter()

_G_NS = "http://base.google.com/ns/1.0"


def _active_products(session: Session, store_id: int) -> list[Product]:
    return list(
        session.scalars(
            select(Product)
            .where(Product.store_id == store_id, Product.active.is_(True))
            .order_by(Product.id)
        )
    )


def _store_url(store: Store) -> str:
    return f"{config.PUBLIC_BASE_URL.rstrip('/')}/s/{store.slug}/"


# ------------------------------------------------------------ OpenAI JSON shape
def _openai_product(
    store: Store, product: Product, store_url: str, include_store: bool = False
) -> dict:
    """One OpenAI product-feed item. `link`/`enable_checkout` point at the hosted
    store (the proven checkout); the x402 block is a vendor extension for x402-native
    agents. No wallet/pay_to is present."""
    item = {
        "id": str(product.id),
        "title": product.name,
        "description": agentic._product_description(store),
        "link": store_url,
        "price": {
            "amount": agentic._usdt_str(product.price_micro),
            "currency": agentic.CURRENCY,
        },
        "availability": "in_stock",
        "enable_search": True,
        "enable_checkout": True,
        "x402": {
            "endpoint": f"/s/{store.slug}/buy",
            "network": agentic.NETWORK,
            "asset": agentic.ASSET,
            "schemes": agentic.enabled_schemes(product),
        },
    }
    if include_store:
        item["store"] = store.slug
    return item


@router.get("/s/{slug}/feed/openai.json")
@limiter.limit("60/minute")
def openai_feed(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    store = agentic._live_store(session, slug)
    if store is None:  # 404 for ANY non-live store (never leak pending)
        raise HTTPException(404, "store not found")
    store_url = _store_url(store)
    body = {
        "feed": {
            "store": store.slug,
            "name": agentic._store_name(store),
            "url": store_url,
        },
        "products": [
            _openai_product(store, p, store_url)
            for p in _active_products(session, store.id)
        ],
    }
    return JSONResponse(body, headers=agentic._AGENT_HEADERS)


@router.get("/feeds/openai.json")
@limiter.limit("60/minute")
def openai_aggregate(request: Request, session: Session = Depends(get_session)):
    """Tilla-wide aggregate across all live stores (the discovery-API companion)."""
    stores = list(
        session.scalars(
            select(Store)
            .where(Store.status == "live")
            .order_by(Store.created_at.desc(), Store.id.desc())
        )
    )
    base = config.PUBLIC_BASE_URL.rstrip("/")
    products: list[dict] = []
    for store in stores:
        store_url = f"{base}/s/{store.slug}/"
        for p in _active_products(session, store.id):
            products.append(_openai_product(store, p, store_url, include_store=True))
    body = {
        "feed": {"name": "Tilla", "url": base, "store_count": len(stores)},
        "products": products,
    }
    return JSONResponse(body, headers=agentic._AGENT_HEADERS)


# ------------------------------------------------------- Google Merchant RSS 2.0
def _tag(name: str, value) -> str:
    return f"<{name}>{escape(str(value))}</{name}>"


def _google_item(store: Store, product: Product, store_url: str) -> str:
    desc = agentic._product_description(store) or product.name
    return "".join(
        [
            "<item>",
            _tag("g:id", product.id),
            _tag("g:title", product.name),
            _tag("g:description", desc),
            _tag("g:link", store_url),
            # Price is emitted honestly as "N.NN USDT". GMC validation requires an
            # ISO-4217 currency; this is a shape-correct export the merchant adapts,
            # never a claim of a real USD price (see docs/acp-checkout.md runbook).
            _tag("g:price", f"{agentic._usdt_str(product.price_micro)} USDT"),
            _tag("g:availability", "in_stock"),
            _tag("g:condition", "new"),
            "</item>",
        ]
    )


def _rss(title: str, link: str, description: str, items: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<rss version="2.0" xmlns:g="{_G_NS}">'
        "<channel>"
        + _tag("title", title)
        + _tag("link", link)
        + _tag("description", description or title)
        + "".join(items)
        + "</channel></rss>"
    )


@router.get("/s/{slug}/feed/google.xml")
@limiter.limit("60/minute")
def google_feed(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    store = agentic._live_store(session, slug)
    if store is None:  # 404 for ANY non-live store (never leak pending)
        raise HTTPException(404, "store not found")
    store_url = _store_url(store)
    items = [
        _google_item(store, p, store_url) for p in _active_products(session, store.id)
    ]
    xml = _rss(
        agentic._store_name(store),
        store_url,
        agentic._store_description(store),
        items,
    )
    return Response(xml, media_type="application/xml", headers=agentic._AGENT_HEADERS)
