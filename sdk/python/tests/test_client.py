"""TillaClient tests — every method against respx-mocked httpx (zero network,
zero funds, zero real keys). Includes the x402 funds-safety matrix: pin/cap
mismatches refuse BEFORE the signer is reached, and a transport failure after
signing raises SettlementUnknown with exactly one signed replay and no re-sign.
"""

import base64
import json

import httpx
import pytest
import respx

from tilla_sdk import (
    Checkout,
    PaymentRefused,
    SettlementUnknown,
    TillaClient,
    TillaError,
)

BASE = "https://tilla.test"
USDT0 = "0x779ded0c9e1022225f8e0630b35a9b54be713736"
PAY_TO = "0x" + "22" * 20

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"

# A real x402-encoded settle response (see test_codec) — tx = 0xabab...
SETTLE_FIXTURE = (
    "eyJzdWNjZXNzIjp0cnVlLCJwYXllciI6IjB4MTExMTExMTExMTExMTExMTExMTExMTExMTExMTEx"
    "MTExMTExMTExMSIsInRyYW5zYWN0aW9uIjoiMHhhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFi"
    "YWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiIiwibmV0d29yayI6ImVpcDE1NTox"
    "OTYifQ=="
)
SETTLE_TX = "0x" + "ab" * 32


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _accept(**over):
    a = {
        "scheme": "exact",
        "network": "eip155:196",
        "asset": USDT0,
        "amount": "1000000",
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USD₮0", "version": "1"},
    }
    a.update(over)
    return a


def _required(*accepts) -> str:
    return _b64({"x402Version": 2, "accepts": list(accepts)})


class FakeSigner:
    """Records every challenge it is asked to sign and returns a static header."""

    def __init__(self):
        self.calls = []

    def sign(self, challenge):
        self.calls.append(challenge)
        return "FAKE-SIGNATURE-HEADER"


def _client() -> TillaClient:
    return TillaClient(base_url=BASE, timeout=2.0)


# ============================================================ read surface
@respx.mock
def test_discovery_parses():
    respx.get(f"{BASE}/discovery/resources").mock(
        return_value=httpx.Response(
            200,
            json={
                "service": "tilla",
                "total": 2,
                "resources": [
                    {
                        "slug": "alpha",
                        "name": "Alpha",
                        "description": "d",
                        "url": "/s/alpha/",
                        "feed": "/s/alpha/feed.json",
                        "buy": "/s/alpha/buy",
                        "mcp": "/s/alpha/mcp",
                        "price_min_micro": 1000000,
                        "price_max_micro": 9000000,
                        "currency": "USDT",
                        "network": "eip155:196",
                        "sold_count": 3,
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            },
        )
    )
    with _client() as c:
        d = c.discovery(limit=10, offset=0)
    assert d.total == 2
    assert d.resources[0].slug == "alpha"
    assert d.resources[0].sold_count == 3


@respx.mock
def test_search_parses():
    respx.get(f"{BASE}/discovery/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "resources": [{"slug": "beta", "name": "Beta", "buy": "/s/beta/buy"}]
            },
        )
    )
    with _client() as c:
        rows = c.search("beta")
    assert len(rows) == 1 and rows[0].slug == "beta"


@respx.mock
def test_feed_parses_products():
    respx.get(f"{BASE}/s/alpha/feed.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "store": {
                    "slug": "alpha",
                    "name": "Alpha",
                    "description": "d",
                    "url": "https://tilla.test/s/alpha/",
                },
                "products": [
                    {
                        "id": "7",
                        "title": "Thing",
                        "description": "a thing",
                        "link": "https://tilla.test/s/alpha/",
                        "price": {"amount": "9.00", "currency": "USDT"},
                        "availability": "in_stock",
                        "x402": {
                            "endpoint": "/s/alpha/buy",
                            "network": "eip155:196",
                            "asset": USDT0,
                            "schemes": ["exact"],
                        },
                    }
                ],
            },
        )
    )
    with _client() as c:
        feed = c.feed("alpha")
    assert feed.name == "Alpha"
    assert feed.products[0].price_amount == "9.00"
    assert feed.products[0].schemes == ["exact"]


