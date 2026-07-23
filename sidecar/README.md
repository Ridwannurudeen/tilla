# Subscription sidecar prototype (x402 `period` scheme)

A minimal Express sidecar that speaks the OKX x402 **subscription** ("period")
scheme using the published JS SDK. It builds a real 402 subscription challenge
and dry-runs the facilitator subscribe call — **without any OKX credentials and
without ever contacting the facilitator**.

## Spike verdict (Phase A)

The subscription scheme **exists** in the shipped JS SDK.

| What | Where (installed dist) |
|---|---|
| `PermitSubscriptionScheme` (`scheme = "period"`, `settlementMode = "pre"`) | `@okxweb3/app-x402-evm@0.2.0` → `dist/cjs/subscription/index.d.ts` (subpath export `@okxweb3/app-x402-evm/subscription`) |
| `AccessProofVerifier` | same file |
| Subscription types, codec, EIP-712 builders, `InMemoryStore`, `SubscriptionClient` | `@okxweb3/app-x402-core@0.2.1` → `dist/cjs/subscription/index.d.ts` (subpath `@okxweb3/app-x402-core/subscription`) |
| `OKXFacilitatorClient` (implements `SubscriptionFacilitatorClient`) | `@okxweb3/app-x402-core` → top-level export (`dist/cjs/index.js`) |
| EIP-712 domain `A2APaySubscription`, version `1`, contract `0x3b01…8032` | `@okxweb3/app-x402-evm` `SUBSCRIPTION_DOMAIN_NAME` / `SUBSCRIPTION_DOMAIN_VERSION` / `SUBSCRIPTION_CONTRACT_ADDRESS` |
| Facilitator REST endpoints `/api/v6/pay/x402/subscriptions{,/change,/charge,/cancel,/cancel-pending-change,/charges,/detail,/finalize-expired,/pending}` | `@okxweb3/app-x402-core` `dist/cjs/index.js` (OKXFacilitatorClient methods) |

The pre-verified expectation was **confirmed**: `PermitSubscriptionScheme`,
EIP-712 domain `A2APaySubscription` with `settlementMode: "pre"`, and the
facilitator `/subscriptions*` REST family are all present, exactly as expected.

Note: `@okxweb3/x402-express@0.1.1` and its dep `@okxweb3/x402-core@0.1.0` are
the older non-subscription middleware — they have no subscription support. The
subscription work lives entirely in the `app-x402-*@0.2.x` packages.

### EIP-712 `SubscriptionTerms` type (verbatim from the dist)

```
SubscriptionTerms: [
  payer address, merchant address, facilitator address, token address,
  amountPerPeriod uint160, periodSec uint64, maxPeriods uint32, startAt uint64,
  initialChargePeriods uint32, initialChargeAmount uint160, termsDeadline uint64,
  permitHash bytes32, salt bytes32, planTier uint8, changeFromSubId bytes32,
  changeEffectiveAt uint8, periodMode uint8
]
```

Domain: `{ name: "A2APaySubscription", version: "1", chainId, verifyingContract: <subscriptionContract> }`.

## Status: in-repo, proxied, deployed dormant (M8)

This sidecar now lives in the Tilla repo at `sidecar/` and is fronted by the
FastAPI proxy `app/subscriptions.py` (`POST /s/{slug}/subscribe`) and run by the
systemd unit `deploy/tilla-sidecar.service` (127.0.0.1:8790, never nginx-exposed).
It is **deployed dormant**: the proxy 503s until `TILLA_SUBSCRIPTIONS_ENABLED=1`,
and the sidecar's two creds-gated routes below hard-refuse (503) until OKX creds
are present. A real subscribe/charge additionally needs a **USER-funded Permit2
buyer**; until a real settle tx is logged, no subscription claim is made anywhere.

## Prototype routes

`server.js` exposes four routes. The `challenge`/`verify` pair NEVER calls the
facilitator; `health/creds` and `subscriptions/settle` contact it ONLY when OKX
creds are present in the env, and 503 otherwise.

### `POST /subscriptions/challenge`

Builds a 402 subscription challenge with the SDK's server-side
`PermitSubscriptionScheme.enhancePaymentRequirements`. The facilitator
address + contracts + EIP-712 domain are injected from a **synthesized
`supportedKind`** (normally the cached OKX `/supported` response) so no network
call is made.

Request body:

```json
{ "payTo": "0x…", "amount": "5000000", "period": 2592000,
  "maxPeriods": 12, "plan": { "id": "pro-monthly", "tier": 2, "name": "Pro Monthly" } }
```

