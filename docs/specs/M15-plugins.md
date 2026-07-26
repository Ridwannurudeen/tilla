# M15 — Plugin / extension ecosystem (VISION §1 → buildable spec)

**Status: BUILT AND SHIPPED** *(updated 2026-07-26 — this header said "SPEC"; the module has since been built, tested and deployed. See `docs/VISION.md` for the built state. The migration numbers quoted below are frozen at authoring time and are NOT current — the deployed head is `0030_creation_block_floor`.)* Originally derived from `docs/VISION.md` §1. Format and discipline follow
`docs/BUILD.md` M0–M14: smallest real slice first, binary acceptance, named tests,
honest parking. **Nothing here changes live behavior until its increment ships.**

**Scope.** Turn Tilla's three hard-coded extension points — delivery
(`app/delivery.py`), payment accepts (`app/payment.py` + `app/agentic.py`), themes
(`app/render.py` + `themes/`) — into formal provider interfaces with the built-ins
as first-class providers, plus a registry, a manifest/review pipeline, and the
sandbox boundary specced BEFORE any third-party code can load. Actual third-party
activation is EXTERNALLY-BLOCKED (see Parking).

## Threat model (FIRST — this is third-party code near the money/delivery path)

| Threat | Defense | Where tested |
|---|---|---|
| Plugin code executes in-process with DB/env/key access | **Sandbox boundary rule (INV-1):** third-party plugin code NEVER imports into the app process. Only *declarative* plugins (data/config, e.g. themes as templates) run in-process; *code* plugins run out-of-process behind a JSON contract (increment 4) or not at all. | 15.4 |
| Malicious theme exfiltrates via markup/JS | Autoescape is enforced by the LOADER (`render.render_store` / Jinja2 env with `autoescape=True`), never by the theme; new-theme install re-runs the M1 XSS corpus against the candidate template; no `{% raw %}`/`|safe` allowed (lint gate). | 15.2 `test_plugin_theme_xss_corpus` |
| Plugin redirects funds (hostile `pay_to`/price) | `PaymentRailProvider` is registry-listed but **built-ins only** until a facilitator-verifiable scheme exists; any provider's `pre_settle_gate` returning ≥400 forces the x402 middleware to skip settle (the live `agent_buy` invariant, `app/agentic.py::agent_guard_dispatch`). A provider can only NARROW (veto) a settlement, never originate one. | 15.3 `test_provider_gate_blocks_settle` |
| Unreviewed plugin activation | Every manifest passes Warden screening (`screening.screen`, fail-closed) + explicit operator approval flag before `status='active'`; default `pending_review`. | 15.1 `test_plugin_pending_until_approved` |
| Supply-chain: tampered plugin artifact | Manifest pins `sha256` of the artifact; loader refuses on mismatch. | 15.2 `test_plugin_hash_mismatch_refused` |
| Registry as IDOR surface | Registry write endpoints are operator-only (no merchant/public write); reads leak no secrets. | 15.1 |

**INV-1 (sandbox boundary, non-negotiable):** until an out-of-process runner exists
(increment 4), the ONLY installable third-party plugin kind is `theme`
(declarative Jinja2 under loader-owned autoescape). `delivery` and `payment_rail`
providers stay built-in-only. This is the whole reason the increments are ordered
as they are.

## Increments

### 15.1 — Provider registry + built-ins formalized (S/M)
Refactor-only + one table. Define three Protocol classes in a new `app/providers.py`:

- `DeliveryProvider` — `kind: str`, `mint(session, order) -> Entitlement payload`,
  `revoke(session, order) -> bool`. Built-ins `file` / `text` / `license` are thin
  wrappers over the EXISTING functions: `delivery.mint_download_token` +
  `delivery.claim_download`, text-secret issue, `delivery.mint_license_key` +
  `activate_license`/`deactivate_license`; `revoke` wraps `delivery.revoke_entitlement`.
- `PaymentRailProvider` — `scheme: str`, `build_accepts(rail) -> list[PaymentOption]`,
  `pre_settle_gate(...) -> int|None` (≥400 vetoes settle), `record_settlement(...)`.
  Built-ins wrap `payment.build_store_payment_options` (exact + flag-gated
  `aggr_deferred`) and `agentic.record_settlement`; the MPP and period rails are
  registry ENTRIES (metadata) only — they are endpoint families, not accepts
  schemes, and stay exactly as M8 shipped them.
