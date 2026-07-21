"""Typed, dependency-free result models (plain dataclasses, no pydantic).

Each ``from_json`` classmethod parses exactly the shape the live Tilla surface
returns today (verified against app/main.py + app/agentic.py). Unknown keys are
ignored so a forward-compatible server never breaks an older SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FeedProduct:
    id: str
    title: str
    description: str
    link: str
    price_amount: str
    currency: str
    availability: str
    x402_endpoint: str
    x402_network: str
    x402_asset: str
    schemes: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "FeedProduct":
        price = d.get("price") or {}
        x402 = d.get("x402") or {}
        return cls(
            id=str(d.get("id", "")),
            title=d.get("title", ""),
            description=d.get("description", ""),
            link=d.get("link", ""),
            price_amount=str(price.get("amount", "")),
            currency=price.get("currency", ""),
            availability=d.get("availability", ""),
            x402_endpoint=x402.get("endpoint", ""),
            x402_network=x402.get("network", ""),
            x402_asset=x402.get("asset", ""),
            schemes=list(x402.get("schemes", []) or []),
        )


@dataclass
class StoreFeed:
    slug: str
    name: str
    description: str
    url: str
    products: list[FeedProduct]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "StoreFeed":
        store = d.get("store") or {}
        return cls(
            slug=store.get("slug", ""),
            name=store.get("name", ""),
            description=store.get("description", ""),
            url=store.get("url", ""),
            products=[FeedProduct.from_json(p) for p in d.get("products", []) or []],
        )


@dataclass
class DiscoveryResource:
    slug: str
    name: str
    description: str
    url: str
    feed: str
    buy: str
    mcp: str
    price_min_micro: int | None
    price_max_micro: int | None
    currency: str
    network: str
    sold_count: int

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "DiscoveryResource":
        return cls(
            slug=d.get("slug", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            url=d.get("url", ""),
            feed=d.get("feed", ""),
            buy=d.get("buy", ""),
            mcp=d.get("mcp", ""),
            price_min_micro=d.get("price_min_micro"),
            price_max_micro=d.get("price_max_micro"),
            currency=d.get("currency", ""),
            network=d.get("network", ""),
            sold_count=int(d.get("sold_count", 0) or 0),
        )


@dataclass
class Discovery:
    service: str
    total: int
    resources: list[DiscoveryResource]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Discovery":
        return cls(
            service=d.get("service", ""),
            total=int(d.get("total", 0) or 0),
            resources=[
                DiscoveryResource.from_json(r) for r in d.get("resources", []) or []
            ],
        )


@dataclass
class Checkout:
    """A freshly created human checkout order (POST /api/checkout/{slug})."""

    id: str
    pay_to: str
    amount_micro: int
    expires_at: str
    network: str = ""
    token: str = ""

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Checkout":
        return cls(
            id=d["id"],
            pay_to=d.get("pay_to", ""),
            amount_micro=int(d.get("amount_micro", 0) or 0),
            expires_at=d.get("expires_at", ""),
            network=d.get("network", ""),
            token=d.get("token", ""),
        )


@dataclass
class CheckoutStatus:
    """A checkout order's current state (GET /api/checkout/{cid})."""

    id: str
    status: str
    amount_micro: int
    pay_to: str
    tx_hash: str | None = None
    delivery: str | None = None
    download_url: str | None = None
    license_key: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "CheckoutStatus":
        return cls(
            id=d["id"],
            status=d.get("status", ""),
            amount_micro=int(d.get("amount_micro", 0) or 0),
            pay_to=d.get("pay_to", ""),
            tx_hash=d.get("tx_hash"),
            delivery=d.get("delivery"),
            download_url=d.get("download_url"),
            license_key=d.get("license_key"),
        )

    @property
    def is_paid(self) -> bool:
        return self.status == "paid"


@dataclass
class StoreCreated:
    """The result of a successful x402-paid create-store."""

    slug: str
    url: str
    manage_key: str
    store_name: str = ""
    settle_tx: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any], settle_tx: str | None) -> "StoreCreated":
        return cls(
            slug=d.get("slug", ""),
            url=d.get("url", ""),
            manage_key=d.get("manage_key", ""),
            store_name=d.get("store_name", ""),
            settle_tx=settle_tx,
        )


@dataclass
class Purchase:
    """The result of a successful x402 agent buy (POST /s/{slug}/buy)."""

    order_id: str
    amount_micro: int
    delivery: str | None = None
    download_url: str | None = None
    license_key: str | None = None
    settle_tx: str | None = None

    @classmethod
    def from_json(cls, d: dict[str, Any], settle_tx: str | None) -> "Purchase":
        return cls(
            order_id=d.get("order_id", ""),
            amount_micro=int(d.get("amount_micro", 0) or 0),
            delivery=d.get("delivery"),
            download_url=d.get("download_url"),
            license_key=d.get("license_key"),
            settle_tx=settle_tx,
        )
