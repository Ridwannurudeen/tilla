"""Portable delivery evidence — Tilla as merchant of record attesting WHAT it sold.

An order's payment is already public: anyone can read the USDT0 transfer on X Layer,
and an attested order carries an EAS UID. What the chain does NOT say is what was
actually delivered against that payment, whether the buyer accepted it, and whether it
was later refunded. Tilla is the only party that holds that mapping, so this module
turns it into something a counterparty can carry elsewhere.

TRUST MODEL, STATED PLAINLY. The bundle carries two classes of fact and a reader
should treat them differently:

  * INDEPENDENTLY VERIFIABLE — ``tx_hash``, ``block_number``, ``attestation_uid`` and
    ``attest_tx`` are on-chain. A reader checks these against X Layer and needs no
    trust in Tilla at all.
  * FIRST-PARTY — ``content_hash`` (sha256 of the delivered payload), ``eval_status``
    (buyer accepted / disputed / still inside the window) and the refund figures come
    from Tilla's records. The HMAC signature makes the bundle tamper-evident, not
    true: it proves Tilla issued this statement, not that Tilla is honest. That is
    the honest ceiling of any merchant-of-record attestation and it is why the
    on-chain anchors are included rather than summarised.

The signature is HMAC over canonical JSON, so verification requires the server-side
key — hence ``verify_bundle`` and the free re-check route that pairs with it. An
EVM-signed variant would be offline-verifiable and is the obvious next step, but it
would mix purposes with the dormant attester key, so it is deliberately not v1.

NEVER EXPOSED, deliberately: buyer_email (PII the chain does not carry), the
deliverable payload itself (the whole point of a content hash), x402_nonce and
referrer_addr (replay/affiliate surface with no evidentiary value).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta

from sqlalchemy import select

from app import config
from app.models import Order, Product, Store

# Bumped when the bundle's shape changes, so a stored bundle stays checkable against
# the rules it was signed under.
EVIDENCE_VERSION = 1
_SIG_PREFIX = "tilla-evidence-v1"


class EvidenceUnavailable(RuntimeError):
    """No signing key configured — the caller must fail closed rather than hand back
    an unsigned bundle that looks authoritative."""


def _iso(dt) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def canonical(bundle: dict) -> str:
    """The exact bytes the signature covers. Sorted keys + tight separators so an
    independent re-serialisation reproduces it byte-for-byte."""
    return json.dumps(bundle, sort_keys=True, separators=(",", ":"))


def sign_bundle(bundle: dict) -> str:
    if not config.SIGNING_KEY:
        raise EvidenceUnavailable("TILLA_SIGNING_KEY is not set")
    mac = hmac.new(
        config.SIGNING_KEY.encode(),
        f"{_SIG_PREFIX}.{canonical(bundle)}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"v1={mac}"


def verify_bundle(bundle: dict, signature: str) -> bool:
    """True iff `signature` is Tilla's over exactly this bundle. Constant-time."""
    if not config.SIGNING_KEY or not signature:
        return False
    try:
        expected = sign_bundle(bundle)
    except EvidenceUnavailable:
        return False
    return hmac.compare_digest(expected, signature)


def _effective_eval(order: Order, window_days: int) -> str:
    """The buyer's verdict as it reads NOW. A 'pending' order auto-confirms by time
    once it is older than the dispute window — the same rule the discovery query
    applies, restated here so evidence and discovery can never disagree."""
    if order.eval_status != "pending":
        return order.eval_status
    if order.paid_at and order.paid_at < _now() - timedelta(days=window_days):
        return "auto_confirmed"
    return "pending"


def _now():
    from app import checkout

    return checkout._now()


def build_bundle(session, *, order_id=None, tx_hash=None, attestation_uid=None) -> dict:
    """Evidence for one delivered order, looked up by whichever handle the caller
    holds. Returns ``{"found": False}`` rather than raising, so a miss costs the
    caller a paid call but never leaks whether an id merely exists in another state.
    """
    from app.agentic import EVAL_WINDOW_DAYS

    stmt = select(Order)
    if order_id:
        stmt = stmt.where(Order.id == order_id)
    elif tx_hash:
        stmt = stmt.where(Order.tx_hash == tx_hash)
    elif attestation_uid:
        stmt = stmt.where(Order.attestation_uid == attestation_uid)
    else:
        return {"found": False, "reason": "no lookup key supplied"}

    order = session.scalars(stmt.limit(1)).first()
    # Only terminal, delivered orders are evidence. A pending or expired checkout
    # says nothing about delivery and must never be dressed up as a receipt.
    if order is None or order.status != "delivered":
        return {"found": False, "reason": "no delivered order for that key"}

    store = session.get(Store, order.store_id)
    product = session.get(Product, order.product_id) if order.product_id else None

    return {
        "evidence_version": EVIDENCE_VERSION,
        "found": True,
        "issuer": config.DOMAIN,
        "order_id": order.id,
        "store_slug": store.slug if store else None,
        "product_name": product.name if product else None,
        "channel": order.channel,
        "network": order.network,
        "currency": "USDT0",
        "expected_micro": order.expected_micro,
        "paid_micro": order.paid_micro,
        "refunded_micro": order.refunded_micro,
        "buyer": order.from_addr,
        "merchant": order.pay_to,
        "paid_at": _iso(order.paid_at),
        # first-party: what was delivered, and whether the buyer accepted it
        "content_hash": order.content_hash,
        "eval_status": _effective_eval(order, EVAL_WINDOW_DAYS),
        "eval_window_days": EVAL_WINDOW_DAYS,
        # independently verifiable on X Layer — check these instead of trusting us
        "settlement_tx": order.tx_hash,
        "block_number": order.block_number,
        "attestation_uid": order.attestation_uid,
        "attestation_tx": order.attest_tx,
        "attestation_status": order.attest_status,
    }
