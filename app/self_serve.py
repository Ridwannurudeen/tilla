"""Human self-serve store creation — the paid dashboard flow.

A signed-in merchant describes a store; the description is screened BEFORE any
payment is offered (fail-closed: only a clear verdict lets a payment be created, so
a merchant is never charged for content that would be blocked). They pay the create-store fee to
Tilla's rail from their own wallet using the same on-chain USDT0 transfer the
storefront checkout uses, then submit the tx hash. The payment is verified on-chain
— exactly the create-store fee, to Tilla's pay_to, from the merchant's wallet, not
already consumed — and only then is the store generated with their wallet as the
receive address.

Payment verification is self-contained (reuses chain.py primitives) so the checkout
money path is untouched. UNIQUE(store_creations.tx_hash) is the durable guard that
one submitted payment funds at most one store.
"""

from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import chain, config, engine, payment, screening
from app.models import StoreCreation
from app.payment import PAYMENT_AMOUNT, load_payment_rail


class CreationError(Exception):
    """A create-store flow error carrying the HTTP status the route should raise."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _tilla_pay_to() -> str:
    """Tilla's own rail address — where the create-store fee lands (same pay_to the
    agent x402 create-store settles to)."""
    return load_payment_rail(os.environ).pay_to


def create_intent(
    session: Session, merchant_addr: str, description: str, theme: str | None
) -> StoreCreation:
    """Screen the description, then create a pending payment intent. Fail-closed:
    a BLOCK or any non-clear verdict raises CreationError(422) and no payment is
    offered."""
    try:
        outcome = screening.screen(description)
    except screening.ScreeningBlocked as exc:
        raise CreationError(422, "content did not pass safety screening") from exc
    if outcome.status != "allow":
        # 'pending' = flagged or screening-unavailable — never take a payment we can't
        # clear. NB: this is the screening verdict ('allow' | 'pending'), not the
        # StoreCreation row status ('pending' | 'live') used further down.
        raise CreationError(422, "content did not pass safety screening")
    creation = StoreCreation(
        merchant_addr=merchant_addr.lower(),
        description=description,
        theme=theme,
        expected_micro=int(PAYMENT_AMOUNT),
        pay_to=_tilla_pay_to(),
        status="pending",
    )
    session.add(creation)
    session.flush()  # assign the id for the response
    return creation


def get_creation(
    session: Session, creation_id: int, merchant_addr: str
) -> StoreCreation | None:
    """Load a creation owned by this merchant (owner-scoped — a foreign id reads as
    not-found, never another merchant's row)."""
    creation = session.get(StoreCreation, creation_id)
    if creation is None or creation.merchant_addr != merchant_addr.lower():
        return None
    return creation


def _verify_payment(
    tx_hash: str, pay_to: str, expected_micro: int, from_addr: str
) -> None:
    """Verify the tx is a succeeded USDT0 transfer summing EXACTLY to expected_micro
    from from_addr to pay_to (exact-amount, mirroring the checkout matcher so a
    stray/partial transfer can't be farmed). Raises CreationError on any mismatch."""
    # Pin verification to Tilla's canonical chain (its RPC + asset only) — the same
    # pinning refunds/checkout use. get_transaction_receipt takes the ChainConfig
    # FIRST; omitting it silently passed tx_hash as the config and 500'd on every pay.
    receipt = chain.get_transaction_receipt(
        payment.CANONICAL_CHAIN, tx_hash, config.RPC_TIMEOUT_REQUEST
    )
    if receipt is None:
        raise CreationError(400, "transaction not found")
    if str(receipt.get("status", "")).lower() != "0x1":
        raise CreationError(400, "transaction did not succeed on-chain")
    want_to = pay_to.lower()
    want_from = from_addr.lower()
    total = 0
    for lg in receipt.get("logs", []):
        if (lg.get("address", "") or "").lower() != config.USDT0:
            continue
        topics = lg.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != config.TRANSFER_TOPIC:
            continue
        decoded = chain.decode_transfer_log(lg)
        if decoded["to"] == want_to and decoded["from"] == want_from:
            total += decoded["value"]
    if total != expected_micro:
        raise CreationError(400, "payment does not match the create-store fee")


def pay_and_create(
    session: Session, creation: StoreCreation, tx_hash: str
) -> StoreCreation:
    """Verify the payment and generate the store. pending -> paid (payment verified)
    -> live (store generated). A generation outage AFTER a real payment leaves the
    record 'paid' with a NULL slug for a no-recharge retry; blocked generated content
    marks it 'failed'. A reused tx is a 409."""
    if creation.status == "live" and creation.slug:
        return creation  # idempotent
    tx_hash = (tx_hash or "").strip().lower()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise CreationError(400, "invalid transaction hash")

    if creation.status == "pending":
        clash = session.scalar(
            select(StoreCreation).where(StoreCreation.tx_hash == tx_hash)
        )
        if clash is not None and clash.id != creation.id:
            raise CreationError(409, "payment already used")
        _verify_payment(
            tx_hash, creation.pay_to, creation.expected_micro, creation.merchant_addr
        )
        creation.tx_hash = tx_hash
        creation.status = "paid"
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise CreationError(409, "payment already used") from exc

    return _generate(creation)


def retry(session: Session, creation: StoreCreation) -> StoreCreation:
    """Retry generation for a paid-but-ungenerated creation (after a model outage).
    No payment re-check — the payment was already verified when status became 'paid'
    — so the merchant is never re-charged."""
    if creation.status != "paid" or creation.slug:
        raise CreationError(409, "nothing to retry")
    return _generate(creation)


def _generate(creation: StoreCreation) -> StoreCreation:
    """Generate the store for a paid creation. Runs only while no slug exists, so a
    retry after a generation outage never double-creates. create_store owns its own
    DB transaction (commits the store independently)."""
    if creation.slug:
        creation.status = "live"
        return creation
    try:
        result = engine.create_store(
            creation.description, creation.merchant_addr, theme=creation.theme
        )
    except engine.GenerationUnavailable:
        return creation  # stays 'paid' — retry when the model is back, no recharge
    if result.get("status") == "live" and result.get("slug"):
        creation.slug = result["slug"]
        creation.status = "live"
    else:
        # Generated content could not clear screening — a live store was not built.
        creation.status = "failed"
    return creation
