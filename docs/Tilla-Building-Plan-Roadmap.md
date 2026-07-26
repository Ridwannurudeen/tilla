# Tilla — Full Building Plan & Roadmap

> ## ⚠️ HISTORICAL — SUPERSEDED (banner added 2026-07-26)
> Written 2026-07-22 and **not maintained since**. Almost every status marking in it is now wrong:
> the create-store fee reads 1 USDT (it is **0.05**), subscriptions / MPP / `aggr_deferred` / EAS /
> paid-Warden-hire / ACP are listed `[dormant]` (**all six are enabled and proven on-chain**), and
> escrow, wholesale tiers, cross-chain, plugins, federation, the growth agent, reviews, the TS SDK
> and custom domains are listed `[designed]` (**all built**). The migration head and test count are
> stale too.
>
> **Current sources of truth:** `docs/ROADMAP.md` (the map), `docs/BUILD.md` (committed scope),
> `docs/PROOF-onchain.md` (what actually settled), `docs/VISION.md` (M15–M18 built state).
> Kept for history only.

*The complete path from today's verified state to the vision: **a commerce operating system for the agent economy** — the one-sentence way to create a storefront that agents can find and pay automatically, spanning humans and agents, non-custodial, on-chain-provable.*

*Anchored on the code-verified state as of 2026‑07‑22 (not the aspirational vision doc). Every phase marks status — **[live]** / **[dormant]** (built, flag-off) / **[designed]** (not built) / **[research]** (new, from the Virtuals/ACP teardown) — plus effort (S/M/L), primary files, and gating (buildable-now vs needs-funding/creds vs needs-hardening). Nothing here requires pivoting to or merging with Virtuals; the research is used only to make **our** stores easier for agents to find and pay.*

---

## Principles (non-negotiable, apply to every phase)

1. **Non-custodial by default.** Human checkout and per-store agent purchases settle **direct to the merchant's wallet**. The only custodial surface is opt-in, labeled escrow (Phase 4) — and even there, funds move by verified user action, never by automated fund-moving code.
2. **Fail-closed screening.** Every store (agent- or human-created) is content-screened before it goes live; ambiguous → refuse, never permit.
3. **Evidence integrity.** Simulated stays labeled simulated; testnet is never called mainnet; public numbers are generated, not typed. A rail ships **flag-off** until a real credential + funded buyer prove a live settlement.
4. **Verify before enabling.** Every dormant rail's enablement is preceded by a read-only capability probe (e.g. facilitator `/supported`) — the discipline already used for `AGGR_DEFERRED`.
5. **Machine-first surfaces are the moat.** Every capability a human gets, an agent should get too (feed/MCP/agent-card), and every purchasable action should be self-describing (requirement schema).

---

## Current state inventory (verified — build on this, not the marketing)

**[live]:** one-sentence LLM store generation + multi-product catalogs; per-product file downloads + license keys; **paid self-serve create-store for humans** (1 USDT, on-chain-verified) *and* agent x402 create-store; hosted checkout (QR, countdown, wallet-connect + X-Layer auto-switch, under/over/late-payment handling); signed expiring download links; buyer library + `/library.html`; **email receipt + magic-link re-delivery** (send dormant until SMTP); merchant refunds (non-custodial); merchant accounts + multi-store + full dashboard (catalog CRUD, deliverable manager, referrals, webhooks); **affiliate rev-share** (DB ledger, on-chain-verified payouts) + human share links; per-store MCP + `feed.json`/JSON-LD/`llms.txt` + agent card + discovery API; **human marketplace directory** + site-wide nav + branded 404/robots/sitemap; polling payment sweeper; growth-kit copy drafting; Python SDK; OKX agent **#6961**.

**[dormant]** (built, flag-off until creds/funds + hardening): subscriptions (x402 "period"); pay-as-you-go metering (MPP); batch (aggr_deferred); on-chain **EAS attestation receipts**; paid **Warden** content-screening hire; **ACP-standard checkout** (OpenAI/Stripe standard, 503 until `TILLA_ACP_ENABLED`).

**[designed]** (not in code): A2A escrow / custom-build; wholesale/B2B tiers; cross-chain; plugins; federation; autonomous growth agent; membership tiers / versioned releases / PWYW; reviews; websocket payment subscription; TS SDK; link-in-bio profile; custom domains.

**Migration head:** `0010_self_serve_create_store`. **Tests:** ~661 green, CI green. **Two nginx changes live-only** (404 error_page + `/ready` proxy), documented in `deploy/nginx-m14.snippet`.

---

## The roadmap

### Phase 0 — Finish the foundation (unblocks everything dormant) · buildable-now + user-gated

