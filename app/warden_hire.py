"""M10 Warden PAID hire — a one-shot x402 payer client (agents-hiring-agents).

Dormant by default. :func:`paid_scan` upgrades a single content screen from the
FREE demo endpoint to a PAID hire of Warden #3808's listed scan service: it decodes
the 402 PAYMENT-REQUIRED challenge (which carries the full EIP-712 spec), signs an
EIP-3009 authorization LOCALLY with eth-account, replays ONCE with the signed
PAYMENT-SIGNATURE header, and reads the PAYMENT-RESPONSE settle tx hash back.

Funds-safety invariants (funds move only when Tilla means to spend):
  (a) it REFUSES to sign unless the challenge's scheme/asset/network/payTo/amount
      match pinned expectations (payTo pinned to Warden's wallet, amount capped at
      ``config.TILLA_WARDEN_MAX_MICRO``) — a changed/hostile 402 can never
      over-commit or redirect the fee;
  (b) it signs AT MOST ONCE and NEVER retries a signed authorization — a transport
      failure after signing returns None (the caller falls back to the free demo
      scan) rather than re-firing; funds settle only on a served 200;
  (c) ANY problem (missing key, undecodable challenge, pin mismatch, non-200,
      timeout) returns None so the caller degrades to the free demo endpoint and
      screening semantics stay unchanged.

The verdict body is returned verbatim; ALLOW/BLOCK interpretation stays in
``app.screening`` (one place enforces the verdict contract).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx
from x402.http.constants import PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER
from x402.http.utils import (
    PAYMENT_REQUIRED_HEADER,
    decode_payment_required_header,
    decode_payment_response_header,
    encode_payment_signature_header,
)
from x402.schemas import PaymentPayload, PaymentRequirements

from app import config
from app.payment import (
    PAYMENT_ASSET,
    PAYMENT_EIP712_NAME,
    PAYMENT_EIP712_VERSION,
    PAYMENT_NETWORK,
    PAYMENT_SCHEME,
    PAYMENT_TIMEOUT_SECONDS,
)

logger = logging.getLogger("tilla")

# The EIP-3009 TransferWithAuthorization struct, signed locally with eth-account so
# the paid path needs NO web3 (the x402 SDK's EVM signer does). Field order/types
# must match the facilitator's expectation exactly (see x402 evm eip712.py).
_TRANSFER_WITH_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}
# Small clock-skew buffer for validAfter (mirrors the SDK's validity window).
_VALIDITY_BUFFER_SECONDS = 60


@dataclass
class PaidScan:
    """The result of a served paid scan: the raw verdict body plus the on-chain
    settle receipt. ``tx_hash`` is None only when the PAYMENT-RESPONSE header was
    undecodable (the scan still settled and was served)."""

    verdict: dict
    amount_micro: int
    tx_hash: str | None


def _select_exact(req) -> PaymentRequirements | None:
    """The exact-scheme requirement on Tilla's network, or None."""
    for a in getattr(req, "accepts", None) or []:
        if (
            getattr(a, "scheme", None) == PAYMENT_SCHEME
            and str(getattr(a, "network", "")) == PAYMENT_NETWORK
        ):
            return a
    return None


def _pins_ok(reqs: PaymentRequirements) -> bool:
    """True iff the challenge matches every pinned expectation — checked BEFORE any
    signing so a hostile/changed 402 can never move funds."""
    pinned_pay_to = (config.WARDEN_PAID_PAYTO or "").lower()
    if not pinned_pay_to:
        logger.warning("warden_hire: no pinned payTo configured; refusing to sign")
        return False
    if (reqs.pay_to or "").lower() != pinned_pay_to:
        logger.warning("warden_hire: challenge payTo does not match the pinned wallet")
        return False
    if (reqs.asset or "").lower() != PAYMENT_ASSET.lower():
        logger.warning("warden_hire: challenge asset does not match USDT0")
        return False
    if str(reqs.network) != PAYMENT_NETWORK:
        logger.warning("warden_hire: challenge network is not eip155:196")
        return False
    try:
        amount = int(reqs.amount)
    except (TypeError, ValueError):
        logger.warning("warden_hire: challenge amount is not an integer")
        return False
    if amount <= 0 or amount > config.TILLA_WARDEN_MAX_MICRO:
        logger.warning(
            "warden_hire: challenge amount %s over cap %s",
            amount,
            config.TILLA_WARDEN_MAX_MICRO,
        )
        return False
    return True


