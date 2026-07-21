"""LocalEip3009Signer tests: the emitted authorization must recover to the signer
and match the warden_hire type struct / domain byte-for-byte (eth_account as the
oracle). A fixed throwaway key is used — no real funds, no network."""

import base64
import json
import time

from eth_account import Account
from eth_account.messages import encode_typed_data

from tilla_sdk.signing import (
    TRANSFER_WITH_AUTHORIZATION_TYPES,
    VALIDITY_BUFFER_SECONDS,
    LocalEip3009Signer,
)
from tilla_sdk.x402_codec import PaymentChallenge

# Throwaway key — public knowledge, holds nothing.
TEST_KEY = "0x" + "11" * 32
ASSET = "0x779ded0c9e1022225f8e0630b35a9b54be713736"
PAY_TO = "0x" + "22" * 20


def _challenge(amount=1_000_000):
    accepted_raw = {
        "scheme": "exact",
        "network": "eip155:196",
        "asset": ASSET,
        "amount": str(amount),
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USD₮0", "version": "1"},
    }
    return PaymentChallenge(
        scheme="exact",
        network="eip155:196",
        asset=ASSET,
        amount_micro=amount,
        pay_to=PAY_TO,
        max_timeout_seconds=300,
        eip712_name="USD₮0",
        eip712_version="1",
        accepted_raw=accepted_raw,
    )


def test_type_struct_matches_warden_hire():
    # The exact struct app/warden_hire.py signs — a drift here breaks settlement.
    assert TRANSFER_WITH_AUTHORIZATION_TYPES == {
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ]
    }


def test_signature_recovers_to_signer_address():
    signer = LocalEip3009Signer(TEST_KEY)
    acct = Account.from_key(TEST_KEY)
    assert signer.address == acct.address

    before = int(time.time())
    header = signer.sign(_challenge())
    after = int(time.time())

    payload = json.loads(base64.b64decode(header))
    auth = payload["payload"]["authorization"]
    signature = payload["payload"]["signature"]

    # Wire shape mirrors warden_hire's ExactEIP3009Payload.
    assert auth["from"] == acct.address
    assert auth["to"] == PAY_TO
    assert auth["value"] == "1000000"
    assert auth["nonce"].startswith("0x") and len(auth["nonce"]) == 66
    # validBefore = now + maxTimeoutSeconds; validAfter = now - 60s buffer.
    assert (
        int(auth["validBefore"]) - int(auth["validAfter"])
        == 300 + VALIDITY_BUFFER_SECONDS
    )
    # validAfter carries the 60s clock-skew buffer.
    assert before - VALIDITY_BUFFER_SECONDS - 1 <= int(auth["validAfter"])
    assert int(auth["validAfter"]) <= after - VALIDITY_BUFFER_SECONDS + 1

    # Rebuild the exact typed data and recover — proves domain + struct + message
    # are internally consistent with a valid signature by the signer's key.
    msg = encode_typed_data(
        domain_data={
            "name": "USD₮0",
            "version": "1",
            "chainId": 196,
            "verifyingContract": ASSET,
        },
        message_types=TRANSFER_WITH_AUTHORIZATION_TYPES,
        message_data={
            "from": auth["from"],
            "to": auth["to"],
            "value": int(auth["value"]),
            "validAfter": int(auth["validAfter"]),
            "validBefore": int(auth["validBefore"]),
            "nonce": bytes.fromhex(auth["nonce"][2:]),
        },
    )
    recovered = Account.recover_message(msg, signature=signature)
    assert recovered == acct.address

    # The chosen requirement is echoed back verbatim.
    assert payload["accepted"]["payTo"] == PAY_TO


def test_signer_never_leaks_key_in_repr():
    signer = LocalEip3009Signer(TEST_KEY)
    assert TEST_KEY not in repr(signer)


def test_empty_key_rejected():
    import pytest

    with pytest.raises(ValueError):
        LocalEip3009Signer("")
