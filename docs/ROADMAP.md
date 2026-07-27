# Tilla — The Ambitious Roadmap (v2)

> **Vision:** *The commerce operating system for the agent economy.*
> One prompt → a live store that sells to **humans and autonomous agents alike**, settling through OKX's **entire** Agent Payments Protocol on X Layer — and reaching every agent surface beyond OKX (MCP, ACP, machine-readable feeds).
> **This version supersedes v1.** It is grounded in a 16-agent verified research pass (2026-07-20/21): OKX's own asks, the live marketplace, the commerce state of the art, the agentic-commerce frontier, the exact payment rails, and X Layer itself. Every load-bearing claim was adversarially fact-checked; anything not verified is tagged `[unverified]` or `[inferred]`.

**Companion doc:** `BUILD.md` = the committed engineering plan (modules, acceptance criteria, tests). This doc = the full map of *what* and *why*.

---

## 1. What OKX wants — the complete checklist

### 1.1 The hackathon frame (verified: hackquest.io/hackathons/OKXAI-Genesis-Hackathon)
- Prize pool **$100,000**, max individual prize 10,000 USDT. Categories: **Best Product $20K**, **Creative Genius $20K**, **Revenue Rocket $20K** (each $10k/$6k/$4k), Finance Copilot / **Software Utility** / Lifestyle Companion / Artistic Excellence $7.5K each, **Social Buzz $10K** (10×$1,000).
- Judging = **"OKXAI Internal Review"** — no public rubric; human judges score product experience, creativity, revenue performance, social traction. `[inferred]` from a Build-X-series page: OKX also inspects code and **on-chain data**.
- Submission requirements: live ASP that passes OKX internal review ("go live or invalid"), X post with **#OKXAI** + demo **≤90s**, Google form.
- ⚠️ **Corrected deadline: 2026-07-27 22:59 UTC** (page JSON `submissionClose` — one hour earlier than the displayed 23:59). Rewards announced Aug 3.
- Per the user's direction, **the roadmap is depth-first, not deadline-first** — the submission is a checkpoint we pass through (§4), not the goal.

### 1.2 OKX's thesis — what a winning product demonstrates
1. **OPC**: "one person, one company, $1M a year" — Tilla is the OPC's storefront.
2. **Agents hiring agents** — discover, hire, pay on-chain.
3. **Real revenue** through the rails (Revenue Rocket).
4. **x402 / Agent Payments Protocol adoption** (the mandated user-facing brand).
5. **X Layer GMV in stablecoins**.
6. **OKX Agentic Wallet (TEE)** as the identity where reputation accumulates.
7. **Inspectable on-chain footprint** — sales, reviews, task history `[inferred]`.
8. **Social traction + partnership-worthiness.**
Framing: "Two Markets, One Society" (Agents market + Tasks market); pillars Identity / Community / Discover / Pay.

### 1.3 The 12 marketplace primitives and how Tilla exploits each

| # | Primitive | Verified mechanics | Tilla's exploit |
|---|---|---|---|
| 1 | ERC-8004 identity on X Layer | Multiple ASPs per wallet; registration/update/activate **gas-free** | Register stores as ASPs — an identity factory |
| 2 | Services + fees | Name 5–30 chars; desc ≤400 CJK-width, no links/prompts; A2MCP fee = USDT string; `validate-listing` QA gate; human review ≤24h | Multiple services under #6961: create-store, upgrade-store, per-store buy |
| 3 | A2MCP (pay-per-call, x402-compliant) | Instant settle, no arbitration | Tilla's live rail (create-store, 0.05 USDT) |
| 4 | A2A escrow services | Escrow on X Layer, released on sign-off; disputes → arbitration (5% bounty deposit) | "Custom store build" with buyer protection |
| 5 | Task board | paymentMode 1=escrow / 3=x402; `task-search`/`recommend-task`/`contact-user` | Take store-build jobs AND post tasks (logo, copy) = agents-hiring-agents receipts |
| 6 | Ratings / reputation | Bidirectional on-chain feedback; `soldCount` ranks the home view | Real GMV directly buys marketplace placement |
| 7 | Semantic search / asp-match | Sentence-level matching; auto-match shortlists ASPs | Keyword-rich store descriptions → organic routing |
| 8 | Avatars | file ≤1 MB via `agent upload` → CDN | Store logos as marketplace avatars |
| 9 | XMTP A2A chat | E2E-encrypted; carries negotiation/delivery envelopes | Buyer-agent ↔ store negotiation (discounts, bundles) |
| 10 | Evaluator role | Stake ≥100 OKB; commit-reveal arbitration | Future: "Tilla commerce evaluator" (separate wallet) |
| 11 | Payment rails | x402 exact / aggr_deferred / upto; MPP charge+session; a2a-pay links | Multi-rail per-SKU pricing (§2.2) |
| 12 | Two-market framing | Agents market + Tasks market | Tilla spans both sides of both markets |

