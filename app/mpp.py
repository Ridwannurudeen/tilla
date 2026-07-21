"""M8 MPP (Merchant Payment Protocol) pay-as-you-go session channels.

DORMANT-SAFE BY CONSTRUCTION. The router is always mounted, but every endpoint
fail-closes to 503 unless BOTH ``config.MPP_ENABLED`` is set AND the OKX SA creds
are present — so no code path can reach the Settlement Agent API while dormant.
The okxweb3-app-mpp SDK (which monkey-patches pympp at import) is imported LAZILY,
only inside the real gateway that is constructed only when a request is served
live — so a broken/young dependency can never take down the running app.

Channel state lives in the ``mpp_channels`` table, NOT the SDK's FileStore, so
every transition is a race-proof conditional UPDATE (the M3 pattern) and the state
machine unit-tests as pure functions over one row with the SA client mocked.

Opening a channel is the ONLY fund-moving act (the buyer deposits real USDT into
the escrow contract), so a real open→voucher→close is USER-gated. NEVER-FAKE rule:
no test or demo marks a channel 'closed' with a synthetic settle — the only
settlement evidence is the SA response + event_log. Vouchers are purely local
off-chain accounting (no SA call); settlement of the accumulated spend is the SA's
job at close, recorded from the SA response only.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException, Path, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from starlette.responses import JSONResponse

from app import checkout, config
from app.db import SessionLocal
from app.limiter import limiter
from app.models import MppChannel, Product, Store, log_event

logger = logging.getLogger("tilla")

router = APIRouter()

_MPP_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
_SA_BASE_URL = "https://web3.okx.com"


# ============================================================================
# Readiness + SA gateway (both fail-closed; SDK imported lazily)
# ============================================================================
def _sa_creds() -> tuple[str, str, str] | None:
    """The OKX SA creds from the server .env, or None when any is absent."""
    key = os.getenv("OKX_API_KEY", "")
    secret = os.getenv("OKX_SECRET_KEY", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    return (key, secret, passphrase) if (key and secret and passphrase) else None


def mpp_ready() -> bool:
    """MPP is live ONLY when its flag is on AND SA creds are present. Otherwise
    every endpoint 503s (fail-closed) and no SA call can ever be made."""
    return config.MPP_ENABLED and _sa_creds() is not None


class _OKXGateway:
    """Thin async wrapper over the installed okxweb3-app-mpp OKXSAClient. Every
    import of the SDK is LAZY (inside methods/__init__) so its import-time
    monkey-patch of pympp never runs while MPP is dormant. Constructed only when
    mpp_ready() is true, i.e. with real creds and a real (USER-funded) buyer."""

    def __init__(self, base_url: str, creds: tuple[str, str, str]) -> None:
        from mpp_evm.saclient import OKXSAClient

        key, secret, passphrase = creds
        self._client = OKXSAClient(
            base_url=base_url,
            api_key=key,
            secret_key=secret,
            passphrase=passphrase,
        )

    async def open(self, payload: dict) -> dict:
        from mpp_evm.saclient import SessionOpenPayload, SessionOpenRequest

        req = SessionOpenRequest(payload=SessionOpenPayload.model_validate(payload))
        receipt = await self._client.session_open(req)
        return receipt.model_dump(by_alias=True)

    async def top_up(self, payload: dict) -> dict:
        from mpp_evm.saclient import SessionTopUpPayload, SessionTopUpRequest

        req = SessionTopUpRequest(payload=SessionTopUpPayload.model_validate(payload))
        receipt = await self._client.session_top_up(req)
        return receipt.model_dump(by_alias=True)

    async def close(self, payload: dict) -> dict:
        from mpp_evm.saclient import SessionClosePayload, SessionCloseRequest

        req = SessionCloseRequest(payload=SessionClosePayload.model_validate(payload))
        receipt = await self._client.session_close(req)
        return receipt.model_dump(by_alias=True)

    async def status(self, channel_id: str) -> dict:
        result = await self._client.session_status(channel_id)
        return result.model_dump(by_alias=True)


def _default_gateway_factory() -> _OKXGateway | None:
    creds = _sa_creds()
    return _OKXGateway(_SA_BASE_URL, creds) if creds is not None else None


# Indirection so tests inject a mock SA gateway; the real path is never touched
# by the suite (the SDK stays unimported, no network, no funds).
_gateway_factory = _default_gateway_factory


def _get_gateway() -> _OKXGateway | None:
    return _gateway_factory()


# ============================================================================
# Store/product resolution (metered products only)
# ============================================================================
class MppStateError(Exception):
    """A channel-state rejection surfaced as HTTP 400 (voucher regression,
    overspend, wrong channel status). No local state changes on this path."""


class ChannelNotFound(Exception):
    """No channel row for the supplied channel_id → HTTP 404."""


def _metered_ctx(slug: str) -> dict:
    """Resolve a live store's active METERED product, mirroring the human/agent
    live-store gate (404 unknown/blocked, 409 pending), and 409 for a non-metered
    product. Returns the fields the endpoints need (no ORM objects escape)."""
    with SessionLocal() as session:
        store = session.scalar(select(Store).where(Store.slug == slug))
        if store is None:
            raise HTTPException(404, "store not found")
        if store.status == "pending_screening":
            raise HTTPException(
                409, "store is not yet live (pending content screening)"
            )
        if store.status != "live":
            raise HTTPException(404, "store not found")
        product = session.scalar(
            select(Product)
            .where(Product.store_id == store.id, Product.active.is_(True))
            .order_by(Product.id)
        )
        if product is None:
            raise HTTPException(409, "store has no active product")
        if (product.pricing_model or "one_time") != "metered":
            raise HTTPException(409, "product is not a metered (pay-as-you-go) product")
        params = (
            product.pricing_params if isinstance(product.pricing_params, dict) else {}
        )
        return {
            "store_id": store.id,
            "pay_to": store.pay_to,
            "product_id": product.id,
            "unit_price_micro": int(params.get("price_per_unit_micro") or 0),
            "min_deposit_micro": int(params.get("min_deposit_micro") or 0),
        }


def _metered_unit_payload(store_id: int) -> dict:
    """One metered unit's deliverable content: the store's active text deliverable
    (or its legacy delivery text). MPP has no per-voucher Order, so this delivers
    the content directly rather than minting an Order/Entitlement per unit."""
    with SessionLocal() as session:
        deliverable = checkout._active_deliverable(session, store_id)
        if (
            deliverable is not None
            and deliverable.kind == "text"
            and deliverable.payload
        ):
            return {"kind": "text", "delivery": deliverable.payload}
        store = session.get(Store, store_id)
        payload = (
            store.delivery if store and store.delivery else checkout.DEFAULT_DELIVERY
        )
        return {"kind": "text", "delivery": payload}


def _channel_view(ch: MppChannel) -> dict:
    return {
        "channel_id": ch.channel_id,
        "status": ch.status,
        "deposit_micro": ch.deposit_micro,
        "spent_micro": ch.spent_micro,
        "unit_price_micro": ch.unit_price_micro,
        "pay_to": ch.pay_to,
        "buyer_addr": ch.buyer_addr,
    }


# ============================================================================
# State machine — pure functions over one mpp_channels row (own session)
# ============================================================================
def open_channel(
    *,
    store_id: int,
    product_id: int,
    pay_to: str,
    channel_id: str,
    buyer_addr: str | None,
    deposit_micro: int,
    unit_price_micro: int,
) -> dict:
    """Insert a fresh 'open' channel row after the SA confirmed the open. The SA
    channel_id is UNIQUE, so a duplicate submission (replay) reconciles to the
    existing row instead of raising — idempotent, mirroring the x402 nonce path."""
    with SessionLocal() as session:
        row = MppChannel(
            store_id=store_id,
            product_id=product_id,
            channel_id=channel_id,
            buyer_addr=buyer_addr,
            pay_to=pay_to,
            deposit_micro=deposit_micro,
            spent_micro=0,
            unit_price_micro=unit_price_micro,
            status="open",
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            row = session.scalar(
                select(MppChannel).where(MppChannel.channel_id == channel_id)
            )
            if row is None:
                raise
            return _channel_view(row)
        log_event(
            session,
            "mpp",
            "channel.opened",
            store_id=store_id,
            data={"channel_id": channel_id, "deposit_micro": deposit_micro},
        )
        session.commit()
        return _channel_view(row)


def apply_voucher(
    *, channel_id: str, cumulative_micro: int, voucher_json: dict
) -> dict:
    """LOCAL off-chain accounting (no SA call). The voucher carries the cumulative
    spend; it must STRICTLY advance (a decrease or replay is a regression) and stay
    within the deposit (overspend). On success the spent counter moves via a
    conditional UPDATE keyed on the observed spent, so a concurrent voucher loses
    instead of double-serving. Any rejection leaves the row unchanged."""
    with SessionLocal() as session:
        ch = session.scalar(
            select(MppChannel).where(MppChannel.channel_id == channel_id)
        )
        if ch is None:
            raise ChannelNotFound(channel_id)
        if ch.status != "open":
            raise MppStateError("channel is not open")
        if cumulative_micro <= ch.spent_micro:
            raise MppStateError("voucher regression: cumulative amount did not advance")
        if cumulative_micro > ch.deposit_micro:
            raise MppStateError("voucher overspend: cumulative amount exceeds deposit")
        moved = session.execute(
            update(MppChannel)
            .where(
                MppChannel.channel_id == channel_id,
                MppChannel.status == "open",
                MppChannel.spent_micro == ch.spent_micro,
            )
            .values(spent_micro=cumulative_micro, last_voucher_json=voucher_json)
        )
        if moved.rowcount != 1:
            raise MppStateError("voucher race: channel changed under the voucher")
        log_event(
            session,
            "mpp",
            "channel.voucher",
            store_id=ch.store_id,
            data={"channel_id": channel_id, "cumulative_micro": cumulative_micro},
        )
        session.commit()
        return _channel_view(
            session.scalar(
                select(MppChannel).where(MppChannel.channel_id == channel_id)
            )
        )


def topup_channel(*, channel_id: str, add_micro: int) -> dict:
    """Raise an open channel's deposit after the SA confirmed the top-up. The
    conditional UPDATE keyed on the observed deposit makes a lost race a no-op."""
    if add_micro <= 0:
        raise MppStateError("top-up amount must be positive")
    with SessionLocal() as session:
        ch = session.scalar(
            select(MppChannel).where(MppChannel.channel_id == channel_id)
        )
        if ch is None:
            raise ChannelNotFound(channel_id)
        if ch.status != "open":
            raise MppStateError("channel is not open")
        new_deposit = ch.deposit_micro + add_micro
        moved = session.execute(
            update(MppChannel)
            .where(
                MppChannel.channel_id == channel_id,
                MppChannel.status == "open",
                MppChannel.deposit_micro == ch.deposit_micro,
            )
            .values(deposit_micro=new_deposit)
        )
        if moved.rowcount != 1:
            raise MppStateError("top-up race: channel changed under the top-up")
        log_event(
            session,
            "mpp",
            "channel.topped_up",
            store_id=ch.store_id,
            data={"channel_id": channel_id, "deposit_micro": new_deposit},
        )
        session.commit()
        return _channel_view(
            session.scalar(
                select(MppChannel).where(MppChannel.channel_id == channel_id)
            )
        )


def close_channel(*, channel_id: str, final_spent_micro: int | None = None) -> dict:
    """Transition an open/closing channel to 'closed' after the SA settled. The
    conditional transition is idempotent — a replay after a lost response finds the
    row already 'closed' and returns it unchanged (rowcount 0, no error)."""
    with SessionLocal() as session:
        ch = session.scalar(
            select(MppChannel).where(MppChannel.channel_id == channel_id)
        )
        if ch is None:
            raise ChannelNotFound(channel_id)
        values = {"status": "closed", "closed_at": checkout._now()}
        if final_spent_micro is not None:
            values["spent_micro"] = final_spent_micro
        moved = session.execute(
            update(MppChannel)
            .where(
                MppChannel.channel_id == channel_id,
                MppChannel.status.in_(("open", "closing")),
            )
            .values(**values)
        )
        if moved.rowcount == 1:
            log_event(
                session,
                "mpp",
                "channel.closed",
                store_id=ch.store_id,
                data={"channel_id": channel_id},
            )
            session.commit()
        return _channel_view(
            session.scalar(
                select(MppChannel).where(MppChannel.channel_id == channel_id)
            )
        )


# ============================================================================
# Endpoints — all fail-closed 503 unless mpp_ready()
# ============================================================================
def _require_mpp_ready() -> None:
    if not mpp_ready():
        raise HTTPException(503, "mpp pay-as-you-go is not configured")


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, "invalid JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "JSON body must be an object")
    return body


def _int_field(body: dict, name: str) -> int:
    value = body.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HTTPException(422, f"{name} must be a non-negative integer")
    return value


async def _sa_call(coro):
    """Run one SA gateway call, mapping any failure to a 502 with NO local state
    change (the caller only mutates the DB after this returns)."""
    try:
        return await coro
    except Exception:
        logger.exception("mpp: settlement agent call failed")
        raise HTTPException(502, "settlement agent unavailable") from None


@router.post("/s/{slug}/mpp/open")
@limiter.limit("30/minute")
async def mpp_open(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    _require_mpp_ready()
    ctx = await asyncio.to_thread(_metered_ctx, slug)
    body = await _json(request)
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(422, "payload (signed SessionIntent open) is required")
    gateway = _get_gateway()
    if gateway is None:
        raise HTTPException(503, "mpp pay-as-you-go is not configured")
    receipt = await _sa_call(gateway.open(payload))
    channel_id = receipt.get("channelId") or payload.get("channelId")
    if not channel_id:
        raise HTTPException(502, "settlement agent returned no channel id")
    deposit_micro = _receipt_deposit(receipt, ctx["min_deposit_micro"])
    buyer_addr = _payload_buyer(payload)
    row = await asyncio.to_thread(
        open_channel,
        store_id=ctx["store_id"],
        product_id=ctx["product_id"],
        pay_to=ctx["pay_to"],
        channel_id=str(channel_id),
        buyer_addr=buyer_addr,
        deposit_micro=deposit_micro,
        unit_price_micro=ctx["unit_price_micro"],
    )
    return JSONResponse({"channel": row}, headers=_MPP_HEADERS)


@router.post("/s/{slug}/mpp/voucher")
@limiter.limit("120/minute")
async def mpp_voucher(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    _require_mpp_ready()
    ctx = await asyncio.to_thread(_metered_ctx, slug)
    body = await _json(request)
    channel_id = body.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id:
        raise HTTPException(422, "channel_id is required")
    cumulative = _int_field(body, "cumulative_amount_micro")
    voucher = body.get("voucher")
    if not isinstance(voucher, dict):
        raise HTTPException(422, "voucher object is required")
    try:
        row = await asyncio.to_thread(
            apply_voucher,
            channel_id=channel_id,
            cumulative_micro=cumulative,
            voucher_json=voucher,
        )
    except ChannelNotFound:
        raise HTTPException(404, "channel not found") from None
    except MppStateError as exc:
        raise HTTPException(400, str(exc)) from None
    unit = await asyncio.to_thread(_metered_unit_payload, ctx["store_id"])
    return JSONResponse({"channel": row, "unit": unit}, headers=_MPP_HEADERS)


@router.post("/s/{slug}/mpp/topup")
@limiter.limit("30/minute")
async def mpp_topup(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    _require_mpp_ready()
    await asyncio.to_thread(_metered_ctx, slug)
    body = await _json(request)
    channel_id = body.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id:
        raise HTTPException(422, "channel_id is required")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(422, "payload (signed SessionIntent topUp) is required")
    gateway = _get_gateway()
    if gateway is None:
        raise HTTPException(503, "mpp pay-as-you-go is not configured")
    receipt = await _sa_call(gateway.top_up(payload))
    add_micro = _int_field(body, "additional_deposit_micro")
    try:
        row = await asyncio.to_thread(
            topup_channel, channel_id=channel_id, add_micro=add_micro
        )
    except ChannelNotFound:
        raise HTTPException(404, "channel not found") from None
    except MppStateError as exc:
        raise HTTPException(400, str(exc)) from None
    return JSONResponse({"channel": row, "receipt": receipt}, headers=_MPP_HEADERS)


@router.post("/s/{slug}/mpp/close")
@limiter.limit("30/minute")
async def mpp_close(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
):
    _require_mpp_ready()
    await asyncio.to_thread(_metered_ctx, slug)
    body = await _json(request)
    channel_id = body.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id:
        raise HTTPException(422, "channel_id is required")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(422, "payload (signed SessionIntent close) is required")
    gateway = _get_gateway()
    if gateway is None:
        raise HTTPException(503, "mpp pay-as-you-go is not configured")
    receipt = await _sa_call(gateway.close(payload))
    final_spent = body.get("final_spent_micro")
    if final_spent is not None and (
        not isinstance(final_spent, int)
        or isinstance(final_spent, bool)
        or final_spent < 0
    ):
        raise HTTPException(422, "final_spent_micro must be a non-negative integer")
    try:
        row = await asyncio.to_thread(
            close_channel, channel_id=channel_id, final_spent_micro=final_spent
        )
    except ChannelNotFound:
        raise HTTPException(404, "channel not found") from None
    return JSONResponse({"channel": row, "receipt": receipt}, headers=_MPP_HEADERS)


def _receipt_deposit(receipt: dict, fallback_micro: int) -> int:
    """Deposit in micro from the SA receipt's ``deposit`` (atomic-unit string),
    falling back to the product's declared minimum when the SA omits it."""
    raw = receipt.get("deposit")
    try:
        if raw is not None and str(raw) != "":
            return int(raw)
    except (TypeError, ValueError):
        pass
    return fallback_micro


def _payload_buyer(payload: dict) -> str | None:
    auth = payload.get("authorization")
    if isinstance(auth, dict):
        addr = auth.get("from")
        if isinstance(addr, str) and addr:
            return addr
    signer = payload.get("authorizedSigner")
    return signer if isinstance(signer, str) and signer else None
