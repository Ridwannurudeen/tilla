"""M9 outbound webhooks: an SSRF-safe, HMAC-signed, at-least-once delivery outbox.

Events (order.paid / order.delivered / order.refunded) are ENQUEUED as a pure DB
insert inside the same transaction as the state change they report — no HTTP in a
transaction, and no import cycle (this module never imports checkout/refunds). The
``webhook_loop`` background task then claims due rows with a race-proof conditional
UPDATE, POSTs them with exponential backoff, and reads only the status code.

SSRF defense (``validate_webhook_url``) runs at REGISTRATION and again at every SEND
(TOCTOU/DNS-rebind defense): https only; every resolved IP is rejected if it is
loopback/private/link-local/reserved/multicast/unspecified or in the CGNAT range.
Delivery uses follow_redirects=False and discards the response body, so a redirect
to an internal host is never followed and a rebind yields no exfiltration.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
import urllib.parse
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update

from app import config
from app.db import SessionLocal
from app.models import Merchant, WebhookDelivery

logger = logging.getLogger("tilla")

_BATCH = 50
_BACKOFF_BASE_SEC = 60  # 1m, 4m, 16m, ~1h … = 60 * 4**(attempts-1)
# CGNAT (RFC 6598); RFC1918 / loopback / link-local / ULA are covered by the
# ipaddress is_* flags below.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")

_Claim = namedtuple("_Claim", "id attempts event payload created_at url secret")


class WebhookURLError(ValueError):
    """A merchant-supplied webhook URL failed the SSRF/scheme checks (→422)."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------- SSRF URL gate