### 1.4 Tilla today vs the gap
**Used (as of 2026-07-26):** ASP #6961; **six** listed services — three A2MCP platform services (Create Storefront 0.05 USDT, upgrade-store 0.03, add-product 0.01 — x402 exact, live 402 verified); USDT0 settlement + on-chain verify; avatar; **all four payment rails enabled** (exact / period / aggr_deferred / MPP — per-rail proof in §4 Phase 4).
**Unused (this roadmap's raw material):** **per-store ASP listings** (the biggest thesis-proof gap); task board both directions; OKX's own A2A escrow service type (Tilla ships its own non-custodial escrow instead); marketplace ratings/soldCount accumulation; XMTP negotiation; evaluator; semantic-search-optimized catalogs.

---

## 2. The full feature map

Tags: **[TS]** table-stakes · **[DIFF]** differentiator · **[EXCEEDS]** exceeds what OKX asks. Effort S/M/L.

### 2.1 Storefront / catalog
| Feature | Tag | Effort |
|---|---|---|
| LLM store generation → extend to multi-product catalogs | TS | S |
| Multi-file digital downloads | TS | S |
| License keys (issue/activate/validate/deactivate, activation limits — Lemon Squeezy pattern) | DIFF | M |
| Membership tiers; versioned releases pushed to past buyers | DIFF | M |
| Pay-what-you-want pricing | DIFF | S |
| **Machine-readable catalog**: `feed.json` (ACP shape) + schema.org JSON-LD + per-store `llms.txt` | EXCEEDS | S |
| Link-in-bio merchant profile; custom domains; OG/SEO hygiene | DIFF | S–M |
| *Skip v1:* physical/shipping, bookings, rentals, DRM | — | — |

### 2.2 Checkout + payments
| Feature | Tag | Effort |
|---|---|---|
| Persistent **order state machine** (pending→confirmed→delivered→refunded) — kills the in-memory dict | TS | M |
| Hosted checkout: QR, countdown, wallet connect (EIP-6963 / `window.okxwallet`, chain-switch to 0xc4) | TS | S |
| **Under/over/late-payment exception handling** — the #1 thing toy crypto checkouts omit (BitPay pattern) | TS | M |
| Discount codes; cross-sells; abandoned-checkout recovery | DIFF | S–M |
| Wallet-remembered repeat buyers (crypto Shop Pay; Shop Pay lifts conversion up to +50%) | DIFF | M |
| **Multi-rail per-SKU pricing** — one-off = x402 `exact` (live); bulk = `aggr_deferred` (server class already installed; TEE-wallet buyers only); metered = MPP session channels (spike-gated; `okxweb3-app-mpp` 0.1.0 is young); subscription = x402 `period` (Node sidecar; spike-gated) | EXCEEDS | L |
| AP2-style mandate/spend-cap enforcement server-side | EXCEEDS | S–M |

### 2.3 Fulfillment / delivery
| Feature | Tag | Effort |
|---|---|---|
| Instant delivery, zero manual steps | TS | S |
| **Signed expiring download links** + download limits (itsdangerous) | TS | S |
| Durable **buyer library** — re-download by wallet-signature sign-in | DIFF→TS | M |
| Verifiable receipts (signed, binding txHash + SKU + content hash) | EXCEEDS | S |
| **EAS attestation receipts on-chain** (predeploys live on X Layer — no contract deploy needed) | EXCEEDS | S |

### 2.4 Trust / refunds / disputes
| Feature | Tag | Effort |
|---|---|---|
| Receipts with tx hash + `oklink.com/x-layer/tx/<hash>` links | TS | S |
| Refund flow with recorded state (merchant-initiated) | TS | M |
| App-layer optimistic refunds (time-boxed; upgrade path to OKX Optimistic Escrow when it ships) | EXCEEDS | M |
| A2A **escrow checkout for agent buyers** (task rail — the only live escrow) | EXCEEDS | L |
| ERC-8004 verified-buyer reviews (gated on purchase tx) — experimental, spec is Draft | EXCEEDS | M |
| *Skip:* chargebacks, MoR tax remittance, fiat | — | — |

### 2.5 Merchant ops
| Feature | Tag | Effort |
|---|---|---|
| Merchant accounts (wallet-signature auth), multi-store, dashboard (orders/revenue/per-product) | TS | M–L |
| CSV exports; tax-ready sales report | TS | S |
| Merchant API + HMAC-signed webhooks (agents are the API consumers) | TS | M |
| Sandbox mode on X Layer testnet 1952 (`testrpc.xlayer.tech/terigon` + faucet) | DIFF | M |

### 2.6 Agent-native commerce (the moat)
| Feature | Tag | Effort |
|---|---|---|
| **Dual-sided stores**: every store buyable by agents via x402 (per-store buy endpoint, merchant payTo — non-custodial) | EXCEEDS | M |
| **Per-store MCP server** (`list_products` / `get_product` / `create_checkout` / `pay`) — any MCP agent can buy | EXCEEDS | S–M |
| **ACP-compliant checkout** (OpenAI/Stripe spec 2026-04-17, five `/checkout_sessions` endpoints, custom x402 handler) | EXCEEDS | M |
| **Store-as-ASP auto-listing** — Option A: delta-service under one ASP (proven pattern); Option B: per-store ASP identity (gas-free, review-gated) | EXCEEDS | M / L |
| Task-board participation both directions + agents-hiring-agents receipts (hire Warden #3808 for content-safety — real x402 receipt) | DIFF | M |
| A2A agent card `/.well-known/agent-card.json`; Bazaar-style discovery API mirror; trust signals in feeds + 402 responses | EXCEEDS | S–M |
| Pay-per-crawl catalog access (Cloudflare pattern) | DIFF | S |
| XMTP negotiation channel | DIFF | L |

### 2.7 Growth — BUILT (M13; see docs/acp-checkout.md)
| Feature | Tag | Effort | Status |
|---|---|---|---|
| **Agent affiliate attribution** (ACP affiliate object + on-chain USDT0 rev-share to referring agents) | EXCEEDS | M | LIVE — capture (web/agent/MCP/ACP) + accrual ledger + self-referral guard + merchant/referrer read surfaces + verify-and-record payout. Payout *execution* never in code (manual operator send, recorded post-hoc). |
| Merchant feed export: OpenAI feed + Google Merchant shape; Perplexity merchant program (free) | DIFF | S | LIVE (export shapes read-only). *Listing* on each surface = USER-owned external step, never claimed. |
| Embeddable buy button; email capture + broadcasts | DIFF | S–M | LIVE — `/embed.js` shadow-DOM button + waitlist capture/export. Broadcasts + SMTP sends parked (dormant until creds). |

### 2.8 On-chain depth
| Feature | Tag | Effort |
|---|---|---|
| Real-time payment detection: polling (committed) + WS `logs` subscription (`wss://ws.xlayer.tech`, spike-gated) + `eth_getLogs` backfill (**101-block cap**, timeouts mandatory) | TS | S |
| **EAS attestation per sale** (predeploy `0x4200…0021`, ~$0.0004/attestation) | EXCEEDS | S |
| StoreRegistry contract (storeId → merchant + metadata hash; Foundry deploy <$0.01) | DIFF | S–M |
| ERC-8004 store registration on canonical registries (`0x8004A169…a432` live on 196) — `[unverified]` whether OKX's marketplace reads these | EXCEEDS | M |
| ERC-721 receipt NFTs (most demo-visible in OKX Wallet) | DIFF | M |

---

## 3. Beyond OKX — ranked features nobody on the marketplace has

All "nobody has" claims `[inferred]` from the live scan (§5): ASPs are overwhelmingly single-endpoint x402 APIs.

1. **Machine-readable catalog stack** (feed.json + JSON-LD + llms.txt) — hours of work, opens every crawler/agent surface. Ship first among EXCEEDS.
2. **Per-store MCP servers** — Claude/Cursor/any MCP agent can browse and buy every Tilla store.
3. **ACP-standard checkout on OKX rails** — same store sellable via OKX x402 AND the OpenAI/Stripe standard (ChatGPT listing itself needs OpenAI approval — external).
4. **Verifiable receipts** (+ EAS attestations) — non-repudiable delivery proof before OKX escrow exists.
5. **ERC-8004 on-chain store reputation** — experimental (Draft spec, `[unverified]` marketplace linkage).
6. **Agent affiliate revenue share** — other agents become Tilla's distribution.
7. **A2A escrow storefronts** — stores hireable via task board with buyer protection (rail proven end-to-end with Warden).
8. **Multi-rail per-SKU pricing** — OKX asks for one rail; Tilla exposes four, all enabled (honest caveat: `aggr_deferred` needs a TEE agentic-wallet buyer and no batch-priced product is listed today. The MPP caveat that stood here — "opened but never closed" — was retired 2026-07-26: both channels closed on-chain, PROOF §10.)
9. **Mandate/spend-policy-aware checkout** — AP2-aligned before OKX is.
10. **Discovery API mirror + agent cards** — future-proofs for Coinbase Bazaar / Google UCP stacks.
11. **Pay-per-crawl catalog access.**
12. **Store-as-ASP identity factory** — per-store marketplace placement at zero gas (throughput bounded by ≤24h human review per listing).

---

## 4. Build phases (depth-first; see BUILD.md for module-level commitments)

- **Phase 1 — Foundation (product, not toy):** git repo + tests + CI; security hardening (kill the live XSS, validation, rate limits, Warden screening); SQLAlchemy persistence; hardened checkout state machine (unique amounts, txhash verify, payment detection — polling committed, WS spike-gated — under/over/late-pay, expiry, idempotency); real gated delivery (uploads, signed expiring links, buyer library).
- **Phase 2 — Storefront excellence:** wallet-connect checkout UX; 3 themes wired + selectable; receipts with explorer links; OG/SEO/JSON-LD.
- **Phase 3 — Agent-native commerce:** per-store x402 buy endpoints (dual-sided stores); machine-readable catalog stack; per-store MCP server; agent card + discovery mirror.
- **Phase 4 — Full payment protocol — DONE except one half-loop (all four rails enabled; proof per rail, 2026-07-24):** per-SKU rail menu (`pricing_model` + declaration endpoint + feed/MCP/llms surfacing) plus all four rails are built, enabled, and — with one exception — settled with real funds. Settlement status per BUILD.md M8: x402 exact **LIVE, PROVEN** (3 recorded proofs); subscription (`period`) **LIVE, PROVEN** (two real settles, blocks 66072022 / 66072295); `aggr_deferred` **LIVE, PROVEN** (one batch settle covering two orders, block 66059520 — and `sync` is batch-priced and listed, so its 402 carried both `exact` and `aggr_deferred`. **As of 2026-07-27 no live 402 advertises the rail again, deliberately:** the deferred rail is now offered only on a *revocable* (file) deliverable, at all three layers — challenge, feed, pay-time — and no store currently holds one. See `docs/runbooks/rail-enablement.md`); MPP pay-as-you-go **LIVE, FULLY PROVEN** *(upgraded 2026-07-26 — this line previously read "HALF-PROVEN … never closed or settled")*: the channel opened against a 2 USDT deposit, a voucher was signed, a metered unit was delivered, and the channel **closed on-chain** at block 66056578 — 0.1 USDT0 to the merchant, 1.9 refunded — with a second test channel closing at 66056643 for a full 2 USDT refund (PROOF §10). Note OKX's settlement-agent API still reports both as `CLOSING`; the chain is authoritative. Every settlement above is a **self-funded arm's-length test**; the rule is unchanged — a rail is only claimed once a real settlement tx hash is logged.
- **Phase 5 — Merchant platform:** accounts, multi-store, dashboard, CSV, merchant API + webhooks.
- **Phase 6 — Marketplace citizenship:** more services under #6961; store-as-ASP auto-listing; task-board participation; agents-hiring-agents (Warden hire); post-task ratings.
- **Phase 7 — On-chain depth:** EAS receipts; StoreRegistry; ERC-8004 experiments.
- **Phase 8 — Growth — BUILT (M13; live, with USER-gated tails):** affiliate attribution + accrual ledger + verify-and-record payout (payout execution never in code — a manual operator wallet send, recorded post-hoc); external feeds (OpenAI JSON + Google Merchant RSS + Tilla-wide aggregate, read-only); embeddable shadow-DOM buy button (`/embed.js`); ACP `/checkout_sessions` five-endpoint surface (**now enabled** — the endpoints answer real status codes, not the old dormant 503, and one session has completed and settled on-chain, block 66025300; x402-middleware complete = spike 9, still parked). External *listings* (ChatGPT/Instant-Checkout, Perplexity, Google Merchant Center) + SMTP broadcasts are USER-owned and never claimed until a real artifact exists. See `docs/acp-checkout.md`.

**Hackathon checkpoint (2026-07-27 22:59 UTC):** submit whatever is genuinely done — target = Phases 1–3 complete with at least one real arm's-length purchase. Submission assets (X thread, ≤90s demo, form) are drafted and **user-owned/approval-gated**.

**Social Buzz workstream (user-owned, runs in parallel):** the $10K Social Buzz category and the "social traction" judging axis need more than the one mandatory #OKXAI post. Plan: build-in-public thread cadence (each shipped phase = one thread with live links + receipts), per-store share cards (OG images make every generated store a shareable artifact), and the ≤90s demo cut for reposting. All drafting is ours; **every post is approval-gated** — nothing is published without explicit sign-off.

---

## 5. Market reality (live scan, 2026-07-20/21 — single-source: authenticated onchainos CLI; re-scan before submission)

- **White space HOLDS on all three axes.** Zero storefront/checkout/gated-delivery agents; nearest neighbors (Titi Commerce Copilot #3603, 数商助手 #3774, DNAcloud #1468) have combined **soldCount ≈ 6**; `asp-match` for "build me an online store…" returns only trading/news agents — OKX's own router has no storefront ASP to route to. No commerce entrant above ID #6023.
- **The bar to beat is winning formats, not commerce rivals:** PixelBrief #5421 (10,098 sold — one viral hook), CoinWM/CoinAnk (many cheap services), AgentFund #6023 (383 sold in ~4 days — ~100 sales/day is achievable). Tilla's store-listing model can replicate the many-service catalog format at marketplace scale.
- **Task board:** only 6 open tasks, all ≤1.5 USDT, all test/trivial — organic volume flows through direct x402 calls `[inferred]`.
- **Fee benchmarks:** Gumroad 10%+$0.50 (30% marketplace), Lemon Squeezy 5%+$0.50 MoR, Coinbase Commerce 1%, NOWPayments 0.5–1%. **No crypto gateway does fulfillment, licensing, buyer accounts, or growth tooling** — the Gumroad-tier-fulfillment + agent-payable-rails combination has no incumbent `[inferred]`.

---

## 6. Business model

- **Live now:** 0.05 USDT0 per store creation — charged identically whether an agent pays over x402 (ASP #6961) or a human self-serves from the dashboard — plus `upgrade-store` (0.03 USDT) and `add-product` (0.01 USDT).
- **Add:** premium creation tier (custom domain, multi-product, themes) at a higher flat fee; later a small platform fee where rails support it (MPP supports payment **splits, max 10** — the only non-custodial %-fee rail today; x402 `exact` pays the merchant directly and stays fee-free).
- **Principle: non-custodial by default.** Human checkout and per-store x402 settle **directly to the merchant wallet**. Custodial flows (A2A escrow proceeds settle to the ASP wallet) are opt-in and clearly labeled (§8).
- **Success metrics to steer by:** ≥3 stores created by real third parties; ≥1 arm's-length agent purchase end-to-end (second self-operated wallet — honestly labeled as such); ≥1 **genuinely external** purchase (a person/agent we don't operate — needs recruitment, user-assisted); ≥1 real sale per rail shipped; test suite ≥60 green tests; every sale carrying an on-chain receipt link; soldCount on #6961 climbing.

---

## 7. Constraints — verified blocked paths (never commit to these)

- **Escrow-as-payments-API does not exist** (OKX "Optimistic Escrow" under development). Only live escrow = A2A task rail; **agent buyers only** (CLI + XMTP + agentic wallet), no headless REST escrow for human web buyers.
- **x402 `period` (subscription) is JS-SDK-only**; Python absent → Node sidecar, and the period-scheme location in the JS packages is `[unverified]` — spike before committing.
- **`aggr_deferred` buyers must be OKX TEE agentic wallets** — not offerable to arbitrary wallets.
- **`eth_getLogs` cap = 101 blocks inclusive**; oversized requests hang (no fast error) — client timeouts mandatory. `eth_subscribe` end-to-end `[unverified]` — validate with one script.
- **OKLink Explorer data API is dead** (suspended 2025-05-20; only contract-verification remains) — self-index payments via WS/getLogs.
- **x402 Bazaar auto-listing requires the CDP facilitator (Base/Solana)** — ship the API mirror; CDP listing is stretch.
- **ChatGPT Instant Checkout needs OpenAI merchant approval** — build ACP compliance; placement is external.
- **XMTP dispatch runtime** (Node ≥22.14, `switch-runtime` dance) is the hard dependency for all A2A automation — shared infra with Warden, build once.
- **Listing review ≤24h + content rules** (no links/prompts/tech-stack in descriptions) bound per-store ASP throughput.
- **slowapi is pre-1.0** (pin exact; in-memory storage only). **FastAPI has no body-size cap** — enforce via nginx + chunked-read budget, and test it.

---

## 8. Risks (named, not buried)

1. **Wash-trading optics:** our only proven full loops are self-trades. Judges inspect on-chain data `[inferred]` — prioritize **real arm's-length purchases** (second funded wallet, external agent) and label demo self-trades honestly.
2. **Demand is unproven** — this remains a judging + promotion bet; the scan shows supply white space, not buyer demand. Mitigation: winning-format replication (many services, low fees), external surfaces (MCP/feeds) so demand isn't OKX-only.
3. **Custody:** A2A escrow settles to Tilla's ASP wallet (custodian of merchant funds) — opt-in only, small amounts, clear labeling; keep default rails non-custodial.
4. **Content abuse / legal:** LLM storefronts can be asked to sell pirated/illegal goods — Warden screening on every store pre-deploy + takedown path + ToS page; no sanctions/KYC capability — keep amounts small, digital-goods-only.
5. **ERC-8004 is a Draft** (churn risk) and OKX-marketplace↔canonical-contract linkage `[unverified]` — tag experimental.
6. **Single-VPS SPOF + sqlite** — backups, monitoring, restart-safety are committed modules (BUILD.md), not afterthoughts.
7. **Young dependencies:** `okxweb3-app-mpp` 0.1.0 (monkey-patches pympp, unattributed publisher) — smoke-test the full channel loop before building on it.
8. **USDT vs USDT0 naming:** rails brand "USDT"; the on-chain asset is USDT0 `0x779ded…3736`. Same-asset assumption holds for our live x402 config (validated), but confirm for MPP/task-rail settlement during spikes.

---

## 9. Open verification spikes (gate the features they precede)

| Spike | Gates | Test |
|---|---|---|
| MPP session loop (open→voucher→topup→close) against SA API with our creds | Pay-as-you-go | scripted e2e on testnet/mainnet-small |
| Node sidecar `period` support (`@okxweb3/app-x402-core` 0.2.1 et al.) + facilitator `/subscriptions/*` | Subscriptions | sidecar spike + one real subscription charge |
| `eth_subscribe` on `wss://ws.xlayer.tech` end-to-end | WS payment detection | one script, one real transfer |
| EAS `attest()` via web3.py on predeploy | On-chain receipts | one attestation on testnet 1952, then mainnet |
| OKX mobile deep-link format | Mobile checkout | manual device test |
| aspCount / multi-ASP + listing-review throughput | Store-as-ASP factory | register 1 extra ASP, measure review |
| Dynamic per-request x402 accepts (per-store payTo from DB) in `okxweb3-app-x402` 0.1.0 | Dual-sided stores (BUILD M7) | test middleware with runtime-built RouteConfig; fallback = hand-built 402 + direct facilitator verify/settle |
