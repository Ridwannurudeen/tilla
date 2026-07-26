# Tilla Open Protocol v1

**Version: 1.0.0.** The agent-facing contract of a Tilla instance: how an
autonomous buyer (or a peer marketplace) discovers stores, reads a machine
catalog, obtains a wholesale quote, and pays over x402 — non-custodially, on
X Layer. This document is the **deliverable of M16.3**: it FREEZES the surfaces
that already ship (M7 agent surface, M13 external feeds, M16 B2B tiers) against
pinned JSON Schemas so the spec can never silently drift from the running code.

Every surface below carries a `version` and, where a body shape applies, a
pinned JSON Schema (Draft 2020-12) under `docs/specs/schemas/` (or the pre-M16
`docs/openapi.*.yaml`). `tests/test_protocol_goldens.py` validates the LIVE
responses of a Tilla instance against these frozen schemas on every run — a drift
between this spec and reality is a failing test, not a stale doc.

## Design invariants (normative)

- **INV-A — non-custodial.** Every `pay_to` in a 402 challenge is the merchant
  wallet; Tilla never custodies funds and never proxies, quotes, or settles a
  peer's sale. Funds move only at settle, only between buyer and merchant.
- **INV-B — no terms / identity leak.** No public surface (feed, llms.txt,
  discovery, agent card) exposes a wholesale tier table, buyer identities, or
  order data. The tier table surfaces only as an opt-in `wholesale: true`
  boolean; a per-buyer price is disclosed only via a per-request `/quote` (or
  MCP `get_product` with an `agent_id`).
  *(Corrected 2026-07-26: this invariant previously also claimed no public
  surface exposes a merchant `pay_to`. That was never true and was never meant
  to be — `requiredFunds.pay_to` is **required** by this spec's own pinned
  schema `docs/openapi.feed.yaml` and by `schemas/mcp-get-product.yaml`, and
  `app/agentic.py::_required_funds` publishes it on every feed product. It is
  published deliberately: `pay_to` is the merchant's own receiving address, and
  an agent cannot pay without it. Publishing it is what makes the rail
  non-custodial and verifiable, not a leak. What INV-B actually protects is
  per-buyer pricing and identity.)*
- **INV-C — INV-1 (tier integrity).** A wholesale discount is granted at
  settle-record time ONLY if the settled payer wallet equals the on-chain owner
  of the presented ERC-8004 agent id. Any unverifiable / mismatched claim, RPC
  outage, or unconfigured registry falls back to the base price
  (fail-to-base, never fail-open-to-a-discount).

## Surface registry (frozen artifacts)

| Surface | Method + path | Body version | Pinned schema |
|---|---|---|---|
| Product feed | `GET /s/{slug}/feed.json` | 1.0.0 | `docs/openapi.feed.yaml` |
| External (OpenAI-shape) feed | `GET /s/{slug}/feed/openai.json`, `GET /feeds/openai.json` | 1.0.0 | `docs/openapi.external-feeds.yaml` |
| Agent card | `GET /.well-known/agent-card.json` | 1.0.0 | `docs/specs/schemas/agent-card.yaml` |
| Wholesale quote (M16 tier ext.) | `GET /s/{slug}/quote?agent_id=<id>` | 1.0.0 | `docs/specs/schemas/quote.yaml` |
| MCP tool catalog | `POST /s/{slug}/mcp` `tools/list` | 1.0.0 | `docs/specs/schemas/mcp-tools-list.yaml` |
| MCP `get_product` result | `POST /s/{slug}/mcp` `tools/call` | 1.0.0 | `docs/specs/schemas/mcp-get-product.yaml` |
| x402 buy | `POST /s/{slug}/buy` | see §402 | header-shaped (x402-v2) |

## §402 — payment challenge conventions

The buy route (`POST /s/{slug}/buy`) is an x402 paywall. An unpaid request (incl.
an unpaid probe `GET`) receives a `402` with a `PAYMENT-REQUIRED` header carrying
the accepts list; a paid retry carries the signed `PAYMENT-SIGNATURE` /
`X-PAYMENT` header and, on success, the response carries a `PAYMENT-RESPONSE`
header with the settle tx.

- **Protocol:** `x402-v2`, scheme `exact` (always). `aggr_deferred` appears on a
  `batch` product only, and only when the operator enables it.
