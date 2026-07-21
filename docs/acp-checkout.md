# M13 growth — ACP checkout, external feeds, embed, affiliates

Post-hackathon growth features. All additive and funds-safe: no existing route,
response shape, 402 challenge, `feed.json` byte, or theme contract changes when the
new features are unused. **Zero fund-moving code is introduced anywhere.**

---

## 1. ACP (Agentic Commerce Protocol) `/checkout_sessions`

Five ACP-standard per-store endpoints (spec 2026-04-17) on top of the proven M3 order
machinery, so a store is buyable via the OpenAI/Stripe agentic-commerce standard on
top of the OKX x402 rail.

| Method | Path | Purpose |
|---|---|---|
| POST | `/s/{slug}/checkout_sessions` | create (validates line items, allocates a unique-amount order) |
| GET | `/s/{slug}/checkout_sessions/{id}` | retrieve |
| POST | `/s/{slug}/checkout_sessions/{id}` | update (buyer info only) |
| POST | `/s/{slug}/checkout_sessions/{id}/complete` | complete |
| POST | `/s/{slug}/checkout_sessions/{id}/cancel` | cancel |

**Dormant by default.** Every endpoint returns `503` until `TILLA_ACP_ENABLED` is set
in `/opt/tilla/.env` (the MPP/subscriptions dormant-mount pattern). Flip the flag ON
**only after** a live smoke of the tx-hash complete path.

- **create** — validates the line items against the store's active products (a
  single-line-item digital store: exactly one item, quantity 1), allocates an order
  via `checkout.create_order`, and returns the ACP session shape with
  `status: ready_for_payment`, totals in USDT (micro → decimal string),
  `fulfillment: digital`, a custom x402 handler advert
  (`{provider: x402, network: eip155:196, asset: USDT0, endpoint: /s/{slug}/buy}`),
  and an on-chain fallback (`{provider: onchain_usdt0, pay_to, amount, expires_at}`).
  The `API-Version` header is echoed; `Idempotency-Key` is honored via
  `UNIQUE(store_id, idempotency_key)` (a replay returns the original session — the ACP
  requirement). The ACP-shape `affiliate` object maps to the affiliate attribution
  (§4).
- **complete — ships ONE mode:** the custom x402/USDT0 handler. The agent submits
  `payment_data: {provider: onchain_usdt0, tx_hash}`; Tilla runs it through the exact
  M3 `checkout.verify_txhash` (exact-amount, one-tx-one-order, quarantine, replay
  rules all inherited) and, on success, returns a `completed` session carrying the
  deliverable exactly like the human `/tx` path. A `≥400` never records a transfer, so
  a wrong-amount/contract/recipient tx settles nothing.
- **cancel** flips a still-unpaid (pending/expired) order to `canceled` and the session
  to `canceled`. An expired order surfaces as a `canceled` session on retrieve.
- **auth** — session ids are high-entropy; endpoints are rate-limited. Optional HMAC
  request-signature verification activates **only** when `TILLA_ACP_SIGNING_SECRET` is
  set. No card/token payment data is ever accepted or stored — x402 / on-chain only.

### Spike 9 (PARKED): x402-middleware complete mode
A second complete mode — registering `POST /s/:slug/checkout_sessions/:id/complete` on
the existing x402 paywall with resolvers keyed off the session row, so an unpaid
complete returns a real `402` the agent pays via `PAYMENT-SIGNATURE`, settling
non-custodially to the merchant — is **spike-gated and NOT shipped**. It is registered
only after its spike passes; until then the **tx-hash mode alone is what is claimed**.

### External listing (USER-OWNED, never claimed)
ChatGPT Instant-Checkout **listing** requires OpenAI merchant approval — a user-owned
external step. No "listed on ChatGPT/Instant Checkout" claim appears anywhere until a
real approval artifact exists.

---

## 2. External product feeds

Read-only export shapes generated from the same Store/Product rows as the M7
`feed.json` (which is untouched, still validated by `openapi.feed.yaml`).

| Path | Shape |
|---|---|
| `GET /s/{slug}/feed/openai.json` | OpenAI product-feed JSON (validated against `openapi.external-feeds.yaml`) |
| `GET /s/{slug}/feed/google.xml` | Google Merchant Center RSS 2.0 (`g:` namespace) |
| `GET /feeds/openai.json` | Tilla-wide aggregate across all live stores |

All three: **live-status-only** (404 for pending/blocked — never leak a pending
store), **no `pay_to` / merchant wallet / buyer / order data** in any body, nosniff +
5-min cache, rate-limited 60/min, zero outbound calls. The Google RSS XML-escapes
every text node (the sitemap precedent), so a hostile store name cannot inject markup.

**Price honesty:** `google.xml` emits `g:price` as `"N.NN USDT"`. GMC validation
requires an ISO-4217 currency; this is a **shape-correct export the merchant adapts**,
never a claim of a fake USD price.

### External listings (USER-OWNED, never claimed)
- **ChatGPT product-feed ingestion + Instant Checkout** — OpenAI merchant approval.
- **Perplexity merchant program** — enrollment.
- **Google Merchant Center** — a Merchant Center account + listing.

Each is a user-completed external step. No "listed on {surface}" claim appears until
that surface is actually live for the user.

---

## 3. Embed button (`/embed.js`)