@respx.mock
def test_llms_txt_and_agent_card():
    respx.get(f"{BASE}/s/alpha/llms.txt").mock(
        return_value=httpx.Response(200, text="# Alpha\n")
    )
    respx.get(f"{BASE}/.well-known/agent-card.json").mock(
        return_value=httpx.Response(200, json={"name": "Tilla"})
    )
    with _client() as c:
        assert c.llms_txt("alpha").startswith("# Alpha")
        assert c.agent_card()["name"] == "Tilla"


# ============================================================ human checkout
@respx.mock
def test_create_checkout_and_status():
    respx.post(f"{BASE}/api/checkout/alpha").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "cid1",
                "pay_to": PAY_TO,
                "amount": 9.0,
                "amount_micro": 9000000,
                "expires_at": "2026-01-01T00:10:00Z",
                "network": "X Layer (chainId 196)",
                "token": "USDT",
            },
        )
    )
    respx.get(f"{BASE}/api/checkout/cid1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "cid1",
                "status": "created",
                "amount": 9.0,
                "amount_micro": 9000000,
                "pay_to": PAY_TO,
            },
        )
    )
    with _client() as c:
        chk = c.create_checkout("alpha", ref="0x" + "9" * 40)
        assert isinstance(chk, Checkout)
        assert chk.id == "cid1" and chk.amount_micro == 9000000
        st = c.checkout_status("cid1")
        assert st.status == "created" and not st.is_paid


@respx.mock
def test_wait_for_paid_polls_until_paid():
    responses = [
        httpx.Response(
            200,
            json={"id": "c", "status": "created", "amount_micro": 1, "pay_to": PAY_TO},
        ),
        httpx.Response(
            200,
            json={
                "id": "c",
                "status": "paid",
                "amount_micro": 1,
                "pay_to": PAY_TO,
                "tx_hash": SETTLE_TX,
            },
        ),
    ]
    respx.get(f"{BASE}/api/checkout/c").mock(side_effect=responses)
    with _client() as c:
        st = c.wait_for_paid("c", timeout=5, interval=0.01)
    assert st.is_paid and st.tx_hash == SETTLE_TX


@respx.mock
def test_wait_for_paid_raises_on_expiry():
    from tilla_sdk import CheckoutExpired

    respx.get(f"{BASE}/api/checkout/c").mock(
        return_value=httpx.Response(
            200,
            json={"id": "c", "status": "expired", "amount_micro": 1, "pay_to": PAY_TO},
        )
    )
    with _client() as c, pytest.raises(CheckoutExpired):
        c.wait_for_paid("c", timeout=1, interval=0.01)


# ============================================================ MCP
@respx.mock
def test_mcp_call_returns_structured_content():
    respx.post(f"{BASE}/s/alpha/mcp").mock(
        return_value=httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "{}"}],
                    "structuredContent": {"products": [{"id": "1"}]},
                },
            },
        )
    )
    with _client() as c:
        out = c.mcp_call("alpha", "list_products", {})
    assert out == {"products": [{"id": "1"}]}


@respx.mock
def test_mcp_call_raises_on_error():
    respx.post(f"{BASE}/s/alpha/mcp").mock(
        return_value=httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32602, "message": "bad"},
            },
        )
    )
    with _client() as c, pytest.raises(TillaError):
        c.mcp_call("alpha", "nope", {})


# ============================================================ x402 buy (happy)
@respx.mock
def test_buy_happy_path_signs_once_and_returns_purchase():
    def handler(request):
        if PAYMENT_SIGNATURE_HEADER.lower() in request.headers:
            return httpx.Response(
                200,
                json={
                    "order_id": "o1",
                    "amount_micro": 1000000,
                    "delivery": "here is your file",
                    "download_url": "https://tilla.test/api/download/tok",
                },
                headers={PAYMENT_RESPONSE_HEADER: SETTLE_FIXTURE},
            )
        return httpx.Response(
            402, headers={PAYMENT_REQUIRED_HEADER: _required(_accept())}
        )

    route = respx.post(f"{BASE}/s/alpha/buy").mock(side_effect=handler)
    signer = FakeSigner()
    with _client() as c:
        purchase = c.buy("alpha", signer=signer, max_amount_micro=2000000)

    assert purchase.order_id == "o1"
    assert purchase.settle_tx == SETTLE_TX
    assert purchase.download_url.endswith("/tok")
    assert route.call_count == 2  # probe + one signed replay
    assert len(signer.calls) == 1
    # The signer saw the FULL challenge, including pay_to (the merchant wallet).
    assert signer.calls[0].pay_to == PAY_TO
    assert signer.calls[0].amount_micro == 1000000


