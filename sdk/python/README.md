# tilla-sdk

A sync, typed, dependency-light Python client for the [Tilla](https://tilla.gudman.xyz)
storefront API. It wraps **only endpoints that are live in production today** and
mirrors the one x402 payer pattern already proven on the server (`warden_hire`).

- **Browse + human checkout** need nothing but `httpx`.
- **The x402 pay paths** (`create_store`, `buy`) additionally need a caller-supplied
  **signer**. The shipped `LocalEip3009Signer` needs the optional `[signer]` extra
  (`eth-account`).
- **The SDK never bundles, reads, defaults, or logs a private key.** You construct
  the signer from your own env/keystore/remote wallet service.

This package lives under `sdk/python/` in the Tilla repo and is **never** part of
the server deploy (the app package stays `["app"]`; `deploy.sh` never ships `sdk/`).

## Install

```bash
pip install tilla-sdk            # browse + human checkout (httpx only)
pip install 'tilla-sdk[signer]'  # + LocalEip3009Signer (adds eth-account)
```

Requires Python 3.10+.

## (a) Zero-funds browse + human checkout

No key, no funds. You open an order and a human pays it from their own wallet.

```python
from tilla_sdk import TillaClient

with TillaClient() as client:
    disc = client.discovery(limit=5)          # GET /discovery/resources
    hits = client.search("template")          # GET /discovery/search
    feed = client.feed("some-slug")           # GET /s/{slug}/feed.json (typed)
    card = client.agent_card()                # GET /.well-known/agent-card.json

    checkout = client.create_checkout("some-slug")   # POST /api/checkout/{slug}
    print(checkout.pay_to, checkout.amount_micro, checkout.expires_at)
    # ... a human sends USDT0 to pay_to, then:
    status = client.submit_tx(checkout.id, "0x<tx hash>")   # POST /.../tx
    # or poll (honors the 40/min status limit):
    status = client.wait_for_paid(checkout.id, timeout=900, interval=5)
```

Runnable, no key required:

```bash
python examples/browse_and_checkout.py            # discovers a store
python examples/browse_and_checkout.py --slug foo
```

## (b) MCP tool surface

For frameworks without an MCP client, `mcp_call` is a thin JSON-RPC 2.0 helper over
`/s/{slug}/mcp` (`list_products` / `get_product` / `create_checkout` / `pay`):

```python
with TillaClient() as client:
    products = client.mcp_call("some-slug", "list_products", {})
    order = client.mcp_call("some-slug", "create_checkout", {"product_id": 1})
```

## (c) The x402 buy — THIS MOVES REAL FUNDS

`buy` and `create_store` spend real USDT0 on X Layer. **You supply and control the
key; the SDK enforces sign-once + an amount cap and pin-checks the challenge.**

The signer is a hook — any object with `def sign(self, challenge: PaymentChallenge)
-> str`. It receives the **full decoded challenge (including `pay_to`)** so your
policy can veto before a signature exists. The shipped local signer:

```python
import os
from tilla_sdk import TillaClient, LocalEip3009Signer

signer = LocalEip3009Signer(os.environ["TILLA_BUYER_KEY"])  # your key, your control

with TillaClient() as client:
    purchase = client.buy(
        "some-slug",
        signer=signer,
        max_amount_micro=1_000_000,   # MANDATORY cap (1 USDT — well above the 0.05 fee)
    )
    print(purchase.order_id, purchase.settle_tx, purchase.delivery)

    created = client.create_store(
        "I sell a Notion productivity template for $9",
        signer=signer,
        max_amount_micro=1_000_000,
    )
    print(created.slug, created.url, created.manage_key, created.settle_tx)
```

### Funds-safety contract (mirrors the production `warden_hire` payer)

1. **Refuse before signing.** The 402 challenge is pin-checked — scheme `exact`,
   network `eip155:196`, asset USDT0 (`0x779ded…3736`), and amount ≤ your
   `max_amount_micro` — **before** the signer is ever called. Any mismatch raises
   `PaymentRefused` and no signature is produced.
   - `buy` cannot pin `pay_to` statically (it is the per-store merchant wallet, and
     the feed does publish it, so
     pin it per-store from the feed if you want a static pin). The guards are the mandatory cap + the
     asset/network/scheme pins + surfacing the full challenge (incl. `pay_to`) to
     your signer so your policy can veto.
2. **Sign at most once.** Exactly one authorization is produced and sent.
3. **Never re-fire a signed authorization.** A transport failure *after* signing
   raises `SettlementUnknown` — the outcome on-chain is unknown, so reconcile
   out-of-band (check the store/order); **do not re-pay**.

Runnable, real funds, interactive confirm, never in CI:

```bash
export TILLA_BUYER_KEY=0x<your funded X Layer key>
python examples/agent_buy.py --slug some-slug --max-usdt 1.0
```

## x402 header codec

`tilla_sdk.x402_codec` encodes/decodes the PAYMENT-REQUIRED / PAYMENT-SIGNATURE /
PAYMENT-RESPONSE headers.

**Build-time verification (decision recorded):** the installed `okxweb3-app-x402`
0.1.0 wire format is plain `base64(JSON)` (its `safe_base64_encode` wraps a
camelCase `model_dump_json`). Because it is **not** a richer envelope, the SDK keeps
a small hand-rolled codec and stays dependency-light (`httpx` only) rather than
taking `okxweb3-app-x402` as a runtime dependency. The codec tests decode **real
x402-encoded fixtures** to prove wire compatibility.

## XSS / output note

Responses are returned as typed Python objects. If you paste any server-provided
text (store copy, growth-kit strings, delivery messages) into HTML you control,
**escape it yourself** — the SDK does not render HTML.

## Tests

```bash
pip install -e '.[dev]'
pytest            # respx-mocked; zero network, zero funds, throwaway keys only
```