def _sign_payload(reqs: PaymentRequirements) -> str | None:
    """Build + sign the EIP-3009 authorization for `reqs` LOCALLY with eth-account
    (no web3), returning the encoded PAYMENT-SIGNATURE header value, or None on any
    signing error. The wire shape mirrors x402's ExactEIP3009Payload.to_dict()."""
    try:
        from eth_account import Account

        account = Account.from_key(config.TILLA_WARDEN_PAYER_KEY)
        chain_id = int(str(reqs.network).split(":")[1])
        extra = reqs.extra or {}
        name = extra.get("name") or PAYMENT_EIP712_NAME
        version = extra.get("version") or PAYMENT_EIP712_VERSION
        now = int(time.time())
        valid_after = now - _VALIDITY_BUFFER_SECONDS
        valid_before = now + (reqs.max_timeout_seconds or PAYMENT_TIMEOUT_SECONDS)
        nonce_bytes = os.urandom(32)
        nonce_hex = "0x" + nonce_bytes.hex()
        value = int(reqs.amount)

        signed = Account.sign_typed_data(
            config.TILLA_WARDEN_PAYER_KEY,
            domain_data={
                "name": name,
                "version": version,
                "chainId": chain_id,
                "verifyingContract": reqs.asset,
            },
            message_types=_TRANSFER_WITH_AUTHORIZATION_TYPES,
            message_data={
                "from": account.address,
                "to": reqs.pay_to,
                "value": value,
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": nonce_bytes,
            },
        )
        inner = {
            "authorization": {
                "from": account.address,
                "to": reqs.pay_to,
                "value": str(value),
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": nonce_hex,
            },
            "signature": "0x" + bytes(signed.signature).hex(),
        }
        payload = PaymentPayload(x402_version=2, payload=inner, accepted=reqs)
        return encode_payment_signature_header(payload)
    except Exception:
        logger.exception("warden_hire: failed to build/sign payment authorization")
        return None


def paid_scan(payload: str) -> PaidScan | None:
    """Hire Warden's paid scan for one screen, or None if the paid path is not
    cleanly usable (caller then falls back to the free demo endpoint). Signs at most
    once and never retries a signed authorization."""
    if not config.TILLA_WARDEN_PAYER_KEY:
        return None
    timeout = config.WARDEN_SCREEN_TIMEOUT
    url = config.WARDEN_PAID_SCAN_URL
    body = {"payload": payload}

    # 1) Unpaid probe: expect a 402 carrying the EIP-712 challenge.
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            challenge = client.post(url, json=body)
            if challenge.status_code != 402:
                logger.warning(
                    "warden_hire: expected 402, got %s; using demo",
                    challenge.status_code,
                )
                return None
            header = challenge.headers.get(PAYMENT_REQUIRED_HEADER)
            if not header:
                logger.warning("warden_hire: 402 without a PAYMENT-REQUIRED header")
                return None
    except httpx.HTTPError:
        logger.warning("warden_hire: challenge request failed; using demo")
        return None

    # 2) Decode + PIN. No signing happens unless every pin matches.
    try:
        required = decode_payment_required_header(header)
    except Exception:
        logger.exception("warden_hire: undecodable PAYMENT-REQUIRED challenge")
        return None
    reqs = _select_exact(required)
    if reqs is None or not _pins_ok(reqs):
        return None

    # 3) Sign LOCALLY (one authorization).
    signature = _sign_payload(reqs)
    if signature is None:
        return None
    amount_micro = int(reqs.amount)

    # 4) Replay EXACTLY ONCE with the signed header. A transport failure here does
    #    NOT re-fire (one-clean-tx): funds settle only on a served 200.
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            paid = client.post(
                url, json=body, headers={PAYMENT_SIGNATURE_HEADER: signature}
            )
    except httpx.HTTPError:
        logger.warning("warden_hire: paid replay failed after signing; using demo")
        return None
    if paid.status_code != 200:
        logger.warning(
            "warden_hire: paid replay returned %s (no settle); using demo",
            paid.status_code,
        )
        return None
    try:
        verdict = paid.json()
    except ValueError:
        logger.warning("warden_hire: paid scan returned a non-JSON body; using demo")
        return None
    if not isinstance(verdict, dict):
        return None

    tx_hash = None
    settle_header = paid.headers.get(PAYMENT_RESPONSE_HEADER)
    if settle_header:
        try:
            tx_hash = decode_payment_response_header(settle_header).transaction or None
        except Exception:
            logger.exception("warden_hire: undecodable PAYMENT-RESPONSE on served scan")
    return PaidScan(verdict=verdict, amount_micro=amount_micro, tx_hash=tx_hash)