- **Network:** `eip155:196` (X Layer). **Asset:** USDT0 (`extra.name` `USD₮0`,
  `extra.version` `1`). **payTo:** the merchant wallet, resolved per-request
  (INV-A) — never Tilla.
- **Amount:** integer micro-USDT (6 decimals), no floats. The challenge amount
  equals what settles.
- **M16 tier pricing.** A buyer presents its ERC-8004 agent id as the query
  param `?agent_id=<id>` (it survives the 402 → paid-retry roundtrip, like
  `?ref=`). When an agent id is presented AND the payer wallet recovered from the
  payment header is the verified on-chain owner of that id, the challenge amount
  is the wholesale **tier** price; otherwise it is the base price (INV-C). The
  settle seam re-derives the same gate with a FRESH ownership read (never a stale
  cache), so a discount can never ride a stale verification. A wrong-payer tier
  claim under-pays the base challenge and never settles; there is nothing to
  refund because the discount was never granted.
- **Idempotency / replay.** The EIP-3009 authorization nonce is the replay key,
  scoped to `(store, payer)`. A replayed nonce returns the original order at its
  original price — the tier is never re-quoted on a replay.

## §MCP — tool surface

A hand-rolled stateless JSON-RPC 2.0 server at `POST /s/{slug}/mcp`
(`application/json`, no SSE). Methods: `initialize`, `ping`, `tools/list`,
`tools/call`, plus the `notifications/initialized` notification. Tools:

- `list_products` — active products with price + network.
- `get_product` — one product's detail + the x402 buy endpoint. Optional
  `agent_id` echoes an advisory wholesale `quote` (same gate as `/quote`).
- `create_checkout` — a unique-amount on-chain checkout for agents that pay the
  merchant directly (optional `ref` for affiliate attribution).
- `pay` — submit the on-chain tx hash for a checkout.

## §Discovery

- `GET /discovery/resources` — the Tilla-wide index of live stores (no merchant
  wallets). `GET /discovery/search?q=` — filtered. Each row links out to the
  store's own `feed.json`, `mcp`, `buy`, and human storefront. **M16.4** adds
  `?include=federated` (peer listings marked `federated: true`, linking OUT to
  the peer's own checkout — Tilla never proxies a peer sale).

## Conformance checklist (curl-only, self-run)

Run against your own instance base URL. Each step is PASS iff the response
validates against the pinned schema for that surface (see the registry table).

1. `curl -s $BASE/.well-known/agent-card.json` → validates `agent-card.yaml`;
   `payment.network == eip155:196`; carries an ERC-8004 registration.
2. `curl -s $BASE/s/$SLUG/feed.json` → validates `openapi.feed.yaml`; no
   `pay_to`; a tiered product shows `pricing.wholesale == true` and NO `tiers`.
3. `curl -s "$BASE/s/$SLUG/quote?agent_id=$AID"` → validates `quote.yaml`;
   returns `base_price_micro`; tier fields present only for a verifiable owner.
4. `curl -s $BASE/s/$SLUG/mcp -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`
   → `result` validates `mcp-tools-list.yaml` (**five** tools — corrected
   2026-07-26; this said "the four tools", but the pinned schema and
   `app/agentic.py` have long served five, the fifth being `preview_order`).
5. `curl -s $BASE/s/$SLUG/mcp -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
   "params":{"name":"get_product","arguments":{"product_id":$PID}}}'` →
   `result.structuredContent` validates `mcp-get-product.yaml`; no tier table
   leaked; `pricing.wholesale == true` for a tiered product.
6. `curl -s -X POST $BASE/s/$SLUG/buy` (no payment) → `402` with a
   `PAYMENT-REQUIRED` header advertising scheme `exact` on `eip155:196`.

## Versioning policy

`version` is semver. An ADDITIVE field (new optional property) is a MINOR bump on
that surface's body version; a removal or a required-shape change is MAJOR and
mints `tilla-protocol-v2.md` (this file is frozen once published). The pinned
schemas are the source of truth; this prose explains them.

## Parking (honest)

- **Publishing tilla-protocol-v1 externally** (announcement, a hosted schema
  registry) is USER-gated distribution — out of scope for the build.
- Cross-instance reputation/ratings need a second independent operator + the
  gated XMTP/task-board runtime (M10) and are specified, not shipped, here.