- `amount` — atomic units per period, decimal string (USDT0 is 6dp → `"5000000"` = 5 USDT0)
- `period` — seconds per billing period
- `maxPeriods` — optional, `0` = open-ended
- `plan` — optional `{ id, tier, name }`

Returns HTTP `402` with `{ x402Version, accepts: [PaymentRequirements], error }`
and an `APP-PAYMENT-REQUIRED` base64 header, where `accepts[0].extra` carries
`contracts`, `facilitator`, and the `A2APaySubscription` `domain`.

### `POST /subscriptions/verify`

Decodes a buyer's `PAYMENT-SIGNATURE` header (`decodePaymentPayload` +
`asSubscriptionPaymentInner`), optionally runs the SDK's **local**
`scheme.verifySubscribe` (a pure terms↔requirements bind check, no network),
and prints the **exact body** `OKXFacilitatorClient.buildWriteBody` would POST
to `/api/v6/pay/x402/subscriptions` — then returns it as a dry-run. Nothing is
sent.

- Header: `PAYMENT-SIGNATURE: <base64>`
- Body (optional): `{ "requirements": <accepts[0]> }` — echo the challenge's
  requirements back to run the local `verifySubscribe`. If omitted, the network
  is recovered from the payload's embedded `accepted` requirements.

### `GET /health/creds` (creds-gated, read-only)

The orchestrator's JS-side read-only creds probe. **No OKX creds in env → `503`
`{ configured: false }`** and no facilitator call. With creds it constructs the
real `OKXFacilitatorClient` and calls `getSupported()` (reports whether `period`
is listed) plus a read-only `getSubscription()` on a nonexistent id: a structured
not-found means the creds authenticate against the subscriptions family
(funds-gated only); a `401` means creds-gated. Cannot move funds.

### `POST /subscriptions/settle` (creds-gated — the ONLY facilitator write)

**No OKX creds in env → `503`** (the committed default never contacts
`web3.okx.com`). With creds it decodes `PAYMENT-SIGNATURE`, runs the local
`verifySubscribe` (reject → `402`), then swaps the stub for a real
`OKXFacilitatorClient` and POSTs `subscribe(payload, requirements, syncSettle=true)`
to `/api/v6/pay/x402/subscriptions`. A facilitator failure → `502`; only a
facilitator success returns `{ settled: true, facilitator }`. A real success needs
a funded Permit2 buyer (USDT0 balance + Permit2 allowance) — USER-gated.

- Header: `PAYMENT-SIGNATURE: <base64>`; Body: `{ "requirements": <accepts[0]> }`.
- Env creds: `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE` (same as the API).

## Run it

```bash
npm install
node server.js            # standalone server on :8790
# or, full self-contained end-to-end demo (starts server, signs, verifies):
node sample-buyer.js
```

`sample-buyer.js` generates a **throwaway, unfunded testnet key** each run,
signs the Permit2 + SubscriptionTerms EIP-712 messages, encodes the
`PAYMENT-SIGNATURE`, and drives both routes.

### Verified output (real `node sample-buyer.js` run)

- `/subscriptions/challenge` → `402`, `accepts[0].extra.domain` =
  `{ name: "A2APaySubscription", version: "1", chainId: 196, verifyingContract: 0x3b01…8032 }`,
  `contracts.subscription` = `0x3b01…8032`, `contracts.permit2` = `0x0000…8BA3`.
- Buyer signs, `PAYMENT-SIGNATURE` ≈ 2720-char base64.
- `/subscriptions/verify` → `200`, `localVerify: { ok: true }`, and the printed
  facilitator dry-run body (`chainIndex: 196`, full `terms`, `permit`,
  `termsSig`, `permitSig`, `syncSettle: true`) targeting
  `POST https://web3.okx.com/api/v6/pay/x402/subscriptions`.

Standalone `curl` also verified: valid input → `402` + `APP-PAYMENT-REQUIRED`
header; missing `payTo`/`amount`/`period` → `400`.

## What works vs. what needs real OKX creds

**Works now, no creds:**
- Building the 402 subscription challenge (correct scheme, domain, contracts).
- Decoding a buyer `PAYMENT-SIGNATURE`.
- Local `verifySubscribe` terms-bind check.
- Producing the exact facilitator subscribe request body (dry-run).

