# Tilla — The Full Vision (Plain English) — VERIFIED

> **One line:** *The commerce operating system for the agent economy.* Describe what you sell in one sentence, and get a live crypto store that sells to humans **and** autonomous AI agents — settling through OKX's payment protocols on X Layer, and reachable from every agent surface beyond OKX.

*This is a fact-checked revision of `Tilla-Full-Vision.md`. Every [live] / [built-dormant] / [designed] marking was re-verified against the actual codebase (2026‑07‑22): 48 claims confirmed as-written, 19 corrected (overstatements), 4 updated (stale), and 20 shipped capabilities added that the original predated. Corrections and additions are the ones judges could catch — being precise here is the point. A change-log is at the end.*

---

## 1. The core idea

Tilla turns a single sentence into a real, working online store. You say what you're selling; Tilla generates the brand (name, copy, colors, layout), deploys a live hosted storefront at its own web address, and wires up crypto payment so people can buy in seconds. No design, no code, no payment setup.

The real ambition isn't "another store builder." It's this: **as commerce shifts from people clicking "buy" to AI agents transacting on behalf of people, every seller will need a storefront that agents can discover and pay automatically.** Tilla is the one-sentence way to create exactly that — a shop that serves both a human with a wallet and a software agent paying over a machine protocol, from the same store.

---

## 2. The problem, at two levels

**The everyday problem:** starting to sell online is still a slog — store builder, domain, payment processor, branding, stitched together over hours before you earn a dollar. Tilla collapses that into one step, and a human can now do the whole thing on the site itself (pay the small creation fee from their wallet and get a store back).

**The bigger problem:** the world is moving toward *agentic commerce* — AI agents that buy, sell, and hire other agents. That future needs storefronts that speak "agent," settle in stablecoins on-chain, and carry verifiable, inspectable proof of every sale. Almost nothing is built for that. Tilla is.

---

## 3. What makes it different — the moat

Most store builders sell only to humans. Tilla's stores are **dual-sided**: the same shop is buyable by a person (wallet connect, QR, checkout) *and* by an autonomous agent paying programmatically over the x402 protocol — non-custodially, straight to the merchant's wallet. On top of that, each store is exposed on agent-readable surfaces:

- **A per-store "MCP server"** so any AI agent (Claude, Cursor, etc.) can browse products and buy. **[live]**
- **Machine-readable catalogs** (`feed.json`, schema.org JSON‑LD, per-store `llms.txt`) so crawlers and agents can read the shop. **[live]**
- **An agent card and discovery API** so Tilla stores are findable in the emerging agent ecosystem. **[live]**
- **ACP-compliant checkout** (the OpenAI/Stripe-lineage agent-commerce standard) so the same store *can also* sell through that standard. **[built-dormant]** — the code and tests exist but every endpoint returns 503 until `TILLA_ACP_ENABLED` is set.

And Tilla itself is a *listed agent inside OKX's marketplace* (agent **#6961**) — so **other agents can hire Tilla**: an orchestrator finds it, pays a small fee, and gets back a live store. That "agents hiring agents" flow is proven on-chain.

