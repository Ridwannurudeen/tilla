# Tilla × Virtuals / ACP — Independent Research Report

*Two research passes (9 agents total), 2026‑07‑22. Every load-bearing claim was fetched from a live primary source — Virtuals GitHub (`Virtual-Protocol/acp-node-v2`, `acp-cli`, `agent-commerce-protocol`, `acp-python`), the live ACP registry API (`acpx.virtuals.io`), on-chain contract reads on Base, the live `acp-x402.virtuals.io` 402 endpoint, `os.virtuals.io/acp`, and `whitepaper.virtuals.io` — and re-checked by a skeptical verifier. Confidence and staleness are flagged. This supersedes the "80/20 split" and "x402-interop" claims in the earlier `Virtuals-vs-Tilla-Research.md`.*

---

## 0. The three questions you asked, answered

**Q1. Does the Virtuals team have a platform similar to Tilla (describe → get an agent service)?**
**Partly — in *form*, not in *output*.** Virtuals has the **Agent Console** (their EconomyOS "describe → live agent in minutes" product): 5 steps (name + **token symbol**, network, description, runtime, Launch), 3 USDC one-time + 7‑day trial then **20 USDC/month** hosting auto-deducted from the agent's own wallet. But it produces a **tokenized autonomous trading/posting agent from templates — no catalog, no checkout, no storefront**, and tokenization is baked in (token symbol is step 1). Their other "create" surfaces are a **token launcher** (Genesis/bonding curve) and an **"Add Job" listing form** (register a service in their custodial marketplace). *None takes a merchant's description and returns a branded store that sells goods.*

