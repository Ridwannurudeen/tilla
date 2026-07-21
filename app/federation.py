"""M16.4 federation ingest — the discovery mirror-of-mirrors, up to the boundary.

Tilla periodically fetches an OPERATOR-configured set of peer instances
(``config.FEDERATION_PEERS`` — never a user-submitted URL), reads each peer's
``/discovery/resources`` index + per-store ``feed.json``, schema-validates them
against the FROZEN v1 protocol contract (``docs/openapi.feed.yaml``), and caches
read-only rows in ``federated_listings``. ``GET /discovery/resources?include=
federated`` then surfaces those rows labeled ``{"origin", "federated": true}``,
each linking OUT to the peer's own checkout page.

HARD BOUNDARY (the M13 rule): this module contains ZERO fund-moving code. It
never settles, verifies a payment, quotes a price, or proxies a purchase — peer
content is DATA (rendered nowhere, only re-emitted json-encoded). Discovery links
out only. The real federated NETWORK is EXTERNALLY-BLOCKED on a second independent
operator running the protocol (VISION §3); this builds + tests the entire ingest
against fixtures up to that boundary.

Defenses (SSRF / poisoned feed): peers are https-only public origins, fetched
with ``trust_env=False`` and ``follow_redirects=False``, each document hard-capped
at ``FEDERATION_MAX_BYTES`` before parse, and validated against the pinned schema;
an off-schema or oversize document is dropped, never ingested.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib

import httpx
import jsonschema
import yaml
from sqlalchemy import delete, select

from app import config
from app.db import SessionLocal
from app.models import FederatedListing
from app.webhooks import WebhookURLError, validate_webhook_url

logger = logging.getLogger("tilla")

# The pinned per-store feed schema (frozen by tilla-protocol-v1, M16.3). A peer
# feed that does not validate is DROPPED — the spec cannot silently ingest an
# off-contract document.
_FEED_SCHEMA = yaml.safe_load(
    (
        pathlib.Path(__file__).resolve().parent.parent / "docs" / "openapi.feed.yaml"
    ).read_text(encoding="utf-8")
)


def _fetch_json(client: httpx.Client, url: str) -> dict | None:
    """GET ``url`` and parse JSON, enforcing the body cap BEFORE parse. None on any
    transport error, oversize body, or non-JSON — the caller drops the document."""
    try:
        resp = client.get(url)
    except httpx.HTTPError:
        logger.warning("federation: fetch failed for %s", url)
        return None
    if resp.status_code != 200:
        return None
    body = resp.content
    if len(body) > config.FEDERATION_MAX_BYTES:
        logger.warning(
            "federation: %s exceeds %d bytes", url, config.FEDERATION_MAX_BYTES
        )
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _peer_store_row(peer: str, resource: dict, feed: dict) -> dict | None:
    """Build one federated_listings row dict from a peer discovery resource + its
    validated feed. Absolute OUTBOUND urls into the PEER (never a Tilla route).
    None when the resource lacks the minimal shape."""
    slug = resource.get("slug")
    if not isinstance(slug, str) or not slug:
        return None
    store = feed.get("store") if isinstance(feed.get("store"), dict) else {}
    base = peer.rstrip("/")
    return {
        "origin": peer,
        "slug": slug,
        "name": str(store.get("name") or resource.get("name") or slug),
        "description": str(
            store.get("description") or resource.get("description") or ""
        ),
        "url": str(store.get("url") or f"{base}/s/{slug}/"),
        "feed_url": f"{base}/s/{slug}/feed.json",
        "price_min_micro": resource.get("price_min_micro"),
        "price_max_micro": resource.get("price_max_micro"),
        "currency": resource.get("currency"),
        "network": resource.get("network"),
    }


def ingest_peer(client: httpx.Client, peer: str) -> list[dict]:
    """Fetch + validate one peer, returning the list of row dicts to cache. A peer
    whose discovery index is missing/oversize/off-shape yields []; individual
    stores whose feed.json is missing or off-schema are dropped (never ingested)."""
    index = _fetch_json(client, f"{peer.rstrip('/')}/discovery/resources")
    if index is None:
        return []
    resources = index.get("resources")
    if not isinstance(resources, list):
        return []
    rows: list[dict] = []
    for resource in resources[: config.FEDERATION_MAX_STORES]:
        if not isinstance(resource, dict):
            continue
        slug = resource.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        feed = _fetch_json(client, f"{peer.rstrip('/')}/s/{slug}/feed.json")
        if feed is None:
            continue
        try:
            jsonschema.validate(feed, _FEED_SCHEMA)  # frozen v1 contract
        except jsonschema.ValidationError:
            logger.warning("federation: %s/%s feed off-schema, dropped", peer, slug)
            continue
        row = _peer_store_row(peer, resource, feed)
        if row is not None:
            rows.append(row)
    return rows


def _replace_peer_rows(peer: str, rows: list[dict]) -> None:
    """Idempotent upsert: replace this peer's cached rows in one transaction so a
    store dropped by the peer disappears here too."""
    with SessionLocal() as session:
        session.execute(delete(FederatedListing).where(FederatedListing.origin == peer))
        for row in rows:
            session.add(FederatedListing(**row))
        session.commit()


def ingest_once() -> int:
    """One ingest pass over every configured peer. DORMANT when no peers are
    configured: returns 0 immediately with ZERO network. Returns the total rows
    cached. Sync — the loop runs it off-thread."""
    peers = config.FEDERATION_PEERS
    if not peers:
        return 0
    total = 0
    with httpx.Client(
        timeout=config.FEDERATION_FETCH_TIMEOUT,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        for peer in peers:
            try:
                validate_peer(peer)
            except WebhookURLError:
                logger.warning("federation: peer %s failed url gate, skipped", peer)
                continue
            try:
                rows = ingest_peer(client, peer)
                _replace_peer_rows(peer, rows)
                total += len(rows)
            except Exception:
                # One hostile/broken peer must not abort ingest of the others.
                # Its prior rows stay as-is until it serves a valid feed again.
                logger.warning("federation: peer %s ingest failed, skipped", peer)
                continue
    return total


def validate_peer(peer: str) -> str:
    """A peer origin must be a safe public https URL (reuses the outbound SSRF
    gate). Operator-configured, but re-checked at fetch time (TOCTOU/DNS-rebind)."""
    return validate_webhook_url(peer)


def federated_rows(session, limit: int) -> list[dict]:
    """The cached peer listings, labeled for the discovery echo. Peer content is
    re-emitted json-encoded (never rendered) and every link points OUT to the
    peer — Tilla exposes no buy/mcp route of its own for a federated row."""
    rows = session.scalars(
        select(FederatedListing).order_by(FederatedListing.id.desc()).limit(limit)
    ).all()
    return [
        {
            "origin": r.origin,
            "federated": True,
            "slug": r.slug,
            "name": r.name,
            "description": r.description or "",
            "url": r.url,
            "feed": r.feed_url,
            "price_min_micro": r.price_min_micro,
            "price_max_micro": r.price_max_micro,
            "currency": r.currency,
            "network": r.network,
        }
        for r in rows
    ]


async def federation_loop() -> None:
    """Background loop (lifespan task) mirroring webhook_loop: each tick runs
    off-thread; a bad tick is logged and the loop continues. Dormant/idle when no
    peers are configured."""
    logger.info(
        "tilla federation loop started (interval=%ss, peers=%d)",
        config.FEDERATION_INTERVAL,
        len(config.FEDERATION_PEERS),
    )
    while True:
        try:
            await asyncio.to_thread(ingest_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("federation ingest tick failed")
        await asyncio.sleep(config.FEDERATION_INTERVAL)
