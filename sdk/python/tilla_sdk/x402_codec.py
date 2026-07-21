"""x402 header codec + the decoded payment challenge.

BUILD-TIME VERIFICATION (recorded in the SDK README): the installed
``okxweb3-app-x402`` 0.1.0 wire format for PAYMENT-REQUIRED / PAYMENT-SIGNATURE /
PAYMENT-RESPONSE is plain ``base64(JSON)`` — its ``safe_base64_encode`` wraps a
``model_dump_json(by_alias=True, exclude_none=True)`` (camelCase). Because it is
not a richer envelope, this hand-rolled codec keeps the SDK dependency-light
(``httpx`` only) instead of taking ``okxweb3-app-x402`` as a runtime dependency.

Field names on the wire are camelCase (``payTo``, ``maxTimeoutSeconds``,
``x402Version``); the inner ``payload`` dict of a signature is passed through
verbatim, matching the production ``warden_hire`` payer byte-for-byte.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

# Tilla's single settlement rail — the SDK pins to it (see client.py).
EXACT_SCHEME = "exact"
X_LAYER_NETWORK = "eip155:196"
USDT0_ASSET = "0x779ded0c9e1022225f8e0630b35a9b54be713736"


@dataclass
class PaymentChallenge:
    """One decoded, exact-scheme x402 requirement, ready to pin-check and sign.

    ``accepted_raw`` is the original requirement dict from the server's challenge;
    it is echoed back verbatim as the ``accepted`` field of the PAYMENT-SIGNATURE
    payload so the facilitator settles against exactly what it advertised.
    """

    scheme: str
    network: str
    asset: str
    amount_micro: int
    pay_to: str
    max_timeout_seconds: int
    eip712_name: str | None
    eip712_version: str | None
    accepted_raw: dict[str, Any] = field(repr=False)
    resource: dict[str, Any] | None = None


def _b64encode_json(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("utf-8")


def _b64decode_json(value: str) -> Any:
    return json.loads(base64.b64decode(value.encode("utf-8")).decode("utf-8"))


def decode_payment_required(header_value: str) -> dict[str, Any]:
    """Decode a PAYMENT-REQUIRED header into its raw dict (``accepts`` etc.)."""
    data = _b64decode_json(header_value)
    if not isinstance(data, dict):
        raise ValueError("PAYMENT-REQUIRED did not decode to an object")
    return data


def select_requirement(
    required: dict[str, Any], scheme: str, network: str
) -> PaymentChallenge | None:
    """Return the first requirement matching ``scheme`` + ``network``, or None.

    Raises ``ValueError`` only if a matching entry is malformed (non-integer
    amount) — an absent match is a clean None so the caller can refuse cleanly.
    """
    resource = required.get("resource") if isinstance(required, dict) else None
    for accept in required.get("accepts") or []:
        if not isinstance(accept, dict):
            continue
        if accept.get("scheme") != scheme or str(accept.get("network")) != network:
            continue
        extra = accept.get("extra") or {}
        return PaymentChallenge(
            scheme=accept.get("scheme", ""),
            network=str(accept.get("network", "")),
            asset=accept.get("asset", ""),
            amount_micro=int(accept["amount"]),
            pay_to=accept.get("payTo", ""),
            max_timeout_seconds=int(accept.get("maxTimeoutSeconds", 0) or 0),
            eip712_name=extra.get("name"),
            eip712_version=extra.get("version"),
            accepted_raw=accept,
            resource=resource if isinstance(resource, dict) else None,
        )
    return None


def encode_payment_signature(
    accepted_raw: dict[str, Any], payload: dict[str, Any]
) -> str:
    """Encode a PAYMENT-SIGNATURE header value.

    ``payload`` is the scheme payload (``{"authorization": {...}, "signature":
    "0x.."}``); ``accepted_raw`` is echoed back verbatim as ``accepted``. The wire
    shape mirrors x402's PaymentPayload exactly.
    """
    return _b64encode_json(
        {
            "x402Version": 2,
            "payload": payload,
            "accepted": accepted_raw,
        }
    )


def decode_payment_response(header_value: str) -> dict[str, Any]:
    """Decode a PAYMENT-RESPONSE header into its raw SettleResponse dict.

    The settle transaction hash is under ``transaction``.
    """
    data = _b64decode_json(header_value)
    if not isinstance(data, dict):
        raise ValueError("PAYMENT-RESPONSE did not decode to an object")
    return data


def settle_tx_from_response(header_value: str | None) -> str | None:
    """Best-effort settle tx hash from a PAYMENT-RESPONSE header, or None if the
    header is absent/undecodable (the payment still settled on a served 200)."""
    if not header_value:
        return None
    try:
        return decode_payment_response(header_value).get("transaction") or None
    except (ValueError, KeyError):
        return None