- `ThemeProvider` — `name`, `template`, 15-token contract check against
  `config.ALLOWED_THEMES` machinery.

New table `plugins` (migration `0011_plugins`, additive; renumber to next free head
at build time — 0010 is pending on another branch): `id, kind, name, version,
source ('builtin'|'external'), artifact_sha256, manifest JSON, status
('pending_review'|'active'|'disabled'), created_at`. Seed rows for every built-in
with `source='builtin', status='active'`.

**Behavior change: NONE.** Checkout/delivery/402 bytes identical (goldens assert it).

**Accept (binary):** claimed ONLY if (a) migration up/down/up passes on a
prod-shape DB copy, (b) full existing suite green unchanged, (c) new
`tests/test_providers.py`: `test_builtin_providers_registered`,
`test_402_challenge_byte_identical`, `test_delivery_via_provider_matches_direct`,
`test_plugin_pending_until_approved` (an external-source row never resolves while
`pending_review`).

### 15.2 — Theme plugins (the ONE safe third-party kind) (M)
Operator-only install path: `scripts/install_theme.py` (offline CLI, not an HTTP
upload endpoint — no new public attack surface) takes a manifest
(`name, version, sha256, template file`), verifies hash, lints the template
(reject `|safe`, `{% raw %}`, `{% include %}` outside themes/, inline
event-handler attributes), runs the M1 XSS corpus through
`render` with hostile store content, screens the manifest text via
`screening.screen` fail-closed, writes the template into `themes/` and a
`plugins` row `pending_review`. A second explicit `--approve` invocation flips
`active`, which adds the name to the allowed-theme set (make `ALLOWED_THEMES`
a function reading builtin frozenset ∪ active theme plugins).

**Accept:** claimed ONLY if a real non-builtin theme artifact exists in-repo as a
fixture and: `test_plugin_theme_xss_corpus` (corpus inert through the plugin
theme), `test_plugin_hash_mismatch_refused`, `test_theme_lint_rejects_safe_filter`,
`test_unapproved_theme_not_selectable` (create-store with it → 422), plus one live
store rendered on an installed plugin theme (URL logged).

### 15.3 — Provider gate conformance harness (S)
A reusable conformance suite any `PaymentRailProvider`/`DeliveryProvider` must
pass: gate-veto skips settle (respx-mocked facilitator; asserts zero settle call
after a ≥400 gate — the `agent_buy` dead-store pattern), idempotent
`record_settlement` (same PAYMENT-RESPONSE twice → one delivery), `revoke` is
idempotent. Run it parametrized over all registered providers in CI.

**Accept:** `tests/test_provider_conformance.py` green over every registry entry;
`test_provider_gate_blocks_settle` proves no settle HTTP call fires post-veto.

### 15.4 — Out-of-process plugin runner — SPEC + STUB BOUNDARY ONLY (L, mostly PARKED)
Design committed here so nobody "temporarily" imports third-party code in-process:
a code plugin runs as a separate systemd unit exposing localhost JSON over a unix
socket; the app talks a versioned contract (`mint`/`revoke`/`gate` with 2s
timeout, fail-closed to the built-in refusal path); no DB handle, no env
inheritance (`PrivateTmp`, `ProtectHome`, dedicated user). Buildable now: the
client shim + fail-closed timeout tests against a fixture echo-server.
NOT buildable honestly: an actual external code plugin.

**Accept (shim only):** `test_runner_timeout_fails_closed` (dead socket → delivery
refuses, order stays undelivered, no 500-with-side-effects), contract goldens.

## Parking (honest, exact missing dependency)

- **EXTERNALLY-BLOCKED — third-party code plugins (delivery/payment kinds):**
  missing dependency = **at least one real external plugin author** with a real
  artifact, per VISION §1's own precondition. Building past 15.4's boundary with
  zero demand is scaffolding theater. Registry + theme kind + conformance harness
  build up TO this boundary.
- **PARKED — `importlib.metadata` entry-point discovery:** entry points mean
  in-process import, which violates INV-1; revisit only alongside the runner.
- **USER-gated:** approving any external manifest (`--approve` is an operator act).

## Build order + size
1. 15.1 registry + built-ins (S/M) → 2. 15.3 conformance harness (S) →
3. 15.2 theme plugins (M) → 4. 15.4 runner shim (L, boundary only).
Total buildable-now: ~2–3 focused days.