@respx.mock
def test_create_store_happy_path():
    def handler(request):
        if PAYMENT_SIGNATURE_HEADER.lower() in request.headers:
            return httpx.Response(
                200,
                json={
                    "slug": "new-store",
                    "url": "https://tilla.test/s/new-store/",
                    "manage_key": "mk-secret",
                    "store_name": "New Store",
                },
                headers={PAYMENT_RESPONSE_HEADER: SETTLE_FIXTURE},
            )
        return httpx.Response(
            402, headers={PAYMENT_REQUIRED_HEADER: _required(_accept())}
        )

    respx.post(f"{BASE}/create-store").mock(side_effect=handler)
    signer = FakeSigner()
    with _client() as c:
        created = c.create_store(
            "I sell socks", signer=signer, max_amount_micro=1000000
        )
    assert created.slug == "new-store"
    assert created.manage_key == "mk-secret"
    assert created.settle_tx == SETTLE_TX
    assert len(signer.calls) == 1


# ============================================================ x402 funds-safety
@pytest.mark.parametrize(
    "accept,cap",
    [
        (_accept(asset="0x" + "de" * 20), 2000000),  # wrong asset
        (_accept(scheme="weird"), 2000000),  # unknown scheme -> no exact match
        (_accept(network="eip155:1"), 2000000),  # wrong network -> no exact match
        (_accept(amount="9000000"), 1000000),  # amount over cap
    ],
)
@respx.mock
def test_buy_pin_mismatch_refuses_before_signing(accept, cap):
    route = respx.post(f"{BASE}/s/alpha/buy").mock(
        return_value=httpx.Response(
            402, headers={PAYMENT_REQUIRED_HEADER: _required(accept)}
        )
    )
    signer = FakeSigner()
    with _client() as c, pytest.raises(PaymentRefused):
        c.buy("alpha", signer=signer, max_amount_micro=cap)
    assert signer.calls == []  # signer PROVABLY never called
    assert route.call_count == 1  # only the unpaid probe; no signed request


@respx.mock
def test_buy_missing_payment_required_header_refuses():
    respx.post(f"{BASE}/s/alpha/buy").mock(return_value=httpx.Response(402))
    signer = FakeSigner()
    with _client() as c, pytest.raises(PaymentRefused):
        c.buy("alpha", signer=signer, max_amount_micro=2000000)
    assert signer.calls == []


@respx.mock
def test_buy_transport_failure_after_signing_is_settlement_unknown():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(
                402, headers={PAYMENT_REQUIRED_HEADER: _required(_accept())}
            )
        raise httpx.ConnectError("connection dropped after signing")

    route = respx.post(f"{BASE}/s/alpha/buy").mock(side_effect=handler)
    signer = FakeSigner()
    with _client() as c, pytest.raises(SettlementUnknown):
        c.buy("alpha", signer=signer, max_amount_micro=2000000)
    assert len(signer.calls) == 1  # signed exactly once
    assert route.call_count == 2  # probe + one paid attempt, never re-fired


@respx.mock
def test_buy_signer_exception_maps_to_payment_refused():
    respx.post(f"{BASE}/s/alpha/buy").mock(
        return_value=httpx.Response(
            402, headers={PAYMENT_REQUIRED_HEADER: _required(_accept())}
        )
    )

    class BoomSigner:
        def sign(self, challenge):
            raise RuntimeError("policy vetoed")

    with _client() as c, pytest.raises(PaymentRefused):
        c.buy("alpha", signer=BoomSigner(), max_amount_micro=2000000)