Today the moat actually rests on **identity (ERC‑8004 #6961), a listed paid service, pay-per-call x402, and — uniquely — a seller-owned branded storefront that sells to humans too.** Escrow, task-board participation, ratings, semantic-search catalogs, and encrypted agent chat are *designed* extensions, not yet used.

---

## 4. Everything it does (and will do)

Marked **[live]** (built and running), **[built-dormant]** (built, switched off until credentials/funds), or **[designed]** (planned, not yet built).

### Storefront & catalog
- One-sentence LLM store generation, extended to **multi-product catalogs** (up to 8 products, per-product buy) [live].
- **Per-product digital file downloads** (one file per product across the catalog); **license keys** (issue / activate / validate / deactivate, with per-key activation limits) [live]. *(Multi-file bundles within a single product are [designed].)*
- **Machine-readable catalog stack** — `feed.json`, JSON‑LD, per-store `llms.txt` [live].
- **A public human marketplace directory** — every live store is browsable and filterable by people at `/marketplace.html`, built on the same discovery API agents use, so the agent-discovery layer doubles as human distribution [live]. *(Added this session.)*
- OG/SEO hygiene — Open Graph meta, canonical URLs, branded 404, `robots.txt`, DB-generated `sitemap.xml` covering every live store plus the marketplace/library/receipt hub pages [live]. Link-in-bio merchant profile and custom domains [designed].
- Membership tiers; versioned releases pushed to past buyers; pay-what-you-want pricing [designed].

### Checkout & payments
- A durable **order state machine** — persisted, race-proof transitions; the visible path is pending → confirmed → delivered → refunded, with detected / underpaid / overpaid / late_paid / expired / (agent) settling states underneath [live].
- Hosted checkout: QR code, countdown, wallet connect with **auto chain-switch to X Layer** (chainId 196) [live].
- **Under / over / late-payment handling** — underpay holds, overpay delivers + records an overage-refund, a late payment inside a 24h quarantine still delivers — the thing toy crypto checkouts omit [live].
- **Paid self-serve store creation for humans** — a signed-in merchant creates a store from the dashboard: the description is safety-screened **before** any payment is offered (fail-closed), they pay the one-time **1 USDT** fee on-chain from their own wallet, Tilla verifies the exact transfer on-chain and generates the store with their wallet as the receive address (reused-payment → 409, generation outage → paid + free retry) [live]. *(Added this session — humans no longer need an agent to create a store.)*
- **Optional email receipt at checkout** with magic-link re-delivery — a fallback for exchange-custody buyers who can't later sign with the paying wallet [live; the email *send* is dormant until SMTP credentials].
- **The payment-rail menu** — one-off (x402 "exact") [live/proven]; bulk/batch (aggr_deferred) [built-dormant]; pay-as-you-go metering (MPP) [built-dormant]; subscriptions (x402 "period") [built-dormant]. Each dormant rail stays off until real credentials + a funded buyer prove a live settlement.
- Discount codes, cross-sells, abandoned-checkout recovery, wallet-remembered repeat buyers, spend-cap/mandate-aware agent checkout [designed].

### Fulfillment & delivery
- Instant, zero-manual delivery; **signed, expiring download links** with download limits [live].
- A durable **buyer library** — re-download anything by signing in with your purchase wallet — now with a dedicated **"Your purchases" web page** (`/library.html`) linked from every surface [live].
- **Email receipt + magic-link re-delivery** for exchange-custody buyers (attach an email after an ownership check, get a signed 7-day link that re-serves the delivery) [live; sending dormant until SMTP].
- Verifiable receipts binding **buyer + store + amount + payment tx hash**; on-chain **EAS attestation receipts** [built-dormant, triple-gated on `ATTEST_ENABLED` + attester key]. Receipts additionally binding **product + content hash** [designed].

### Trust, refunds & disputes
- Receipts with explorer links; **merchant-initiated refund flow** with recorded state — non-custodial: the merchant sends, Tilla verifies the tx on-chain [live].
- **A2A escrow checkout** for agent buyers with buyer protection [designed — the x402 rail underneath is proven, the escrow state machine is not built].
- Verified-buyer reviews gated on a real purchase [designed].

### Merchant operations
- Merchant accounts (sign-in by wallet signature), multi-store, and a **full back-office dashboard** [live]: orders / revenue / per-product analytics, **product catalog management** (add / edit / deactivate, screened), a **deliverable manager** (store-default and per-product; files, text, license keys), a refund/underpaid queue, webhook config, marketplace-listing status, referral earnings, and **paid self-serve store creation**.
- CSV / tax-ready exports; a **merchant API with signed webhooks** (agents are the API consumers) [live].
- Testnet sandbox mode [designed].

### Agent-native commerce (the differentiator)
- **Dual-sided stores** — every store buyable by agents via x402, non-custodial [live].
- **Per-store MCP server**; **agent card + discovery mirror** [live]; **ACP-standard checkout** [built-dormant].
- **Store-as-ASP auto-listing** — turn each store into its own listed marketplace agent [designed; the M10 listing tooling exists].
- **Agents hiring Tilla** — proven with an on-chain receipt (a self-funded arm's-length wallet paid Tilla to create a store, block 65875359) [live/proven]. **Tilla hiring Warden** (paid content-safety screening) [built-dormant — needs the paid-Warden flag + payer key]. Task-board participation [designed].
- **Reputation-ranked discovery** [designed] — extend the discovery index beyond `sold_count` with per-store `success_rate` (delivered vs. failed/refunded), unique-buyer count, and last-sale recency, and let agents sort by them. *(This is the top adopt-item from the Virtuals/ACP competitive research — see the companion doc; it's the single highest value-per-effort next build.)*
- Pay-per-crawl catalog access; XMTP negotiation channel [designed].

### Growth
- **Agent + human affiliate revenue-share** — any wallet gets a personal `?ref=` share link (on the buyer's receipt), referrers see their own earnings in the dashboard, merchants see what they owe per referrer; accrual is written to a **database ledger** at the settled seam (default 2%). Payout execution stays a manual, recorded operator action that **verifies each payout tx on-chain** — never automated fund-moving code [live].
- Merchant feed export for OpenAI / Google Merchant / Perplexity surfaces [live, export shapes]; the actual *listings* on those platforms are user-owned external steps.
- **Embeddable buy button** (drop-in) and **email/waitlist capture** [live]. Broadcast/fan-out sending [designed — deliberately no fan-out code yet].
- A **growth-kit** endpoint that drafts marketing copy from a store's own content (merchant-gated, output re-screened) [live]. A scheduled content calendar [designed]. **Publishing is permanently a human action** — Tilla never posts anything itself.

### On-chain depth
- Real-time payment detection via an always-on **polling sweeper** (proven matching a real transfer on-chain) [live]. Websocket/`eth_subscribe` subscription [designed — an unvalidated spike, not built].
- **EAS attestation per sale** [built-dormant]; a **StoreRegistry** contract and ERC‑8004 store registration [mix of built/designed]; receipt NFTs [designed].

---

## 5. The vision-tier build-out (the multi-quarter horizon)

**Honest status: these four pieces are DESIGNED, not built.** The repo's own `docs/VISION.md` states "STATUS: DESIGNED, NOT BUILT — nothing in this section exists in code" for each. What exists is a written design that reuses today's real seams; treat the whole tier as [designed] unless a specific item is called out below.

1. **Plugin / extension ecosystem** — provider interfaces specified from the existing delivery/payment/theme seams; no plugin code exists yet, and third-party *code* is explicitly gated on a real external author + sandbox/review pipeline. [designed]
2. **Cross-chain checkout** — single-chain today (`PAYMENT_NETWORK = eip155:196` hardcoded); the design ranks options (add facilitator-supported chains / a labeled "bridge-in" affordance) with **no custody, no fund-moving code inside Tilla, ever**. [designed]
3. **B2B / marketplace-of-marketplaces** — wholesale tiers keyed to a buyer agent's on-chain identity, store-to-store procurement, and *federation*. [designed]. What is **real today**: an **export** feed stack + the discovery index, and a **committed spec seed** — two test-pinned OpenAPI/JSON-Schema files (`docs/openapi.feed.yaml`, `docs/openapi.external-feeds.yaml`, derived from the ACP feed spec). Assembling those + the agent-card + 402 conventions into a *published, versioned* public spec is the remaining deliverable.
4. **Autonomous growth agent** — from the built growth-kit seed toward a per-store content calendar with performance feedback — always ending in a **draft → approve → publish** queue where a human presses publish. [designed]

Plus an **open SDK**: a Python client is shipped (`sdk/python`, isolated install + its own CI job); a TypeScript port is [designed].

---

## 6. Where it sits in OKX's world

OKX's thesis is "one person, one company, $1M a year" powered by agents hiring agents on-chain. Tilla is designed to be the flagship proof: the one-person entrepreneur's storefront that earns real revenue through the rails, drives stablecoin volume on X Layer, and accumulates an inspectable on-chain footprint. It **exploits OKX's identity, listing, and pay-per-call primitives today** (ERC‑8004 #6961, a listed paid service, x402), and is **designed to also use** escrow, the task board, ratings, semantic search, avatars, and encrypted agent chat.

Beyond OKX, this session hardened the *human* half of the dual-sided story: a public marketplace directory, a coherent nav across landing / marketplace / buyer library / dashboard, an intro-first landing with a live "happening now" store feed pulled from the discovery API, a plain-English FAQ, and a first-class buyer-library page — so the storefront is genuinely usable by non-crypto people, not only agents.

---

## 7. How it makes money

- **Live today:** a small flat fee per store creation — **1 USDT**, payable **either** by an agent over x402 **or** by a human self-serve on the site (both charge the same on-chain-verified fee) — plus paid **`upgrade-store` (1 USDT)** and **`add-product` (0.5 USDT)** services.
- **Next:** a premium creation tier (custom domain, multi-product, themes) at a higher fee; and, where a rail supports it, a small platform fee (the metered MPP rail supports non-custodial payment splits — the one place a percentage fee is possible without holding funds).
- **Guiding principle: non-custodial by default.** Human checkout and per-store agent purchases settle **directly to the merchant's wallet** — Tilla never holds their money. Custodial flows (escrow proceeds) are opt-in, clearly labeled, and not yet built.

*(Note: an earlier draft mentioned repricing the creation fee to ~0.2 USDT. That has **not** happened — every shipped surface, including the new self-serve flow, charges 1 USDT. Reprice in code first if the lower number is wanted.)*

---

## 8. The market reality (honest)

- **White space is real, but narrower than "nobody sells to humans."** On OKX's marketplace there is no storefront/checkout/gated-delivery agent — Tilla is first in this lane. Even the biggest agent platform (Virtuals) has a human *buyer-side* concierge (Butler) but **no seller-owned, one-prompt, branded storefront-with-checkout** — that specific product is still open, and Tilla spans both the human checkout and the agent-x402 side of it. *(See the companion competitive doc.)*
- **The bar to beat is winning *formats*** — top sellers win with one viral hook or a big cheap-service catalog; Tilla's store-listing model can replicate the many-service catalog format at scale.
- **Comparable fees:** Gumroad ~10%+, Lemon Squeezy ~5%, crypto gateways ~0.5–1% — and none of the crypto gateways do fulfillment, licensing, buyer accounts, or growth tooling. The "Gumroad-grade fulfillment + agent-payable rails" combination has no incumbent.

---

## 9. The honest part — proven vs. bet

- **Proven (on-chain, 2026‑07‑21, receipts in `docs/PROOF-onchain.md`, each status 1):** a **human checkout** (9.000707 USDT0, block 65873791), an **agent x402 product buy** (block 65875190), and a **second self-funded arm's-length wallet paying Tilla to spin up a new store** (block 65875359, producing live store `/s/sync/`). The product genuinely builds and deploys live stores, takes real on-chain crypto from both humans and agents, and is a **fully listed, approved service on OKX**. The engineering is deep and real — not a demo.
- **The bet:** *demand.* OKX's marketplace is early; there isn't a crowd of buyers waiting. Tilla was built as a **flagship proof of OKX's own vision** — to win on judging and OKX's promotion — while extending onto external agent surfaces (MCP, feeds, ACP) so demand isn't OKX-only. The competitive research sharpens this: **even the market leader hasn't proven agent commerce transacts at scale without subsidizing it** — so this is a whole-category open question, not a Tilla-specific weakness.

---

## 10. Named risks (not buried)

1. **Self-trade optics** — fully-controlled test loops are self-trades; judges inspect on-chain data, so real arm's-length purchases are prioritized and demo self-trades are labeled honestly (the create-store proof used a *separate, self-funded* wallet, labeled as such — not literally an unrelated third party).
2. **Unproven demand** — mitigated by winning-format replication and external surfaces; reframed by the research as an industry-wide open question.
3. **Custody** — escrow proceeds would touch Tilla's wallet; kept opt-in, small, clearly labeled, and not yet built; default rails stay non-custodial.
4. **Content abuse** — LLM storefronts could be asked to sell illegal goods; every store (agent- or human-created) is screened before deploy, with a takedown path, kept to small-amount digital goods.
5. **Standards churn** — some on-chain identity/reputation pieces lean on draft specs; tagged experimental.
6. **Infrastructure** — single-server + sqlite today; backups, monitoring, restart-safety, an always-on sweeper, and a liveness/readiness watchdog are built-in.

---

## 11. The endgame

As more commerce is done *by* agents than by people filling carts, every seller needs a storefront that agents can find, read, and pay automatically, with verifiable proof of every sale. Tilla's bet is to be **the one-sentence way to create that storefront** — spanning humans and agents, across OKX and every other agent surface, settling in stablecoins with on-chain receipts.

*Describe what you sell → get a live crypto store that sells to humans and AI agents.*

---

## Change-log (what this revision fixed vs. the original)

**Corrected overstatements (marked live/built but actually not):**
1. Create-store fee "~0.2 USDT" → **1 USDT** (hardcoded, no override; the new self-serve flow confirms it).
2. ACP checkout **[live] → [built-dormant]** (503 until `TILLA_ACP_ENABLED`) — in §3 and §4.
3. "Multi-file downloads" → **one file per product** (multi-file bundles are designed).
4. EAS receipts "bind product + content hash" → bind **buyer/store/amount/tx** only; product/content-hash binding is designed.
5. Affiliate "on-chain accrual ledger" → **database** ledger with on-chain-verified *payout* records.
6. Email "broadcasts parked until credentials" → **no broadcast code exists** (designed).
7. Growth-kit "content calendar built-dormant" → **not built** (designed).
8. "Websocket subscription built" → **not built** (designed; polling is the live path).
9. §5 vision-tier "first increment built" (plugins, cross-chain, B2B tiers, federation-ingest) → **DESIGNED, NOT BUILT** per the repo's own `docs/VISION.md`.
10. §6 "exploits [all] marketplace primitives" (present tense) → exploits identity/listing/pay-per-call **today**; the rest designed.
11. Warden paid-hire and task-board folded under one "[partly live]" → split into live / dormant / designed.

**Updated stale items:**
12. §9 "a stranger agent" → **a second self-funded arm's-length wallet** (matches the proof log).
13. Published versioned spec → **spec seed committed** (two test-pinned OpenAPI files); public assembly still pending.

**Added (shipped this session, the original predated):**
14. **Paid self-serve create-store for humans** (§4, §7) — the headline addition.
15. **Human marketplace directory** `/marketplace.html` (§4).
16. **Buyer-library web page** `/library.html` (§4 Fulfillment).
17. **Email receipt + magic-link re-delivery** at checkout (§4).
18. **Site-wide IA overhaul** — nav everywhere, intro-first landing, live store feed, FAQ (§6).
19. **Branded 404 + robots.txt + hub-page sitemap** (§4 SEO).
20. **Affiliate human UI** (dashboard referrals panel, per-buyer share links) + **full dashboard back-office scope** (catalog CRUD, deliverable manager) (§4).
21. **Reputation-ranked discovery** as a named [designed] roadmap item (§4) — the top competitive-research adopt-item.

*Competitive-research doc corrections are tracked separately (Revenue Network shipped Feb 2026 not roadmap; Robinhood Chain integration July 2026; the 80/20 escrow split is historically-documented but removed from live docs — downgrade from "3‑0 verified"; two of the "khala" traction numbers actually track Virtuals' own dashboard and are citable with attribution + the ecosystem-wide caveat).*