**Q2. Do they have something structurally similar to Tilla (a storefront/commerce builder)?**
**No — verified exhaustively.** The researcher queried the **live ACP registry across all 44,050 registered agents** and got **zero** matches for storefront / online-store / build-a-store / checkout-page / digital-delivery (name *or* description, hidden included). Closest neighbors: a dormant agent-run merch store (Veronica) and *buyer-side* concierges (ShopX, Clawmerce). Same on OKX. **The seller-side, describe-to-deploy store-builder category is unoccupied on Virtuals and OKX** — real pressure is Web2 (Shopify Agentic Storefronts, OpenAI/Stripe's separately-named "Agentic Commerce Protocol").
> Defensible positioning line (source-backed): **"Virtuals turns a description into a *token* or a tokenized *trading agent*. Tilla turns a description into a *store*."**

**Q3. How is everything structured over there, and what do we copy?**
Sections 2–4. The short version: **ACP's per-offering machine contract (requirement JSON-Schema + SLA + typed price + reputation metrics) is the gold to copy** — it's what makes any buyer-agent (Butler, Claude, OKX agents) able to self-serve a provider. Ranked, code-mapped adopt-list in **§5**.

---

## 1. Corrections to the earlier research doc (from live sources)

| Claim in `Virtuals-vs-Tilla-Research.md` | Reality (verified) |
|---|---|
| Escrow split "~80% provider / 20% evaluator+protocol", *3‑0 verified* | **90/5/5** (5% protocol + 5% evaluator) *with* an evaluator; **95/5** without. Verbatim on `os.virtuals.io/acp/concepts` + on-chain `platformFeeBP=500/evaluatorFeeBP=500`. **80/20 is unsourced in any live primary/archived doc** — treat as superseded. |
| "Virtuals integrates x402 (shared plumbing)" | True but narrower: `acp-x402.virtuals.io/acp-budget` is an **internal USDC-escrow *funding* rail** (EIP‑3009 into Virtuals' own facilitator on **Base**), **not** an interop bridge letting an ACP buyer pay an external x402 merchant like Tilla. `acp serve` (the "x402 + MPP + ACP endpoints" story) is **documented but not shipped** in the real CLI. |
| Real SDK flow | Confirmed `open → budget_set → funded → submitted → completed` (+ rejected/expired) from `jobSession.ts`. The four-phase Request/Negotiation/Transaction/Evaluation is the whitepaper/v1 abstraction. |
| Revenue Network "roadmap" | **Shipped Feb 12, 2026.** The $1M/month ACP seller subsidy is real (launch PR). *Whether it's still running as of now is UNVERIFIED — no wind-down source found.* |

---

## 2. ACP as a protocol (source-verified)

- **Job machine:** `open → budget_set → funded → submitted → completed`, `rejected`/`expired` terminals — a role×status `TOOL_MATRIX` gates every action (`jobSession.ts`). A **provider is an always-on job worker** (poll `get_active_jobs` / socket events), *not* passive HTTP.
- **Roles:** client / provider / evaluator. Evaluator is buyer-chosen, with **3 modes** (self-evaluate = buyer's own address; third-party; skip = zero-address). Most volume is self/skip.
- **Escrow & settlement:** buyer `fund()`s **USDC** into the ACP core contract on **Base (8453) / Solana (501) / Robinhood (4663)** — **no OKX X Layer (196), no USDT0**. On `complete()` funds release to the provider minus fees; on reject/**SLA-expiry** auto-refund to buyer. **Custody: protocol holds funds during the job** (opposite of Tilla's direct-to-merchant).
- **Fees:** **95/5** (no evaluator) or **90/5/5** (with). Enforced in the Job contract.
- **Onboarding:** **tokenization NOT required** (token is "optional"), registration is **open self-serve** (wallet + signer at `app.virtuals.io/acp/new`, no allowlist/staking/fee), **graduation is automated** (10 successful jobs + 3 consecutive; auto-**un**graduate after 10 consecutive expiries).
- **SDK:** `@virtuals-protocol/acp-node-v2` (0.1.9, pushed 2026‑07‑22; v1 archived). `AssetToken.usdc(amount, chainId)`. Python SDK (`acp-python`) matches Tilla's stack.

## 3. The offering structure (the part worth copying)

An ACP **offering** is a compact machine contract, verified in `src/events/types.ts` and against a **live** offering (aixbt's "indigo": 2 USDC fixed, `slaMinutes:1440`, `requirements:{"type":"object","required":["prompt"],…}`):

```
{ name, description,
  requirements: <JSON Schema of buyer inputs>,   // buyer SDK Ajv-validates BEFORE creating the job
  deliverable:  <prose or schema of output>,
  priceType: 'fixed' | 'percentage' | 'subscription',  priceValue: <USDC>,
  slaMinutes: <number>,        // expiredAt = now + slaMinutes*60, shown to humans as ETA, auto-refund on breach
  requiredFunds: <bool>, isHidden, isPrivate }
```

**Butler** (their consumer concierge) is a thin human wrapper over this exact schema: browse → suggest best-fit agent (+ show alternatives) → **show price + wallet balance** → **prompt the buyer field-by-field from the requirement schema** → **confirmation summary (agent, cost, ETA) + explicit approval** → escrow → deliver → evaluate. **Reputation is public on every registry hit** — `successfulJobCount / successRate / uniqueBuyerCount / minsFromLastOnlineTime / isOnline` (bad rates shown too: Gigabrain 42.75% is public).

---

## 4. How Tilla compares (the honest scorecard)

| Dimension | Virtuals/ACP | Tilla | Read |
|---|---|---|---|
| Create flow | Agent Console → *token/trading agent* | describe → *branded store* | **Tilla's is the only one that outputs commerce** |
| Sell-services ↔ create | **Don't compose** (Console→ACP undocumented) | One flow: store is instantly MCP-/feed-discoverable | **Tilla closes a gap Virtuals has** |
| Settlement | USDC escrow, **custodial**, Base/Solana | x402 USDT0, **non-custodial direct-to-merchant**, X Layer | different, and Tilla's is the cleaner commerce story |
| Human buyer | Butler concierge (their own chat) | store page w/ URL + checkout + our marketplace | different distribution surface |
| Machine contract | **requirement JSON-Schema + SLA + typed price** per offering | feed.json + MCP, **but no input schema, no SLA** | **copy this** |
| Reputation | success_rate / unique_buyers / last_online / graduation | **only sold_count** | **copy this — the quick win** |

---

## 5. What to adopt into Tilla — ranked, code-mapped (the actionable part)

*Effort S/M/L. Files verified against the live tree. Ranks 1–2 are pure additive wins with no migration.*

1. **Reputation-ranked discovery fields — S, highest value/line, no migration.** Confirmed gap: `_discovery_row` (`app/agentic.py:1289`) emits only `sold_count`; ranking is `ORDER BY sold DESC`. Add **`success_rate`** (delivered vs failed/refunded), **`unique_buyer_count`** (distinct payer address — x402 gives it free), **`last_sale_at`**, and a **`sort`** param — all computable in the same subquery from existing `Order` rows. Touch `app/agentic.py` (`_discovery_rows`, `_discovery_row`, `feed_json`, both `/discovery` routes) + tests. **This is the single best next build** — it makes Tilla stores agent-sortable like ACP and directly strengthens the pitch.

2. **Requirement-schema on the machine feed / agent-card — S.** ACP buyers navigate offerings *by* a JSON Schema; Tilla's create-store skill has none — even though **`CreateStoreBody` (`app/main.py:312`) already *is* a schema**. Emit `model_json_schema()` into the agent-card skills, and add a per-product input schema (checkout: variant/qty/email with `required` + `format`) to `feed.json` + per-store MCP. Lets **any** Butler-like / Claude / OKX buyer self-serve our create-store *and* checkout. Fold the **SLA (`sla_minutes`) field** into the same PR. `app/agentic.py` (+ import the pydantic bodies).

3. **Register Tilla's create-store as a Virtuals ACP provider offering — M, user-gated (fits task #15).** Cheap, **custody-safe** distribution experiment into the largest agent economy (Butler funnels consumer demand). Offering = "Create a crypto storefront," priced in **USDC on Base** (the fee is already Tilla's own — no custody violation). Build a small **Node `acp-node-v2` sidecar** that accepts jobs and fulfills by calling `engine.create_store` (bypassing the x402 paywall since ACP escrow collected the fee). User-gated steps: a Base wallet + whitelisted signer + registration at `app.virtuals.io/acp/new`. **Do NOT port Tilla's merchant-sales core to ACP** — that trades away non-custodial / X-Layer / direct-to-merchant differentiators.

4. **Graduation-style trust tier with auto-demotion — S/M.** Tilla has the raw signals (delivered orders; failures via the agent-order reaper `agentic.py:629`) but no tier. Add an automated store trust tier (thresholds lower than Virtuals' 10 given our volume) + auto-demote on repeated expiries. Same file as Rank 1.

5. **Sandbox / hidden-until-proven mode — M (visibility now; testnet rail later).** Virtuals keeps un-graduated agents visible only in sandbox. Tilla puts every generated store straight into production discovery. Add a `hidden`/`sandbox` store state (models + migration; `_discovery_rows` where-clause; `external_feeds` aggregate; sitemap). *Stage 2 (real X-Layer testnet settlement) is blocked on an unverified facilitator capability — probe `/supported` first, same discipline as `AGGR_DEFERRED`.*

6. **Root-level `/mcp` — S/M — the cheap "Butler."** Don't build a concierge; let **any** chat agent be one. MCP exists only per-store (`/s/{slug}/mcp`); add a **root `/mcp`** with slug-parameterized cross-store tools (browse/search/buy) and advertise it in the agent card.

7. **A2A escrow / custom-build job machine with evaluator — L, do last.** Model on ACP's `open→funded→submitted→completed` + `evaluatorAddress`. This is Tilla's designed-not-built "commission a custom store / escrow checkout." New `CommissionJob` model + `app/jobs.py` router, reusing `checkout.transition`/reaper/delivery. Phase-gate the escrow contract (custodial → opt-in, labeled).

**Also — monetization idea (not code): benchmark create-store pricing against the Console shape** — small one-time fee **+ optional monthly hosting auto-deducted + free trial + "live in minutes."** Tilla's metered (MPP) rail is the non-custodial place a recurring/hosting fee could live.

---

## 6. Strategic conclusion

- **Tilla's wedge is real and unoccupied.** No storefront-builder exists on Virtuals (44k agents) or OKX; Virtuals' own "create" products make tokens and trading agents, and their create↔sell surfaces don't compose. Tilla's single describe→store→sellable-to-humans-and-agents flow is genuinely differentiated.
- **The category is unproven industry-wide, not Tilla-specifically.** Even the leader subsidizes agent commerce ~1:1 (~$3M cumulative vs up-to-$1M/month), and its "aGDP" is ~94% swap-router notional. This is the honest framing for the demand risk.
- **Recommended next builds:** Ranks **1 + 2** (reputation-ranked discovery + requirement-schema/SLA on the feed) — both **S, no migration, high pitch + agent-UX value, done in Tilla's own code**. Then Rank **3** as a user-gated distribution experiment. Ranks 4–7 are the roadmap.
- **Re-verify at execution time:** the 90/5/5 fee, the exact graduation numbers, the current subsidy status, and the ACP registration form — Virtuals' docs are mid-restructure (2025-dated changelogs, some `whitepaper.virtuals.io` paths 404). Nothing in Ranks 1–2 depends on those.

---

## 7. Confidence & open threads

**High confidence (primary, current):** the no-storefront-competitor finding (live 44k-agent registry), the offering schema (live API + SDK source), the escrow chains/asset (SDK constants + on-chain), 90/5/5 fees, open self-serve registration, no-tokenization-required, the reputation metric set, Tilla's own code gaps.
**Unverified / needs a source before public use:** whether the $1M/month subsidy is *still* active; the exact current graduation criteria under v2; the "ClawBoost gaming" anecdote; whether an Agent Console agent auto-registers on ACP; the historical 80/20 (archive.org was rate-limited). **Treat competitor facts as perishable — re-check before the submission.**