*Goal: make the dormant rails safe to switch on, and close the known correctness gaps. Small, high-leverage, mostly terminal-owned.*

| Item | Status | Effort | Files | Gating |
|---|---|---|---|---|
| **Migration 0011** — `attest_nonce` + `settle_ref` on `orders` (plain `add_column`, no batch rebuild) + round-trip test | designed | S | `alembic/versions/0011_*`, `app/models.py` | buildable-now (chains off 0010) |
| **aggr_deferred reconciliation poller** (settles batched accepts) | dormant→hardening | M | `app/mpp.py`/sweeper, `app/checkout.py` | buildable-now |
| **EAS attester crash-window** (idempotent re-attest, no double-attest) | dormant→hardening | M | `app/attest.py`, `app/main.py` lifespan | buildable-now |
| **Websocket payment subscription** (replace/augment polling; probe `eth_subscribe`) | designed | M | `app/chain.py`, sweeper | buildable-now (optional) |
| Funding / faucet / live OKX creds / Warden creds | — | — | VPS `.env` | **user-gated (see parallel track)** |

**Exit criteria:** 0011 applied; poller + attester hardened; a read-only probe confirms each rail's facilitator support. *Then* Phase 2 enablement is safe.

---

### Phase 1 — Agent-native findability & payability (the vision's core: "agents can find & pay automatically") · buildable-now, no funding needed

*Goal: make every Tilla store a first-class, self-describing, reputation-ranked ACP-grade citizen — using our own code, on our own rails. This is where the research pays off most, and it's all shippable without a single external dependency.*

