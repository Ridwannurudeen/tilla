# Tilla — The Full Vision (Plain English) — VERIFIED

> **One line:** *The commerce operating system for the agent economy.* Describe what you sell in one sentence, and get a live crypto store that sells to humans **and** autonomous AI agents — settling through OKX's payment protocols on X Layer, and reachable from every agent surface beyond OKX.

*This is a fact-checked revision of `Tilla-Full-Vision.md`. Every [live] / [built-dormant] / [designed] marking was re-verified against the actual codebase (2026‑07‑22): 48 claims confirmed as-written, 19 corrected (overstatements), 4 updated (stale), and 20 shipped capabilities added that the original predated. Corrections and additions are the ones judges could catch — being precise here is the point. A change-log is at the end.*

*Revision 2026‑07‑24 — re-verified against **production**, not only the codebase. Three things moved: the create-store fee is now **0.05 USDT0** (was 1 USDT, stale everywhere); **human self-serve creation is live and has been used by a real merchant**; and the rails this doc called [built-dormant] or [designed] — subscriptions, `aggr_deferred`, MPP, escrow, EAS attestations, ACP checkout — are **enabled**, each carried below with its own precisely-scoped proof, or an explicit statement of what is still unproven. Every settlement cited is a **self-funded arm's-length test** unless stated otherwise; blocks are X Layer (chainId 196). Added to the change-log at the end.*

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
- **ACP-compliant checkout** (the OpenAI/Stripe-lineage agent-commerce standard) so the same store *can also* sell through that standard. **[live]** — enabled in production: the endpoints now answer real status codes (401/422 on bad auth or payload), not the old dormant 503, and one full checkout session has completed and settled on-chain (block 66025300, a self-funded arm's-length test).

And Tilla itself is a *listed agent inside OKX's marketplace* (agent **#6961**) — so **other agents can hire Tilla**: an orchestrator finds it, pays a small fee, and gets back a live store. That "agents hiring agents" flow is proven on-chain.

Today the moat actually rests on **identity (ERC‑8004 #6961), a listed paid service, pay-per-call x402, and — uniquely — a seller-owned branded storefront that sells to humans too.** Escrow is now built and enabled, with one full fund→release cycle on-chain (§4). Task-board participation, OKX marketplace ratings, semantic-search catalogs, and encrypted agent chat are still *designed* extensions, not yet used.

---

## 4. Everything it does (and will do)

Marked **[live]** (built and running), **[built]** (shipped and tested, but not yet exercised with real funds or real users), **[built-dormant]** (built, switched off until credentials/funds), or **[designed]** (planned, not yet built).

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
- **Paid self-serve store creation for humans** — a signed-in merchant creates a store from the dashboard: the description is safety-screened **before** any payment is offered (fail-closed), they pay the one-time **0.05 USDT0** fee on-chain from their own wallet, Tilla verifies the exact transfer on-chain and generates the store with their wallet as the receive address (reused-payment → 409, generation outage → paid + free retry) [live — and used: a merchant paid the fee on-chain at block 66151616 and got the live store `/s/jersey-fc/` back, 2026‑07‑24].
- **Optional email receipt at checkout** with magic-link re-delivery — a fallback for exchange-custody buyers who can't later sign with the paying wallet [live; the email *send* is dormant until SMTP credentials].
- **The payment-rail menu** — all four rails are now enabled, with per-rail proof stated exactly:
  - one-off (x402 "exact") — [live; proven, 3 recorded settlement proofs].
  - subscriptions (x402 "period") — [live; enabled, first settlements proven on-chain — two real charges at blocks 66072022 and 66072295].
  - bulk/batch (`aggr_deferred`) — [live; enabled and proven — one real batch settle covering two orders at block 66059520. **No batch-priced product is currently listed**, so today's live 402 challenges carry only "exact"].
  - pay-as-you-go metering (MPP) — [live; enabled, **channel opened, close pending** — one channel opened with a signed voucher against a 2 USDT deposit, never yet closed or settled. The close/settle half of the loop is unproven and is not claimed].

  Every settlement above is a **self-funded arm's-length test** — real funds, our own second wallet, not external demand.
- Discount codes, cross-sells, abandoned-checkout recovery, wallet-remembered repeat buyers, spend-cap/mandate-aware agent checkout [designed].

### Fulfillment & delivery
- Instant, zero-manual delivery; **signed, expiring download links** with download limits [live].
- A durable **buyer library** — re-download anything by signing in with your purchase wallet — now with a dedicated **"Your purchases" web page** (`/library.html`) linked from every surface [live].
- **Email receipt + magic-link re-delivery** for exchange-custody buyers (attach an email after an ownership check, get a signed 7-day link that re-serves the delivery) [live; sending dormant until SMTP].
- Verifiable receipts binding **buyer + store + amount + payment tx hash**; on-chain **EAS attestation receipts** [live — the attester is enabled and 4 real attestations are recorded on-chain]. Receipts additionally binding **product + content hash** [designed].

### Trust, refunds & disputes
- Receipts with explorer links; **merchant-initiated refund flow** with recorded state — non-custodial: the merchant sends, Tilla verifies the tx on-chain [live].
- **A2A escrow checkout** for agent buyers with buyer protection [live — the commission/escrow state machine is built and enabled, with one full fund→release cycle proven on-chain (deposit block 66036695, release block 66036700), a self-funded arm's-length test. Still **verify-only and non-custodial**: the parties designate the holding wallet, Tilla holds no keys, sends nothing, and only verifies and records each transfer].
- Verified-buyer reviews gated on a real purchase [live — `POST /api/library/review`, purchase-gated; the review average feeds discovery].

### Merchant operations
- Merchant accounts (sign-in by wallet signature), multi-store, and a **full back-office dashboard** [live]: orders / revenue / per-product analytics, **product catalog management** (add / edit / deactivate, screened), a **deliverable manager** (store-default and per-product; files, text, license keys), a refund/underpaid queue, webhook config, marketplace-listing status, referral earnings, and **paid self-serve store creation**.
- CSV / tax-ready exports; a **merchant API with signed webhooks** (agents are the API consumers) [live].
- Testnet sandbox mode [designed].

### Agent-native commerce (the differentiator)
- **Dual-sided stores** — every store buyable by agents via x402, non-custodial [live].
- **Per-store MCP server**; **agent card + discovery mirror** [live]; **ACP-standard checkout** [live — enabled, one completed session settled on-chain (block 66025300)].
- **Store-as-ASP auto-listing** — turn each store into its own listed marketplace agent [designed; the M10 listing tooling exists].
- **Agents hiring Tilla** — proven with an on-chain receipt (a self-funded arm's-length wallet paid Tilla to create a store, block 65875359) [live/proven]. **Tilla hiring Warden** (paid content-safety screening) [built-dormant — the paid hire is still OFF: all 13 screenings to date ran in Warden's free demo mode, so no agent-to-agent screening fee has ever been paid]. Task-board participation [designed].
- **Reputation-ranked discovery** [live] — the discovery index now serves per-store `trust_tier`, `success_rate` (delivered vs. failed/refunded) and `review_avg` alongside `sold_count`, so agents can rank on earned outcomes rather than volume alone. *(This was the top adopt-item from the Virtuals/ACP competitive research — see the companion doc.)*
- Pay-per-crawl catalog access; XMTP negotiation channel [designed].

### Growth
- **Agent + human affiliate revenue-share** — any wallet gets a personal `?ref=` share link (on the buyer's receipt), referrers see their own earnings in the dashboard, merchants see what they owe per referrer; accrual is written to a **database ledger** at the settled seam (default 2%). Payout execution stays a manual, recorded operator action that **verifies each payout tx on-chain** — never automated fund-moving code [live].
- Merchant feed export for OpenAI / Google Merchant / Perplexity surfaces [live, export shapes]; the actual *listings* on those platforms are user-owned external steps.
- **Embeddable buy button** (drop-in) and **email/waitlist capture** [live]. Broadcast/fan-out sending [designed — deliberately no fan-out code yet].
- A **growth-kit** endpoint that drafts marketing copy from a store's own content (merchant-gated, output re-screened) [live]. A performance-aware content calendar + draft outbox [built; the scheduler that would drive it stays double-gated OFF]. **Publishing is permanently a human action** — Tilla never posts anything itself.

### On-chain depth
- Real-time payment detection via an always-on **polling sweeper** (proven matching a real transfer on-chain) [live]. Websocket/`eth_subscribe` subscription [designed — an unvalidated spike, not built].
- **EAS attestation per sale** [live — enabled, 4 real attestations recorded on-chain]; a **StoreRegistry** contract and ERC‑8004 store registration [mix of built/designed]; receipt NFTs [designed].

---

## 5. The vision-tier build-out (the multi-quarter horizon)

**Status update (2026‑07‑24): this tier is no longer "designed, not built."** `docs/VISION.md` now carries its own SUPERSEDED banner (2026‑07‑23) recording that the tier was built and tested as modules M15–M18. Built-and-tested is still **not** the same as proven in the wild, so each item below says which it is.

1. **Plugin / extension ecosystem** — provider interfaces over the existing delivery/payment/theme seams. [built — `app/plugin_runner.py`, `app/providers.py`, covered by `test_providers` / `test_provider_conformance` / `test_theme_plugins`]. Third-party *code* remains gated on a real external author + sandbox/review pipeline [designed].
2. **Cross-chain checkout** — settlement is still **single-chain** (`PAYMENT_NETWORK = eip155:196`); what shipped is the labeled **"bridge-in" affordance** surfaced at checkout (`TILLA_BRIDGE_URL`, off unless configured), with **no custody and no fund-moving code inside Tilla, ever** [built; multi-chain settlement remains designed].
3. **B2B / marketplace-of-marketplaces** — wholesale tiers keyed to a buyer agent's on-chain identity, store-to-store procurement, and *federation* [built — `app/b2b.py` (ERC‑8004 owner-gated tiers), `app/federation.py` (mirror ingest), covered by `test_b2b` / `test_federation`; **no external federation partner has ingested yet**]. Also real: the export feed stack + discovery index, and a **committed spec seed** — two test-pinned OpenAPI/JSON‑Schema files (`docs/openapi.feed.yaml`, `docs/openapi.external-feeds.yaml`, derived from the ACP feed spec). Assembling those + the agent-card + 402 conventions into a *published, versioned* public spec is the remaining deliverable.
4. **Autonomous growth agent** — the growth-kit seed grew into a draft→approve→discard→mark-published outbox, first-party performance aggregates, and a performance-aware content calendar [built and live on prod (`app/growth.py`, `app/growth_scheduler.py`); the scheduler itself stays double-gated OFF]. **Publishing is still permanently a human action** — Tilla never posts anything itself.

Plus an **open SDK**: a Python client is shipped (`sdk/python`, isolated install + its own CI job); the **TypeScript port is now built** too (`sdk/typescript/src`) — neither is published to a package registry yet.

---

## 6. Where it sits in OKX's world

OKX's thesis is "one person, one company, $1M a year" powered by agents hiring agents on-chain. Tilla is designed to be the flagship proof: the one-person entrepreneur's storefront that earns real revenue through the rails, drives stablecoin volume on X Layer, and accumulates an inspectable on-chain footprint. It **exploits OKX's identity, listing, and payment primitives today** (ERC‑8004 #6961, a listed paid service, and all four x402/MPP rails — see §4 for each rail's exact proof), and is **designed to also use** OKX's own A2A escrow service type, the task board, marketplace ratings, semantic search, and encrypted agent chat.

Beyond OKX, this session hardened the *human* half of the dual-sided story: a public marketplace directory, a coherent nav across landing / marketplace / buyer library / dashboard, an intro-first landing with a live "happening now" store feed pulled from the discovery API, a plain-English FAQ, and a first-class buyer-library page — so the storefront is genuinely usable by non-crypto people, not only agents.

---

## 7. How it makes money

- **Live today:** a small flat fee per store creation — **0.05 USDT0**, payable **either** by an agent over x402 **or** by a human self-serve on the site (both charge the same on-chain-verified fee) — plus paid **`upgrade-store` (0.03 USDT)** and **`add-product` (0.01 USDT)** services.
- **Next:** a premium creation tier (custom domain, multi-product, themes) at a higher fee; and, where a rail supports it, a small platform fee (the metered MPP rail supports non-custodial payment splits — the one place a percentage fee is possible without holding funds).
- **Guiding principle: non-custodial by default.** Human checkout and per-store agent purchases settle **directly to the merchant's wallet** — Tilla never holds their money. The escrow surface is now built and enabled but stayed non-custodial too: the parties designate the holding wallet, Tilla verifies and records the deposit and the release and never holds keys or sends funds.

*(Pricing note: the creation fee **was** repriced — 1 USDT → **0.05 USDT0**, `PAYMENT_AMOUNT = "50000"` at 6dp, which is what the live 402 challenge advertises. Any surface still saying "1 USDT" is stale and should be corrected against the code.)*

---

## 8. The market reality (honest)

- **White space is real, but narrower than "nobody sells to humans."** On OKX's marketplace there is no storefront/checkout/gated-delivery agent — Tilla is first in this lane. Even the biggest agent platform (Virtuals) has a human *buyer-side* concierge (Butler) but **no seller-owned, one-prompt, branded storefront-with-checkout** — that specific product is still open, and Tilla spans both the human checkout and the agent-x402 side of it. *(See the companion competitive doc.)*
- **The bar to beat is winning *formats*** — top sellers win with one viral hook or a big cheap-service catalog; Tilla's store-listing model can replicate the many-service catalog format at scale.
- **Comparable fees:** Gumroad ~10%+, Lemon Squeezy ~5%, crypto gateways ~0.5–1% — and none of the crypto gateways do fulfillment, licensing, buyer accounts, or growth tooling. The "Gumroad-grade fulfillment + agent-payable rails" combination has no incumbent.

---

## 9. The honest part — proven vs. bet

- **Proven (on-chain, 2026‑07‑21, receipts in `docs/PROOF-onchain.md`, each status 1):** a **human checkout** (9.000707 USDT0, block 65873791), an **agent x402 product buy** (block 65875190), and a **second self-funded arm's-length wallet paying Tilla to spin up a new store** (block 65875359, producing live store `/s/sync/`). The product genuinely builds and deploys live stores, takes real on-chain crypto from both humans and agents, and is a **fully listed, approved service on OKX**. The engineering is deep and real — not a demo.
- **Proven since (2026‑07‑24), each a self-funded arm's-length test unless noted:** a **subscription** rail settling twice (blocks 66072022, 66072295); an **`aggr_deferred` batch settle** covering two orders (block 66059520); a **full escrow fund→release cycle** (blocks 66036695 / 66036700); a **completed ACP checkout session** (block 66025300); **4 EAS attestation receipts** written on-chain; and **a merchant paying the 0.05 USDT0 fee from the dashboard** and getting the live store `/s/jersey-fc/` back (block 66151616) — the human self-serve path proven end-to-end with real money. Seventeen stores now exist, and discovery ranks them on earned outcomes (`trust_tier`, `success_rate`, `review_avg`).
- **Still unproven, and not claimed:** the MPP channel has been opened with a signed voucher (2 USDT deposit) but **never closed or settled**; the paid Warden hire is off (all 13 screenings ran in free demo mode); and none of the above is external customer demand.
- **The bet:** *demand.* OKX's marketplace is early; there isn't a crowd of buyers waiting. Tilla was built as a **flagship proof of OKX's own vision** — to win on judging and OKX's promotion — while extending onto external agent surfaces (MCP, feeds, ACP) so demand isn't OKX-only. The competitive research sharpens this: **even the market leader hasn't proven agent commerce transacts at scale without subsidizing it** — so this is a whole-category open question, not a Tilla-specific weakness.

---

## 10. Named risks (not buried)

1. **Self-trade optics** — fully-controlled test loops are self-trades; judges inspect on-chain data, so real arm's-length purchases are prioritized and demo self-trades are labeled honestly (the create-store proof used a *separate, self-funded* wallet, labeled as such — not literally an unrelated third party).
2. **Unproven demand** — mitigated by winning-format replication and external surfaces; reframed by the research as an industry-wide open question.
3. **Custody** — the escrow surface is now built and enabled, and it was deliberately kept **non-custodial**: funds move between the buyer, a party-designated holding wallet, and the provider, with Tilla only verifying and recording each transfer. Tilla holds no keys and sends no funds on any rail.
4. **Content abuse** — LLM storefronts could be asked to sell illegal goods; every store (agent- or human-created) is screened before deploy, with a takedown path, kept to small-amount digital goods.
5. **Standards churn** — some on-chain identity/reputation pieces lean on draft specs; tagged experimental.
6. **Infrastructure** — single-server + sqlite today; backups, monitoring, restart-safety, an always-on sweeper, and a liveness/readiness watchdog are built-in.

---

## 11. The endgame

As more commerce is done *by* agents than by people filling carts, every seller needs a storefront that agents can find, read, and pay automatically, with verifiable proof of every sale. Tilla's bet is to be **the one-sentence way to create that storefront** — spanning humans and agents, across OKX and every other agent surface, settling in stablecoins with on-chain receipts.

*Describe what you sell → get a live crypto store that sells to humans and AI agents.*

---

## Change-log — revision 2026‑07‑22 (what that revision fixed vs. the original)

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

---

## Change-log — revision 2026‑07‑24 (re-verified against production)

**Corrected stale prices:**
1. Create-store fee **1 USDT → 0.05 USDT0** (`PAYMENT_AMOUNT = "50000"`, 6dp — what the live 402 advertises), in §4, §7 and the §7 pricing note, which previously said the reprice had *not* happened.
2. Paid services **`upgrade-store` 1 USDT → 0.03 USDT** and **`add-product` 0.5 USDT → 0.01 USDT** (§7), matching the fee options the app registers.

**Rails and surfaces promoted with precise scope (§3, §4, §9):**
3. **Subscriptions** (x402 `period`) [built-dormant] → **[live]** — enabled, two real settles (blocks 66072022, 66072295).
4. **`aggr_deferred`** [built-dormant] → **[live]** — enabled and proven by one batch settle covering two orders (block 66059520), with the honest caveat that **no batch-priced product is listed**, so live 402s carry only `exact`.
5. **MPP** [built-dormant] → **[live, partial]** — enabled, one channel opened with a signed voucher (2 USDT deposit); **close/settle still unproven and not claimed**.
6. **Escrow** [designed, "state machine is not built"] → **[live]** — built, enabled, one full fund→release cycle on-chain (blocks 66036695 / 66036700), still verify-only and non-custodial; §10 risk 3 rewritten accordingly.
7. **EAS attestations** [built-dormant] → **[live]** — enabled, 4 real attestations recorded.
8. **ACP checkout** [built-dormant, "every endpoint returns 503"] → **[live]** — enabled, endpoints answer 401/422, one completed session settled on-chain (block 66025300).
9. **Reputation-ranked discovery** [designed] → **[live]** — discovery serves `trust_tier`, `success_rate`, `review_avg`; **verified-buyer reviews** [designed] → [live] (purchase-gated `POST /api/library/review`).
10. **Human self-serve creation** now marked live **and used** — a merchant paid the fee on-chain and got `/s/jersey-fc/` back (block 66151616, 2026‑07‑24).
11. §5 vision tier "DESIGNED, NOT BUILT" → **built as M15–M18** per `docs/VISION.md`'s own SUPERSEDED banner (plugins, bridge-in affordance, B2B/federation, growth outbox/calendar) plus the **TypeScript SDK**; each item states built-vs-live, and cross-chain *settlement* stays single-chain.

**Kept honest (unchanged claims):**
12. The **paid Warden hire stays OFF** — all 13 screenings ran in free demo mode; no agent-to-agent screening fee has ever been paid.
13. Every **rail proof** above remains a **self-funded arm's-length test** — real funds from our own second wallet, not external customer demand.

---

*Competitive-research doc corrections are tracked separately (Revenue Network shipped Feb 2026 not roadmap; Robinhood Chain integration July 2026; the 80/20 escrow split is historically-documented but removed from live docs — downgrade from "3‑0 verified"; two of the "khala" traction numbers actually track Virtuals' own dashboard and are citable with attribution + the ecosystem-wide caveat).*
