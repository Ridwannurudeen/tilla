"""The signer hook — the ONLY place a private key is ever used.

The SDK NEVER bundles, reads, defaults, or logs a key. The caller constructs a
signer (from their own env / keystore / remote wallet service) and hands it to the
x402 methods. A remote-signer implementation can satisfy :class:`PaymentSigner`
without the key ever entering this process at all.

``LocalEip3009Signer`` reproduces the production ``warden_hire`` payer's local
EIP-712 ``TransferWithAuthorization`` signing byte-for-byte: same type struct, same
60-second ``validAfter`` buffer, ``os.urandom(32)`` nonce, and the same
``ExactEIP3009Payload`` wire shape. It requires the optional ``[signer]`` extra
(``eth-account``); importing it without that installed raises a clear error.
"""

from __future__ import annotations

import os
import time
from typing import Protocol, runtime_checkable

from .x402_codec import PaymentChallenge, encode_payment_signature

# The EIP-3009 struct, identical to app/warden_hire.py. Field order/types must match
# the facilitator's expectation exactly.
TRANSFER_WITH_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}
# Small clock-skew buffer for validAfter (mirrors the SDK/facilitator window).
VALIDITY_BUFFER_SECONDS = 60
# Fallbacks only when the challenge omits extra.name / extra.version (USDT0 on X
# Layer always carries them, but a defensive default keeps signing deterministic).
DEFAULT_EIP712_NAME = "USD₮0"
DEFAULT_EIP712_VERSION = "1"
DEFAULT_TIMEOUT_SECONDS = 300


@runtime_checkable
class PaymentSigner(Protocol):
    """A callable that turns a decoded x402 challenge into a PAYMENT-SIGNATURE
    header value. Implementations receive the FULL challenge (including ``pay_to``)
    so a caller policy can veto before producing a signature."""

    def sign(self, challenge: PaymentChallenge) -> str: ...


class LocalEip3009Signer:
    """Signs EIP-3009 authorizations locally with a caller-supplied private key.

    The key is held only on this instance and is never logged or serialized. Use a
    throwaway/dedicated key; this object moves real funds when its signature is
    submitted to a live facilitator.
    """

    def __init__(self, private_key: str):
        if not private_key:
            raise ValueError("private_key is required")
        try:
            from eth_account import Account
        except ImportError as exc:  # pragma: no cover - import guard
            raise ImportError(
                "LocalEip3009Signer needs the optional dependency eth-account. "
                "Install it with: pip install 'tilla-sdk[signer]'"
            ) from exc
        self._account = Account.from_key(private_key)
        self._private_key = private_key

    @property
    def address(self) -> str:
        return self._account.address

    def sign(self, challenge: PaymentChallenge) -> str:
        from eth_account import Account

        chain_id = int(challenge.network.split(":")[1])
        name = challenge.eip712_name or DEFAULT_EIP712_NAME
        version = challenge.eip712_version or DEFAULT_EIP712_VERSION
        now = int(time.time())
        valid_after = now - VALIDITY_BUFFER_SECONDS
        valid_before = now + (challenge.max_timeout_seconds or DEFAULT_TIMEOUT_SECONDS)
        nonce_bytes = os.urandom(32)
        nonce_hex = "0x" + nonce_bytes.hex()
        value = challenge.amount_micro

        signed = Account.sign_typed_data(
            self._private_key,
            domain_data={
                "name": name,
                "version": version,
                "chainId": chain_id,
                "verifyingContract": challenge.asset,
            },
            message_types=TRANSFER_WITH_AUTHORIZATION_TYPES,
            message_data={
                "from": self._account.address,
                "to": challenge.pay_to,
                "value": value,
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": nonce_bytes,
            },
        )
        payload = {
            "authorization": {
                "from": self._account.address,
                "to": challenge.pay_to,
                "value": str(value),
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": nonce_hex,
            },
            "signature": "0x" + bytes(signed.signature).hex(),
        }
        return encode_payment_signature(challenge.accepted_raw, payload)
