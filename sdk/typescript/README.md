# tilla-sdk (TypeScript)

A typed, dependency-light TypeScript client for the [Tilla](https://tilla.gudman.xyz)
storefront API. It wraps **only endpoints that are live in production today** and is
a faithful port of the shipped Python [`tilla-sdk`](../python) — same client surface,
same x402 wire format, and the same funds-safety invariants proven on the server
(`warden_hire`).

- **Browse + human checkout** need nothing but the platform `fetch` (Node 18+ or any
  modern browser).
- **The x402 pay paths** (`createStore`, `buy`) additionally need a caller-supplied
  **signer**. The shipped `LocalEip3009Signer` signs locally with `viem`.
- **The SDK never bundles, reads, defaults, or logs a private key.** You construct
  the signer from your own env/keystore/EIP-1193 wallet/remote service.

This package lives under `sdk/typescript/` in the Tilla repo and is **never** part of
the server deploy.

## Why `viem` (the one runtime dependency)

The x402 pay paths sign an EIP-712 `TransferWithAuthorization` (EIP-3009). Two
options were considered:

1. **`viem`** — the modern standard for EVM signing. `privateKeyToAccount().signTypedData`
   produces the exact EIP-712 digest with a well-audited implementation, and
   `recoverTypedDataAddress` gives the test oracle. Its `Account` abstraction also
   maps cleanly onto an EIP-1193 wallet, which is how a browser caller supplies key
   control.
2. **Hand-rolled EIP-712 over `@noble/*`** — smaller, but re-implements struct
   hashing / domain separators / secp256k1 by hand. For a path that moves real funds,
   re-deriving the digest by hand is exactly where a byte-level drift silently breaks
   settlement.

**Decision: `viem`.** For a funds-moving signature the audited, standard
implementation wins over a marginally smaller dependency. The signer itself is a
plug — a caller who wants zero deps can implement `PaymentSigner` against an
EIP-1193 provider (`eth_signTypedData_v4`) and never import `viem`.

The x402 **codec** stays hand-rolled (no wire-library dependency): the production
`okxweb3-app-x402` format is plain `base64(JSON)`, and the codec tests decode the
**same real x402-encoded fixtures** the Python SDK uses, proving both ports are
wire-compatible with the facilitator.

## Install

```bash
npm install tilla-sdk
```

Requires Node 18+ (or a browser with `fetch`, `crypto`, and `TextEncoder`).

## (a) Zero-funds browse + human checkout

No key, no funds. You open an order and a human pays it from their own wallet.

```ts
import { TillaClient } from "tilla-sdk";

const client = new TillaClient();

const disc = await client.discovery(5); // GET /discovery/resources
const hits = await client.search("template"); // GET /discovery/search
const feed = await client.feed("some-slug"); // GET /s/{slug}/feed.json (typed)
const card = await client.agentCard(); // GET /.well-known/agent-card.json

const checkout = await client.createCheckout("some-slug"); // POST /api/checkout/{slug}
console.log(checkout.payTo, checkout.amountMicro, checkout.expiresAt);
// ... a human sends USDT0 to payTo, then:
const status = await client.submitTx(checkout.id, "0x<tx hash>"); // POST /.../tx
// or poll (honors the 40/min status limit):
const paid = await client.waitForPaid(checkout.id, 900_000, 5_000);
```

Runnable, no key required:

```bash
npx tsx examples/browse_and_checkout.ts            # discovers a store
npx tsx examples/browse_and_checkout.ts --slug foo
```

## (b) MCP tool surface

For frameworks without an MCP client, `mcpCall` is a thin JSON-RPC 2.0 helper over
`/s/{slug}/mcp` (`list_products` / `get_product` / `create_checkout` / `pay`):

```ts
const products = await client.mcpCall("some-slug", "list_products", {});
const order = await client.mcpCall("some-slug", "create_checkout", { product_id: 1 });
```

## (c) The x402 buy — THIS MOVES REAL FUNDS

`buy` and `createStore` spend real USDT0 on X Layer. **You supply and control the
key; the SDK enforces sign-once + an amount cap and pin-checks the challenge.**

The signer is a hook — any object with `sign(challenge: PaymentChallenge): string |
Promise<string>`. It receives the **full decoded challenge (including `payTo`)** so
your policy can veto before a signature exists. The shipped local signer:

```ts
import { TillaClient, LocalEip3009Signer } from "tilla-sdk";

const signer = new LocalEip3009Signer(process.env.TILLA_BUYER_KEY!); // your key, your control
const client = new TillaClient();

const purchase = await client.buy("some-slug", {
  signer,
  maxAmountMicro: 1_000_000, // MANDATORY cap (1 USDT here)
});
console.log(purchase.orderId, purchase.settleTx, purchase.delivery);

const created = await client.createStore("I sell a Notion template for $9", {
  signer,
  maxAmountMicro: 1_000_000,
});
console.log(created.slug, created.url, created.manageKey, created.settleTx);
```

### Funds-safety contract (mirrors the production `warden_hire` payer)

1. **Refuse before signing.** The 402 challenge is pin-checked — scheme `exact`,
   network `eip155:196`, asset USDT0 (`0x779ded…3736`), and amount ≤ your
   `maxAmountMicro` — **before** the signer is ever called. Any mismatch throws
   `PaymentRefused` and no signature is produced.
   - `buy` cannot pin `payTo` statically (it is the per-store merchant wallet, and
     feeds deliberately never leak it). The guards are the mandatory cap + the
     asset/network/scheme pins + surfacing the full challenge (incl. `payTo`) to your
     signer so your policy can veto.
2. **Sign at most once.** Exactly one authorization is produced and sent.
3. **Never re-fire a signed authorization.** A transport failure *after* signing
   throws `SettlementUnknown` — the outcome on-chain is unknown, so reconcile
   out-of-band (check the store/order); **do not re-pay**.

Runnable, real funds, interactive confirm, never in CI:

```bash
export TILLA_BUYER_KEY=0x<your funded X Layer key>
npx tsx examples/agent_buy.ts --slug some-slug --max-usdt 1.0
```

## x402 header codec

`tilla-sdk` re-exports the codec that encodes/decodes the PAYMENT-REQUIRED /
PAYMENT-SIGNATURE / PAYMENT-RESPONSE headers (`decodePaymentRequired`,
`selectRequirement`, `encodePaymentSignature`, `settleTxFromResponse`). The wire
format is plain `base64(JSON)`, camelCase field names, and the inner signature
`payload` is passed through verbatim so the facilitator settles against exactly what
it advertised.

## XSS / output note

Responses are returned as typed objects. If you paste any server-provided text
(store copy, growth-kit strings, delivery messages) into HTML you control, **escape
it yourself** — the SDK does not render HTML.

## Honesty note

The signer and payment paths are exercised only by **mocked-HTTP unit tests** (zero
network, zero real keys) and by author-run **self-funded** testing against
throwaway keys. This SDK makes **no claim** of a live end-to-end paid purchase — a
real on-chain buy against production is a user-gated action, not something this
package asserts on its own.

## Tests

```bash
npm install
npm run typecheck   # tsc --noEmit, strict, no `any`
npm test            # vitest; mocked fetch, zero network, zero funds, throwaway keys
```