def _ip_is_blocked(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # unwrap ::ffff:10.0.0.1 style mappings
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    return ip.version == 4 and ip in _CGNAT_V4


def validate_webhook_url(url: str) -> str:
    """Return the URL if it is a safe public https endpoint, else raise
    WebhookURLError. Resolves the host and checks EVERY resolved address, so a
    hostname that maps to an internal IP is rejected too."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise WebhookURLError("webhook URL must use https")
    host = parsed.hostname
    if not host:
        raise WebhookURLError("webhook URL has no host")
    if host.lower() == "localhost":
        raise WebhookURLError("internal host is not allowed")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise WebhookURLError("webhook host does not resolve") from exc
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise WebhookURLError("webhook host does not resolve")
    for addr in addrs:
        if _ip_is_blocked(addr):
            raise WebhookURLError("webhook host resolves to a private/internal address")
    return url


# --------------------------------------------------------------- HMAC signing
def sign_payload(secret: str, timestamp: int, body: str) -> str:
    """Stripe-style signature over ``timestamp.body`` — the timestamp is inside the
    signed material so a receiver can reject stale replays (|now - t| > 300s)."""
    mac = hmac.new(
        secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={mac}"


# ------------------------------------------------------------------- enqueue
def enqueue(session, merchant_id: int, event: str, order, extra: dict | None = None):
    """Insert one outbox row for `event` about `order`, IFF the merchant has a
    webhook configured (otherwise a no-op — the loop stays idle). Pure DB write,
    committed by the caller's transaction. Never raises on a missing webhook."""
    merchant = session.get(Merchant, merchant_id)
    if merchant is None or not merchant.webhook_url or not merchant.webhook_secret:
        return
    payload = {
        "order_id": order.id,
        "store_id": order.store_id,
        "status": order.status,
        "channel": order.channel,
        "amount_micro": order.amount_micro,
        "expected_micro": order.expected_micro,
        "paid_micro": order.paid_micro,
        "overpaid_micro": order.overpaid_micro,
        "refunded_micro": order.refunded_micro,
        "from_addr": order.from_addr,
        "tx_hash": order.tx_hash,
    }
    if extra:
        payload.update(extra)
    session.add(
        WebhookDelivery(
            merchant_id=merchant_id,
            event=event,
            order_id=order.id,
            payload=payload,
            status="pending",
            attempts=0,
            next_attempt_at=_now(),
        )
    )


# ------------------------------------------------------------------- dispatch
def _record_result(row_id: int, attempts: int, status_code: int | None) -> None:
    now = _now()
    with SessionLocal() as session:
        if status_code is not None and 200 <= status_code < 300:
            values = {
                "status": "delivered",
                "delivered_at": now,
                "last_status_code": status_code,
            }
        elif attempts >= config.WEBHOOK_MAX_ATTEMPTS:
            values = {"status": "dead", "last_status_code": status_code}
        else:
            delay = _BACKOFF_BASE_SEC * (4 ** (attempts - 1))
            values = {
                "next_attempt_at": now + timedelta(seconds=delay),
                "last_status_code": status_code,
            }
        session.execute(
            update(WebhookDelivery).where(WebhookDelivery.id == row_id).values(**values)
        )
        session.commit()


def _deliver(c: _Claim) -> None:
    body = json.dumps(
        {
            "event": c.event,
            "delivery_id": c.id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "order": c.payload,
        },
        separators=(",", ":"),
    )
    timestamp = int(time.time())
    headers = {
        "content-type": "application/json",
        "X-Tilla-Event": c.event,
        "X-Tilla-Delivery-Id": str(c.id),
        "X-Tilla-Signature": sign_payload(c.secret, timestamp, body),
    }
    status_code: int | None = None
    try:
        # Re-validate at SEND (TOCTOU/DNS-rebind). follow_redirects=False so a
        # redirect to an internal host is never followed; the body is discarded.
        validate_webhook_url(c.url)
        with httpx.Client(
            timeout=config.WEBHOOK_TIMEOUT, trust_env=False, follow_redirects=False
        ) as client:
            resp = client.post(c.url, content=body, headers=headers)
        status_code = resp.status_code
    except WebhookURLError:
        logger.warning("webhook %s url failed re-validation at send", c.id)
    except httpx.HTTPError:
        logger.warning("webhook %s delivery failed (transport)", c.id)
    _record_result(c.id, c.attempts, status_code)


def dispatch_once() -> int:
    """One dispatch pass: claim due pending rows (race-proof conditional UPDATE
    bumping attempts), POST each OFF the transaction, record the result. Returns
    the number of deliveries attempted. Sync — the loop runs it off-thread."""
    now = _now()
    claims: list[_Claim] = []
    with SessionLocal() as session:
        due = session.scalars(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status == "pending",
                WebhookDelivery.next_attempt_at <= now,
            )
            .order_by(WebhookDelivery.id)
            .limit(_BATCH)
        ).all()
        for row in due:
            merchant = session.get(Merchant, row.merchant_id)
            if (
                merchant is None
                or not merchant.webhook_url
                or not merchant.webhook_secret
            ):
                # webhook was removed after enqueue — nothing to deliver to
                session.execute(
                    update(WebhookDelivery)
                    .where(
                        WebhookDelivery.id == row.id,
                        WebhookDelivery.status == "pending",
                    )
                    .values(status="dead")
                )
                continue
            # capture BEFORE the UPDATE: synchronize_session syncs row.attempts to
            # the bumped value in-session, so read it now to avoid double-counting.
            attempt_no = row.attempts + 1
            claimed = session.execute(
                update(WebhookDelivery)
                .where(
                    WebhookDelivery.id == row.id,
                    WebhookDelivery.status == "pending",
                )
                .values(attempts=WebhookDelivery.attempts + 1)
            ).rowcount
            if claimed == 1:
                claims.append(
                    _Claim(
                        row.id,
                        attempt_no,
                        row.event,
                        row.payload,
                        row.created_at,
                        merchant.webhook_url,
                        merchant.webhook_secret,
                    )
                )
        session.commit()
    for c in claims:
        _deliver(c)
    return len(claims)


async def webhook_loop() -> None:
    """Background loop (lifespan task) mirroring sweeper_loop: each tick runs
    off-thread; a bad tick is logged and the loop continues. Idle when no rows are
    due."""
    logger.info("tilla webhook loop started (interval=%ss)", config.WEBHOOK_INTERVAL)
    while True:
        try:
            await asyncio.to_thread(dispatch_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webhook dispatch tick failed")
        await asyncio.sleep(config.WEBHOOK_INTERVAL)