| # | Item (research-derived) | Effort | Primary files | Why it serves the vision |
|---|---|---|---|---|
| 1.1 | **Reputation-ranked discovery** — add `success_rate` (delivered vs failed/refunded), `unique_buyer_count` (distinct payer addr — x402 gives it free), `last_sale_at`, `is_new`; add a `sort` param; keep `sold_count`. All computable in the existing subquery, **no migration**. | S | `app/agentic.py` (`_discovery_rows`, `_discovery_row`, `feed_json`, both `/discovery` routes) + tests | "Agents can *find* it" — buyers sort providers on performance; today we emit only `sold_count`, which is weaker *and* more gameable than ACP's set. |
| 1.2 | **Requirement-schema per purchasable action** — emit JSON Schema (checkout: variant/qty/email w/ `required` + `format`; create-store: the existing `CreateStoreBody`) into the agent-card skills, `feed.json`, and per-store MCP tools. Add `sample_request`/`sample_deliverable`. | S | `app/agentic.py` (agent_card, feed, MCP) importing pydantic bodies from `app/main.py` | "Agents can *pay automatically*" — ANY buyer concierge (Butler, Claude, OKX agents) can collect the right inputs and transact without human back-and-forth. The machine-negotiation contract. |
| 1.3 | **SLA / delivery-time promise** per offering/product, surfaced in `feed.json` + the buy-flow confirmation as an ETA, with a **consequence** on breach (auto-refund where refundable; `success_rate` penalty + `is_delinquent` flag otherwise). | S | `app/agentic.py`, `app/models.py` (product/deliverable field + migration), `app/checkout.py` | Trust signal agents rank on; "an SLA without a consequence is what Tilla has today (none)." |
| 1.4 | **Root-level `/mcp`** — cross-store browse/search/buy tools (today MCP is per-store only) so **any** chat agent becomes the "Butler" concierge; advertise in the agent card. | S/M | `app/agentic.py` (new root `/mcp` + slug-parameterized tools) | Findability + automation without us building a concierge. |
| 1.5 | **Graduation-style store trust tier** — automated tier from delivered/expired signals (thresholds lower than Virtuals' 10 given our volume) + auto-demote on repeated expiries; surface in discovery. | S/M | `app/agentic.py`, `app/models.py` | A public, un-fakeable trust ladder — the reputation layer agents need. |
| 1.6 | **Evaluation → reputation (not money)** — since x402 settles instantly, count a sale toward `success_rate` only after a buyer-confirmation window (auto-confirm after N days = ACP "skip" mode; explicit confirm/reject = self-eval). Optionally emit an ACP-style evaluation receipt. | M | `app/checkout.py`, `app/agentic.py`, `app/models.py` | Makes `success_rate` meaningful and dispute-aware without adding custody. |
| 1.7 | **Sandbox / hidden-until-proven mode** (visibility first) — a `hidden`/`sandbox` store state kept out of production discovery/aggregate feed/sitemap until it clears a threshold; owner + agent can preview. *(Real X-Layer testnet settlement is Phase 5, gated on a facilitator probe.)* | M | `app/models.py` + migration, `app/agentic.py`, `app/external_feeds.py`, `app/main.py` (sitemap) | Anti-spam + trust; mirrors ACP's sandbox→production gate. |
| 1.8 | **"Preview your store as an agent sees it"** — a dashboard view rendering the store's feed/MCP/agent-card exactly as a buyer-agent would consume it. | S | `themes/_dashboard.html`, reuse `app/agentic.py` outputs | Merchant confidence; cheap, demo-friendly. |

**Exit criteria:** a third-party agent can discover a Tilla store ranked by real performance, read a typed schema for every purchasable action, see an SLA + ETA, and complete a purchase over x402 with zero human glue — all verifiable in a demo.

---

### Phase 2 — Turn on the built rails (the payment-rail menu of the vision) · needs funding/creds + Phase 0 hardening

*Goal: light up the dormant rails, one verified live settlement at a time, so Tilla is genuinely one-off + subscription + metered + batch.*

| Item | Status | Effort | Files | Gate |
|---|---|---|---|---|
| **Subscriptions** (x402 "period", payer-binding) | dormant | S (enable) + verify | `app/subscriptions.py`, sidecar | flag + creds + one live period settle |
| **Pay-as-you-go / metering (MPP)** | dormant | S (enable) + verify | `app/mpp.py` | flag + creds + one live channel |
| **Batch (aggr_deferred)** | dormant (Phase 0 poller) | S (enable) | `app/mpp.py`/checkout | poller done + one live batch |
| **On-chain EAS attestation receipts** | dormant | S (enable) + M (extend) | `app/attest.py` | attester key + Phase 0 crash-window; **extend receipt to bind product + content hash** (today: buyer/store/amount/tx only) |
| **Paid Warden content-screening hire** (agents-hiring-agents, both directions) | dormant | S (enable) | `app/warden_hire.py` | flag + payer key |
| **ACP-standard (OpenAI/Stripe) checkout** | dormant | S (enable) | `app/acp.py` | flag; a second standard our stores sell through |

**Each rail:** read-only `/supported` probe → enable flag in VPS `.env` → one real funded settlement → label it proven in `docs/PROOF-onchain.md`. **Never** flip a flag without the live proof.

---

### Phase 3 — Escrow, custom-build & wholesale (the vision's "escrow + wholesale pricing for agent buyers") · larger builds, ACP-blueprinted

*Goal: the higher-order commerce primitives — commissioned/custom builds with buyer protection, and agent-buyer wholesale — the parts of the vision that need real new machinery.*

| Item | Status | Effort | Files | Notes |
|---|---|---|---|---|
| **A2A escrow / custom-build job machine** | designed | L | `app/models.py` (`CommissionJob` + migration), new `app/jobs.py`, reuse `checkout.transition`/reaper/delivery/Refund | Model on ACP's proven `open→budget_set→funded→submitted→completed` + optional `evaluatorAddress` gating release. **Phase-gate the escrow contract** (custodial → opt-in, labeled). This is the vision's "escrow" pillar with a validated blueprint. |
| **Requirement-schema + offering envelope, full** | research | M | `app/agentic.py`, `app/models.py` | Generalize Phase-1 schemas into a full offering envelope (`priceType` fixed/percentage/**subscription**, `deliverable`, `requiredFunds`, `nextActions` in delivery payloads) so commissioned + subscription + metered offerings all self-describe. |
| **Wholesale / B2B price tiers for agent buyers** | designed | M | `app/models.py` (`Product.pricing_params`), `app/payment.py`/resolvers, `app/agentic.py` | Tiered pricing keyed to a buyer agent's on-chain identity (ERC-8004); reuses the per-request price resolver already in the x402 wiring. |
| **Verified-buyer reviews** (gated on a real purchase) | designed | M | `app/models.py`, `app/dashboard.py`, themes | Feeds the reputation layer with content, not just counts. |
| **Membership tiers / versioned releases / PWYW** | designed | M each | `app/engine.py`, `app/models.py`, checkout | Storefront depth for creators. |

---

### Phase 4 — Reach, distribution & monetization · mix of buildable + user-gated

*Goal: get Tilla stores in front of demand wherever it is, and mature the money model — without touching the non-custodial core.*

| Item | Status | Effort | Notes |
|---|---|---|---|
| **List Tilla's create-store as a Virtuals ACP provider offering** | research | M | **A distribution channel, not a merge.** Offering "Create a crypto storefront," priced USDC on Base (our own fee — no custody conflict). Small Node `acp-node-v2` sidecar → `engine.create_store`. Taps Butler's consumer demand. **User-gated:** Base wallet + whitelisted signer + `app.virtuals.io/acp/new` registration. *Re-verify ACP fees/graduation at execution — Virtuals docs are mid-restructure.* |
| **Monetization: hosting/subscription tier** | research/designed | M | Benchmark the Agent Console shape — small one-time fee **+ optional monthly hosting auto-deducted + free trial + "live in minutes."** The **metered (MPP) rail is the one non-custodial place a % / recurring platform fee can live.** |
| **External surfaces** (OpenAI/Google/Perplexity feed listings; embeds; email broadcast) | live-exports / dormant | S–M | Export shapes exist; the *listings* + broadcast sending are user-owned/creds-gated. |
| **Link-in-bio merchant profile + custom domains** | designed | M | Human distribution surface. |
| **soul.md-style plain-language store editing** | research | S | "Edit your one-line description → re-renders next build, no redeploy" — mirrors Console's edit model; we already persist `content` for re-render. |
| **Butler-style buy-flow polish** | research | S | In store chat / MCP buy tool: show price + balance context, collect schema fields conversationally, render a confirmation summary (items, total, ETA, store) + require explicit approval before invoking x402. |

---

### Phase 5 — Vision-tier (multi-quarter horizon) · [designed] — honest long-range

*Goal: the operating-system-scale pieces. All currently DESIGNED (per `docs/VISION.md`); sequence them last, each gated on a real external dependency.*

1. **Cross-chain checkout** — single-chain today (`eip155:196` hardcoded); add facilitator-supported chains and/or a labeled "bridge-in" affordance. **No custody, no fund-moving code.** Gated on external facilitator/bridge support (probe first).
2. **Plugin / extension ecosystem** — provider interfaces from the existing delivery/payment/theme seams; third-party *code* gated on a real external author + sandbox/review pipeline.
3. **B2B / marketplace-of-marketplaces + federation** — assemble the committed spec seed (the two test-pinned OpenAPI files + agent-card + 402 conventions) into a **published, versioned public spec**; ingest a second operator's feed. Gated on a second independent operator.
4. **Autonomous growth agent** — from the growth-kit seed to a per-store content calendar with performance feedback, always ending in a **draft → approve → publish** queue (human presses publish).
5. **TypeScript SDK** — port the shipped Python client when a browser/Node consumer needs it.

---

## The parallel USER-GATED track (gates Phase 2+; only you can do these)

These are not code — they're the credentials/funds/actions that unlock enablement, and the submission itself:
- **Funding + faucet** (a funded buyer wallet + gas) — unblocks every "prove a live settlement" step.
- **Live OKX / facilitator creds + Warden creds** — unblock subscriptions, MPP, batch, paid Warden, EAS.
- **A Base wallet + ACP registration** — for the Phase-4 distribution experiment.
- **On-chain listings / ASP submissions**, GitHub, and the **hackathon submission** (X/#OKXAI post + ≤90s demo video + Google form).

I prepare each of these to the edge; you perform the irreversible/outbound step.

---

## Sequencing & dependency logic (the "why this order")

- **Phase 1 is first because it needs nothing external** and it *is* the vision's core ("agents find & pay automatically"). It also makes the pitch visibly true. Do it regardless of funding.
- **Phase 0 hardening runs alongside Phase 1** (terminal-owned), because it's the safety gate for Phase 2.
- **Phase 2 waits on funding/creds** (user-gated) + Phase 0. Each rail is one verified settlement, never a bulk flip.
- **Phase 3 (escrow/wholesale)** is the largest lift and depends on Phase 1's offering-envelope groundwork; escrow introduces the only custodial surface, so it's opt-in and late.
- **Phase 4** can start once Phase 1 exists (schemas make the ACP listing + external surfaces meaningful).
- **Phase 5** is long-range, each item gated on a real external dependency — never faked.

## Definition of done for the vision
A seller types one sentence → gets a branded store that (a) a human buys on a page, (b) **any** agent discovers ranked by real performance, reads a typed schema for, and pays automatically over the buyer's choice of rail (one-off / subscription / metered / escrow), (c) settles non-custodially with an on-chain, product-and-content-bound receipt, (d) carries public, un-fakeable reputation — across OKX and every external agent surface. Every layer above moves one step toward that.

*Companion docs: `Tilla-Full-Vision-VERIFIED.md` (state truth), `Tilla-Virtuals-ACP-Research-Report.md` (the adopt-list with sources), `Virtuals-Research-VERIFICATION-ADDENDUM.md` (competitor corrections). Re-verify perishable competitor facts (ACP fees, graduation, subsidy status) at execution time.*
