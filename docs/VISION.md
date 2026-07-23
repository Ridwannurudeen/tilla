# Tilla — Vision (Phase 3–4 forward design)

> **⚠️ HISTORICAL DOC — SUPERSEDED 2026-07-23.** This was written as forward design,
> but the vision tier was subsequently BUILT + tested as modules **M15–M18**, and the
> TypeScript SDK (§5) shipped. The per-section "DESIGNED, NOT BUILT" notes below are
> now HISTORICAL. Actual built state (134 vision-tier tests green):
> - **§1 Plugin/provider ecosystem → BUILT (M15):** `app/plugin_runner.py`, `app/providers.py` (tests: `test_providers`, `test_provider_conformance`, `test_theme_plugins`).
> - **§2 Cross-chain checkout → BUILT (M18):** cross-chain surfacing in `app/checkout.py`/`config.py`/`render.py` (test: `test_crosschain`).
> - **§3 B2B / marketplace-of-marketplaces → BUILT (M16):** `app/b2b.py` (ERC-8004 owner-gated wholesale tiers), `app/federation.py` (mirror-of-mirrors ingest) (tests: `test_b2b`, `test_federation`).
> - **§4 Autonomous growth agent → BUILT (M17), LIVE on prod:** `app/growth.py` + `app/growth_scheduler.py` — outbox draft→approve→discard→mark-published queue, first-party performance aggregates, performance-aware content calendar + dormant scheduler, multi-channel drafts (5 growth test files). Endpoints live under `/api/stores/{slug}/growth/*`.
> - **§5 TypeScript SDK → BUILT:** `sdk/typescript/src/` (client/signing/x402Codec/models).
> - Also: ERC-8004 **verified-buyer reviews** live (`/api/library/review`, purchase-gated).
>
> The original forward-design text is retained below for context.

This document is the honest, forward-looking design for the multi-quarter Phase 3–4
"commerce OS" vision. It is deliberately **not** an implementation plan for a single
session. Two slices of this vision **are** built and are cross-referenced as such:

- the **open Python SDK** (`sdk/python/`, `tilla-sdk`) — SHIPPED, see §5 and `BUILD.md`;
- the **growth-kit endpoint** (`app/growth.py`) — SHIPPED as the seed of the full
  growth agent, see §4 and `BUILD.md`.

The rest is designed, not built. Where a design leans on something the code already
does today, it names the exact seam so a future build starts from reality, not a
blank page.

---

## 1. Plugin / extension ecosystem

> **STATUS: DESIGNED, NOT BUILT.** Nothing in this section exists in code.

Today Tilla has three hard-coded extension points. This section sketches turning
each into a provider interface, derived from the seam that already exists.

**`DeliveryProvider`** (derived from `app/delivery.py`).
```
kind: str                         # provider id, e.g. "file" | "text" | "license"
mint(order) -> payload | claim-keys   # produce the deliverable for a paid order
revoke(order) -> None                 # kill a minted download token / license key
```
Today's file / text / license delivery become the three built-in providers. The
existing entitlement + signed-token + license-activation machinery is the reference
implementation each provider must satisfy.

**`PaymentRailProvider`** (derived from `app/payment.py` + `app/agentic.py`).
```
scheme: str                       # rail id, e.g. "exact" | "aggr_deferred" | "metered" | "period"
build_accepts(product) -> [PaymentOption]      # what the 402 advertises
pre_settle_gate(request) -> None | HTTPError   # return >=400 to KEEP the funds-safe skip-settle path
record_settlement(tx) -> None                  # persist the settle receipt
```
`exact`, `aggr_deferred`, MPP-metered, and subscription/`period` become built-ins.
The critical invariant carried forward from the live code: `pre_settle_gate` must be
able to force a `>=400` **before** settlement so the x402 middleware skips it — this
is exactly how `agent_buy` refuses a dead store or a mis-scheme today without moving
funds.

**`ThemeProvider`** (derived from `app/render.py` + `themes/`).
```
tokens: 15-token contract         # the existing store-content token set
render(content, ...) -> html      # autoescape MANDATED by the loader, not the theme
```
The autoescape guarantee stays in the loader (the M1 XSS lesson): a third-party theme
can never opt out of escaping.

**Registration** via `importlib.metadata` entry points. **Every third-party plugin
passes Warden screening + human review before activation.**

**Why not built:** third-party code inside the money/delivery path needs sandboxing,
signing, a review pipeline, and at least one real external plugin author. Building the
registry with zero third-party demand would be scaffolding theater. **Precondition:** a
real external plugin author + a review/sandboxing pipeline.