**Needs real OKX creds / approval (NOT done here):**
- `SIDECAR_FACILITATOR_ADDRESS` — the real facilitator EOA. Fetch it once from
  `GET /api/v6/pay/x402/supported` (via `OKXFacilitatorClient.getSupported()`)
  instead of the stubbed zero address. This is the only field in the challenge
  that is currently a placeholder.
- Actually settling a subscribe / charge / cancel — requires
  `OKXFacilitatorClient({ apiKey, secretKey, passphrase })` (OK-ACCESS-* HMAC
  auth) and swapping the stub facilitator for it in `server.js`.
- On-chain: the buyer must have USDT0 and a Permit2 allowance; the demo key is
  unfunded, so a real settle would fail balance/allowance checks.

## Config (env vars)

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `8790` | HTTP port |
| `SIDECAR_NETWORK` | `eip155:196` | CAIP-2 network (X Layer mainnet) |
| `SIDECAR_ASSET` | `0x779d…3736` | USDT0 (6dp) |
| `SIDECAR_RESOURCE` | tilla x402 url | `resource` field in the challenge |
| `SIDECAR_FACILITATOR_ADDRESS` | zero addr | facilitator EOA (stub; get from `/supported`) |
| `SIDECAR_SUBSCRIPTION_CONTRACT` | `0x3b01…8032` | subscription contract |
| `SIDECAR_PERMIT2_CONTRACT` | `0x0000…8BA3` | Permit2 |
| `OKX_FACILITATOR_BASE_URL` | `https://web3.okx.com` | printed in dry-run URL only |

## Integration contract for a FastAPI proxy

A FastAPI (or any) proxy would sit in front of this sidecar and:

1. **On a gated request with no payment** → call
   `POST /subscriptions/challenge { payTo, amount, period, maxPeriods?, plan? }`,
   relay the sidecar's `402` body + `APP-PAYMENT-REQUIRED` header to the client
   unchanged.
2. **On a request carrying `PAYMENT-SIGNATURE`** → call
   `POST /subscriptions/verify` with that header (and the original
   `requirements` in the JSON body). Read `localVerify.ok`:
   - `false` → reject (`402`/`400`), surface `localVerify.error`.
   - `true` → the request is well-formed; the proxy then decides whether to
     settle. To settle for real, the sidecar needs an OKX-cred-backed
     `settleSubscribe` route (not in this prototype) that swaps the stub
     facilitator for `OKXFacilitatorClient`. The `facilitatorDryRun` body shown
     here is exactly what that settle call would send.

Response shapes are stable JSON: challenge returns
`{ x402Version, accepts, error }`; verify returns
`{ decoded, localVerify, facilitatorDryRun, note }`.

## Files

- `server.js` — the sidecar (both routes, stub facilitator, env config)
- `sample-buyer.js` — self-contained end-to-end demo / test driver
- `package.json` — deps: `@okxweb3/app-x402-core`, `@okxweb3/app-x402-evm`,
  `@okxweb3/x402-express` (unused; installed for the spike), `express`, `viem`
  (transitive, used by the sample)

## Store-page browser flow (prepare / encode) + real-facilitator env

Two helper routes let a bundler-less store page drive the buyer signing (the SDK's
EIP-712 builders can't run in-page): `POST /subscriptions/prepare` returns the two
typed-data envelopes (Permit2 + SubscriptionTerms) built from the server-owned
challenge; `POST /subscriptions/encode` assembles the `PAYMENT-SIGNATURE` from the
buyer's two signatures. The browser only does native `eth_signTypedData_v4`. The
public proxy (`/s/{slug}/subscribe/{prepare,encode}` in `app/subscriptions.py`)
resolves the store's terms server-side and reads the buyer's on-chain Permit2 nonce.

**`/subscriptions/settle` fail-closed:** the OKX facilitator returns a 200 body even
on failure (e.g. `code 30001 "max_periods_invalid"`), so a non-throwing call is NOT
proof of settlement. The route now delivers only on an OKX success code (`"0"`); any
other code is a rejection (402), never a false settle.

**Real-facilitator env (required for on-chain settle).** The default challenge uses
the SDK's placeholder subscription contract; the live X Layer facilitator uses its
own. Set these (from the facilitator `/supported` `period` kind) in the sidecar env
so the EIP-712 domain binds to the real contract:

```
SIDECAR_SUBSCRIPTION_CONTRACT=0xe9e4529d2af54de1078424e495c620d23f4432cc
SIDECAR_FACILITATOR_ADDRESS=0x2c0f34506e6a825b1d27383eee28980ad82a37ff
```
