"""Codec golden tests against REAL okxweb3-app-x402 0.1.0 header fixtures.

The two fixtures below were produced by the installed x402 encoder
(``encode_payment_required_header`` / ``encode_payment_response_header``) for an
exact USDT0-on-X-Layer challenge and settle — so decoding them proves the SDK's
hand-rolled ``base64(JSON)`` codec is wire-compatible with the production
facilitator, with no runtime x402 dependency in the SDK.
"""

import base64
import json

import pytest

from tilla_sdk.x402_codec import (
    EXACT_SCHEME,
    USDT0_ASSET,
    X_LAYER_NETWORK,
    decode_payment_required,
    encode_payment_signature,
    select_requirement,
    settle_tx_from_response,
)

# base64(JSON) produced by x402.http.utils.encode_payment_required_header(...)
PAYMENT_REQUIRED_FIXTURE = (
    "eyJ4NDAyVmVyc2lvbiI6MiwiYWNjZXB0cyI6W3sic2NoZW1lIjoiZXhhY3QiLCJuZXR3b3JrIjoi"
    "ZWlwMTU1OjE5NiIsImFzc2V0IjoiMHg3NzlkZWQwYzllMTAyMjIyNWY4ZTA2MzBiMzVhOWI1NGJl"
    "NzEzNzM2IiwiYW1vdW50IjoiMTAwMDAwMCIsInBheVRvIjoiMHhmNGM5ZmEwN2YzYmI4NTI1NDdm"
    "ZGM0ZGY3YzFkOWZkOTk5MWNmYTUxIiwibWF4VGltZW91dFNlY29uZHMiOjMwMCwiZXh0cmEiOnsi"
    "bmFtZSI6IlVTROKCrjAiLCJ2ZXJzaW9uIjoiMSJ9fV19"
)
# base64(JSON) produced by encode_payment_response_header(SettleResponse(...))
PAYMENT_RESPONSE_FIXTURE = (
    "eyJzdWNjZXNzIjp0cnVlLCJwYXllciI6IjB4MTExMTExMTExMTExMTExMTExMTExMTExMTExMTEx"
    "MTExMTExMTExMSIsInRyYW5zYWN0aW9uIjoiMHhhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFi"
    "YWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiIiwibmV0d29yayI6ImVpcDE1NTox"
    "OTYifQ=="
)


def test_decode_and_select_exact_from_real_fixture():
    required = decode_payment_required(PAYMENT_REQUIRED_FIXTURE)
    challenge = select_requirement(required, EXACT_SCHEME, X_LAYER_NETWORK)
    assert challenge is not None
    assert challenge.scheme == "exact"
    assert challenge.network == "eip155:196"
    assert challenge.asset.lower() == USDT0_ASSET
    assert challenge.amount_micro == 1_000_000
    assert challenge.pay_to == "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"
    assert challenge.max_timeout_seconds == 300
    assert challenge.eip712_name == "USD₮0"
    assert challenge.eip712_version == "1"
    # accepted_raw is preserved verbatim for the signature echo-back.
    assert challenge.accepted_raw["payTo"] == challenge.pay_to


def test_select_requirement_none_on_scheme_or_network_mismatch():
    required = decode_payment_required(PAYMENT_REQUIRED_FIXTURE)
    assert select_requirement(required, "aggr_deferred", X_LAYER_NETWORK) is None
    assert select_requirement(required, EXACT_SCHEME, "eip155:1") is None


def test_settle_tx_from_real_response_fixture():
    assert settle_tx_from_response(PAYMENT_RESPONSE_FIXTURE) == "0x" + "ab" * 32


def test_settle_tx_none_when_header_absent_or_bad():
    assert settle_tx_from_response(None) is None
    assert settle_tx_from_response("not-base64-$$$") is None


def test_encode_payment_signature_wire_shape():
    required = decode_payment_required(PAYMENT_REQUIRED_FIXTURE)
    challenge = select_requirement(required, EXACT_SCHEME, X_LAYER_NETWORK)
    inner = {"authorization": {"from": "0xabc"}, "signature": "0xdeadbeef"}
    header = encode_payment_signature(challenge.accepted_raw, inner)
    decoded = json.loads(base64.b64decode(header))
    assert decoded["x402Version"] == 2
    assert decoded["payload"] == inner
    # The chosen requirement is echoed back verbatim as `accepted`.
    assert decoded["accepted"]["scheme"] == "exact"
    assert decoded["accepted"]["payTo"] == challenge.pay_to


def test_decode_rejects_non_object():
    bad = base64.b64encode(json.dumps([1, 2, 3]).encode()).decode()
    with pytest.raises(ValueError):
        decode_payment_required(bad)