---

## 2. Cross-chain checkout

> **STATUS: DESIGNED, NOT BUILT. EXTERNAL DEPENDENCY: bridges / facilitator support.**
> Nothing in this section exists in code.

Options, ranked:

**(a) Additional x402 `accepts` on other networks — IF/WHEN the OKX facilitator
supports them.** Verify `/supported` first; never advertise an unsettleable rail
(the M8 lesson — the `aggr_deferred` scheme is only registered after the read-only
`/supported` probe confirms it on `eip155:196`). This is the cleanest path and needs
no bridge code.

**(b) A front-end "bridge in" affordance** linking a third-party bridge, **USER-gated**,
clearly labeled third-party risk, **zero bridge code in Tilla.**

**(c) X Layer USDT0 stays the canonical settlement ledger** regardless of how funds
arrive.

**Hard rule carried forward:** no custody, no in-code fund movement, every on-chain step
user-gated. **Precondition:** facilitator support for a second network, or a vetted
third-party bridge the operator is willing to link.

---

## 3. B2B / marketplace-of-marketplaces

> **STATUS: DESIGNED, NOT BUILT.** Nothing in this section exists in code.

**Wholesale price tiers** keyed on buyer identity (an ERC-8004 agent id presented at
buy time). Agents-buying-from-agents needs **no new rail** — only tiered pricing
params on `Product.pricing_params` (the same JSON column that already carries
`batch` / `metered` params today) and the existing per-store MCP + x402 surfaces.

**Store-to-store procurement** rides the existing per-store MCP (`/s/{slug}/mcp`) +
x402 buy surface. A purchasing agent is just another x402 buyer.

**Federation** = other Tilla-protocol instances publishing the same `feed.json` /
`agent-card.json` / 402 conventions, aggregated by a `/discovery` mirror-of-mirrors.

**The "open protocol" deliverable is documentation**, not new runtime: publish
`docs/openapi.feed.yaml` + the agent-card + the 402 conventions as a **versioned
spec** others can implement. (`docs/openapi.feed.yaml` and
`docs/openapi.external-feeds.yaml` already exist as the seed of this spec.)

**Precondition:** a second independent operator running the protocol.

---

## 4. Autonomous full growth agent

> **STATUS: DESIGNED, NOT BUILT — except its seed, which IS built (cross-ref below).**

**BUILT seed (see `BUILD.md`):** `POST`/`GET /api/stores/{slug}/growth-kit`
(`app/growth.py`) generates a small, re-screened marketing kit from a store's
already-screened content and persists it to `event_log`. It is merchant-gated,
LLM-spend-capped (6/hour), fail-closed, and **triggers no external posting**.

**Full design (not built):** a scheduled content calendar per store; performance
feedback from affiliate/order data; multi-channel drafts — all flowing into a
**draft → approve → publish** queue where **PUBLISH IS PERMANENTLY A USER ACTION**.
There is no external-posting authority in code, ever (the M13 rule: listings/sends are
user-owned). The growth-kit endpoint already embodies this — it returns copy for a
human to publish and never sends anything itself.

**Precondition:** SMTP / social credentials (user-gated) and demonstrated merchant
demand for automation beyond one-shot kit generation.

---

## 5. Open SDK

> **Python: SHIPPED** (cross-ref `sdk/python/`, `BUILD.md`). **TypeScript: DESIGNED,
> NOT BUILT.**

**Python — BUILT.** `tilla-sdk` (`sdk/python/`) is a sync, typed, dependency-light
client wrapping only endpoints live today: discovery/search, feeds, agent card, human
checkout + poll, the MCP tool surface, and the two x402 pay paths (`create_store`,
`buy`). It mirrors the production `warden_hire` payer invariants — refuse-before-sign
on pin/cap mismatch, sign-once, never re-fire a signed authorization — and never
bundles, reads, defaults, or logs a key (the caller supplies a signer hook). It is
**never** part of the app deploy (the app package stays `["app"]`; `deploy.sh` never
ships `sdk/`).

**TypeScript — DESIGNED, NOT BUILT.** Same client surface (discovery / feed / checkout
/ MCP / x402 buy + create-store). The signer hook maps to an **EIP-1193 provider** or a
raw signer callback, so a browser/Node caller supplies wallet control exactly as the
Python `PaymentSigner` does. The x402 codec is the same `base64(JSON)` wire format.
**Precondition:** a JS/TS consumer that needs it (no point porting ahead of demand).