A merchant drops one line on their own site:

```html
<script src="https://tilla.gudman.xyz/embed.js"
        data-tilla-store="SLUG" data-ref="0x.." async></script>
```

`assets/embed.js` is a committed, dependency-free script served by `GET /embed.js`
(`application/javascript`, nosniff, `Cache-Control public max-age=3600` + ETag). It
locates its own tag, validates the slug + optional ref against strict patterns
(silently no-ops on failure), renders a "Buy with USDT — Tilla" button inside an
attached **shadow DOM** (all text via `textContent` — zero innerHTML, zero eval), and
on click opens the hosted checkout in a **popup** at the hard-coded literal
`https://tilla.gudman.xyz/s/{slug}/?ref={ref}` — the base is a fixed constant, so an
attacker page can never redirect the buy flow. Popup (not iframe) deliberately: wallet
extensions inject unreliably into cross-origin iframes, and framing the checkout would
open a clickjacking class — store pages also gain `X-Frame-Options: SAMEORIGIN` via
nginx. CSP-safe: one external script, no eval/inline handlers, no external
fonts/CSS/fetches. The `data-ref` wires the embed into the affiliate ledger (§4).

---

## 4. Affiliate rev-share (attribution + ledger + verify-and-record payout)

Attribution identity = a bare EVM address (the referring agent's payout wallet). No
signup. Captured first-write-wins and immutable after order creation via: the web
checkout `ref` body field (themes forward `?ref=`), the agent x402 `?ref=` query
param, the MCP `create_checkout` `ref` arg, and the ACP `affiliate` object. Every
capture path validates against the EVM-address regex, lowercases, and rejects the zero
address.

**Accrual** is a pure DB ledger row (`affiliate_accruals`) written exactly at the
settled/delivered seam — `checkout.deliver` for web orders and
`agentic.record_settlement` for agent orders (so a voided/reaped agent order never
accrues). `accrued_micro = basis_micro * TILLA_AFFILIATE_BPS / 10000`
(default 200 = 2%). A **self-referral guard** (referrer == buyer `from_addr` or ==
store `pay_to`) writes no accrual and logs `affiliate.self_referral_rejected`. A full
M9 refund voids the accrual (`accrued → void`).

**Payout — DORMANT / USER-GATED, and stronger: NO fund-moving code exists at all.**
The payout is a manual on-chain USDT0 send the merchant/operator makes from their OWN
wallet. `POST /api/merchant/affiliates/{address}/payout` accepts a `tx_hash`, verifies
the on-chain Transfer to the referrer (USDT0 contract, status 1, exact amount ≤ owed)
via the existing `chain.py` receipt-decode path, records an `affiliate_payouts` row
with `UNIQUE(tx_hash, log_index)` (the Refund idempotency pattern), and flips covered
accruals to `paid`. **Verify-and-record only** — byte-identical philosophy to the M9
non-custodial refunds. There is no signer, no private key, and no fund-moving call in
`app/affiliates.py` (asserted grep-level by a test).

**Read surfaces:** `GET /api/merchant/affiliates` (merchant-gated) lists per-referrer
totals + per-order rows with OKLink links; the merchant summary gains
`affiliate_owed_micro`. A referring agent checks its own balance via
`GET /api/affiliate/summary`, gated by the buyer wallet-signature session (sign as the
referrer address) — no public amount oracle.

---

## 5. Email capture (waitlist)

`POST /api/stores/{slug}/waitlist` — unauthenticated, live stores only, body
`{email}`: CRLF-stripped, `valid_email()` + 255-cap validated, lowercased, inserted
into `email_subscribers` with `UNIQUE(store_id, email)` (a duplicate returns the same
silent `{ok:true}` — no membership oracle). Abuse controls: the app's tightest slowapi
limiter (5/min), a per-store row cap (`TILLA_SUBSCRIBERS_MAX`, default 5000, → 429 when
full), and the store-status gate. Merchant surface:
`GET /api/merchant/stores/{slug}/subscribers`, `DELETE` per email, and
`GET /api/merchant/export/subscribers.csv` (through the `_csv_cell` formula-injection
guard). Emails never appear in feeds, discovery, logs, or any unauthenticated body.

**Sending stays DORMANT.** No broadcast endpoint ships. The already-shipped magic-link
redelivery sender no-ops with an `event_log` row while `SMTP_*` is unset; it activates
unchanged when the user later provides SMTP creds. A broadcast module would be a
separate user-gated increment (per-row signed unsubscribe tokens at send time).

---

## 6. USER-GATED / PARKED summary (honest, no claims, no fakes)

- **Affiliate payout execution** — never in code; a manual merchant/operator wallet
  send, recorded post-hoc via verify-and-record.
- **ChatGPT product-feed ingestion + Instant Checkout listing** — OpenAI merchant
  approval (external).
- **Perplexity merchant program** — enrollment (external).
- **Google Merchant Center** — account + listing (external).
- **SMTP sends + any broadcast feature** — needs creds; capture/export only until then.
- **ACP x402-middleware complete mode** — spike 9, parked until it passes.
- **Affiliate rate customization UI + non-address affiliate codes** — later increment.

No external listing, no affiliate payout, and no ACP platform integration is ever
claimed until a real, verifiable artifact (approval, tx hash) exists.
