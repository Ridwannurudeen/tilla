# Tilla — Build Plan (v2, committed scope)

**This version supersedes v1.** v1's mistake (called out, agreed): it committed only a thin MVP and parked all depth in "post-hackathon" — the deferral *was* the shortcut. v2 pulls the depth into committed scope. Companion: `ROADMAP.md` (the full feature map + verified constraints). Everything here is buildable with rails/tools **verified 2026-07-20/21**; features on unverified rails sit behind explicit spikes (§7).

**Current live state (do not break):** https://tilla.gudman.xyz (nginx + `tilla-api.service`, uvicorn 127.0.0.1:8040), example store `/s/invoice-flow/`, ASP #6961 x402-gated `/create-store` (1 USDT). `/opt/tilla` = 4 files, 560 lines, **no git, no tests, in-memory checkouts, unescaped LLM HTML (live XSS), fake delivery string**.

---

## 0. Ground rules (Warden-level rigor, non-negotiable)

1. **Git from commit one.** Local repo `GITHUB-FILES/tilla` = source of truth; VPS is a deploy target, never edited directly. Small, single-purpose commits; no attribution tags.
2. **Tests with every module.** Bug fixes start with a failing test. Run the suite before every deploy; report results, never "should work".
3. **Deploy = ship changed files individually** (scp per file + `systemctl restart tilla-api` + smoke test). Never full-directory clobber (`stores/`, `tilla.db`, `.env` are server-owned).
4. **Evidence integrity:** self-trades labeled self-trades; testnet labeled testnet; no inflated claims anywhere.
5. **Secrets never in the repo.** `.env` stays on the VPS (chmod 600). Test fixtures use fake creds.
6. **Every increment leaves the live site working.** Migration path for the 2 existing stores (invoice-flow, billable) is part of M2 acceptance.

## 1. Repo layout

```
tilla/                      # git repo root (GITHUB-FILES/tilla)
  pyproject.toml            # deps pinned; ruff config
  app/
    __init__.py
    main.py                 # FastAPI assembly, middleware wiring
    config.py               # env-driven settings (paths, RPC, flags)
    db.py                   # SQLAlchemy engine/session (sqlite WAL)
    models.py               # Merchant, Store, Product, Order, Delivery, EventLog
    engine.py               # LLM store generation (from make_store.py)
    render.py               # Jinja2 autoescaped theme rendering
    checkout.py             # order state machine + verification
    chain.py                # X Layer RPC: balanceOf, receipts, getLogs, WS
    delivery.py             # files, signed links, buyer library, license keys
    screening.py            # Warden content screening client
    payment.py              # x402 rail (existing, extended per-store)
    agentic.py              # feeds, MCP server, agent card, discovery
    dashboard.py            # merchant auth + dashboard routes
  themes/                   # original + bold + editorial (Jinja2)
  alembic/                  # migrations
  tests/                    # pytest suite
  scripts/deploy.sh         # file-by-file scp + restart + smoke
  docs/                     # ROADMAP.md, BUILD.md, DEMO-SCRIPT.md, SUBMISSION.md
```

## 2. Pinned toolchain (all versions verified on PyPI/npm 2026-07-20)

| Purpose | Tool | Version |
|---|---|---|
| ORM / migrations | SQLAlchemy / Alembic | 2.0.51 / 1.18.5 (`render_as_batch=True`) |
| Signed download tokens | itsdangerous | 2.2.0 (`URLSafeTimedSerializer`, `max_age`) |
| Rate limiting | slowapi (pin exact, in-memory) + nginx `limit_req` | 0.1.10 |
| Uploads | python-multipart | 0.0.32 (+ nginx `client_max_body_size` + chunked-read cap) |
| Templating | Jinja2, `autoescape=True` | 3.1.6 |
| x402 | okxweb3-app-x402 (exact + aggr_deferred server-side) | 0.1.0 |
| MPP (PAYG) | pympp / okxweb3-app-mpp | 0.9.1 / 0.1.0 — **candidate, spike decides** |
| Tests | pytest / pytest-asyncio / httpx / respx / coverage | 9.1.1 / 1.4.0 / 0.28.1 / 0.23.1 / 7.15.2 |
| Lint | ruff | 0.15.22 |
| CI | GitHub Actions (checkout@v7, setup-python@v7, py3.12) | — |
| Node sidecar (subscriptions) | @okxweb3/app-x402-core 0.2.1 + x402-express 0.1.1, Node 22 LTS | **candidate, spike decides** |
| LLM | claude-haiku-4-5 (pin `claude-haiku-4-5-20251001`) | anthropic 0.117.0 |
| Wallet connect | hand-rolled EIP-1193/EIP-6963 (`window.okxwallet` → `window.ethereum`), `wallet_switchEthereumChain` 0xc4 | no build step |
| Explorer links | `https://www.oklink.com/x-layer/tx/<hash>` | — |

Chain constants: RPC `https://rpc.xlayer.tech`, WS `wss://ws.xlayer.tech`, testnet 1952 `https://testrpc.xlayer.tech/terigon` + faucet, USDT0 `0x779ded0c9e1022225f8e0630b35a9b54be713736` (6dp), `eth_getLogs` ≤101 blocks + client timeouts.

---

## 3. Modules (each ships with tests; sequenced §5)

### M0 — Repo bootstrap
Init git repo; restructure the 4 VPS files into `app/` layout (no behavior change); pyproject with pinned deps; ruff + pytest + CI green; `scripts/deploy.sh` (per-file sync → **`alembic upgrade head` on the VPS** → restart → smoke — migrations are part of every deploy from M2 on); deploy and verify live parity.
**Accept:** CI green; live site + 402 on `/create-store` unchanged; first ~10 tests (slugify, payment-rail validation, health).

### M1 — Security foundation (kills the live XSS first)
Jinja2 autoescaped rendering replaces `str.replace` (LLM output can never become markup; `| tojson` in script blocks; `https?://` allowlist on any URL fields). Pydantic validation on every endpoint (desc length, address regex, price bounds 0.01–10,000, slug charset + reserved list, collision → suffix). slowapi + nginx `limit_req` on `/create-store` + checkout endpoints; body-size caps (nginx + chunked read) with a test proving the cap. Warden screening on merchant description + generated content pre-deploy: verdict BLOCK → refuse (fail-closed), logged; **screening unavailable (timeout/5xx) → store held in `pending_screening` with retry, never silently deployed and never a hard-brick of creation**. Canonical endpoint: `POST https://warden.gudman.xyz/api/demo/scan` until M10 switches to the paid x402 hire of Warden #3808 (same verdict contract).
**Accept:** XSS corpus test (script/img-onerror/`javascript:` payloads through the full generate→render path) renders inert; screening-BLOCK test **and screening-timeout→pending test**; rate-limit 429 test; oversized-body test.

### M2 — Persistence core
SQLAlchemy models: Merchant (wallet addr, api-key hash), Store, Product (multi-product per store), Order (state machine), Delivery/Entitlement, EventLog (append-only audit). sqlite WAL, `check_same_thread=False`, sync engine + `def` endpoints. Alembic migrations. Import script for the 2 existing stores. Nightly backup (sqlite `.backup` + rotate 7) via cron.
**Accept:** migration up/down test; restart-survives test (order state persists across process restart); existing stores importable + still render; backup file produced.

### M3 — Hardened checkout
Order state machine: `pending → detected → confirmed → delivered` + `expired / late_paid / underpaid / overpaid / refunded`. **Unique per-order amount** (price + unique 6dp offset, reserved while pending, **quarantined for a cooldown after expiry before reuse** — a late tx must never confirm a reassigned amount). Late payment: `expired → late_paid` when the matching tx lands after expiry → honored (deliver) since funds reached the merchant. Underpaid: buyer can top-up — cumulative matched txs ≥ amount within the window → `confirmed`; unresolved → refund path (M9). Overpaid: `confirmed` + overage recorded on the order (merchant optionally refunds overage via M9). Verification paths: (a) buyer-submitted txhash → `eth_getTransactionReceipt` decode (Transfer to merchant, exact amount, status 1, USDT0 contract); (b) polling (committed) with WS `logs` subscription + ≤101-block `eth_getLogs` backfill behind spike §7. Idempotent transitions (a tx can pay exactly one order; replayed webhook/poll can't double-deliver). All RPC calls time-boxed.
**Accept:** concurrent-buyers test (two pending orders, same store — right one confirms); underpay→top-up→confirmed test; overpay test; expiry test; **late-payment test (expired amount quarantined; late tx honors the original order, never a reassigned one)**; idempotency test (same tx twice); txhash-verify test against respx-mocked RPC; one **real testnet-1952 e2e** + one small mainnet smoke.

### M4 — Real gated delivery
Per-store file storage (uploads via python-multipart, size/type caps, hash-named); text secrets (license text/URL) as alternative deliverable. Signed expiring download links (itsdangerous, default 24h, configurable download limit). **Buyer library**: wallet-signature sign-in — server-issued nonce + timestamp inside the signed message, verified via eth-account, then a short-lived session token (no raw-signature reuse) — lists past purchases, re-issues links. **Exchange-wallet buyers** (can't sign): optional email capture at checkout → re-delivery magic link; fallback = order-id + txhash support path. License keys: issue on sale; `POST /api/licenses/{activate,validate,deactivate}` with activation limits.
**Accept:** token-expiry + download-limit tests; tampered-token test; signature-auth tests (wrong wallet ≠ access; **replayed signature ≠ access; stale nonce ≠ access**); email re-delivery test; license lifecycle tests; e2e: pay → download real file.

### M5 — Checkout UX
Wallet-connect buyer page: EIP-6963 discovery → `window.okxwallet`/`window.ethereum`; auto chain-switch to 0xc4; ERC-20 transfer calldata (`0xa9059cbb…`) with exact unique amount; QR + address fallback; countdown; live status; receipt page with OKLink link. Mobile deep link after spike (§7).
**Accept:** documented manual smoke script covering: EIP-6963 discovery, missing-wallet fallback (QR/address), chain-switch to 0xc4 incl. user-reject path, exact-amount prefill, countdown expiry behavior, paid→delivered transition, receipt tx link — executed and results logged per deploy; one real browser purchase on mainnet (small amount, arm's-length wallet).

### M6 — Themes
Convert original + bold + editorial to autoescaped Jinja2 themes (same 15-token contract, verified); theme selection in create-store (merchant choice or LLM pick); per-store OG image, JSON-LD, sitemap.
**Accept:** 3 themes render one store model; escaping tests run against **all** themes; live store on each theme.

### M7 — Dual-sided commerce (agent-buyable stores)
Per-store x402 buy endpoint `POST /s/<slug>/buy` — 402 with per-store PaymentOption (**payTo = merchant wallet, non-custodial**); paid call returns the deliverable token directly. Mechanism is **spike-gated (§7.8)**: dynamic per-request accepts in `okxweb3-app-x402` 0.1.0, fallback = hand-built 402 + direct facilitator verify/settle calls. Machine-readable catalog: `/s/<slug>/feed.json` (ACP product-feed shape), JSON-LD in pages, `/s/<slug>/llms.txt`. **Per-store MCP server** (`/s/<slug>/mcp`: `list_products`, `get_product`, `create_checkout`, `pay`). A2A agent card `/.well-known/agent-card.json`; Tilla-wide discovery API (`GET /discovery/resources` + `/search`).
**Accept:** x402-check valid on a per-store endpoint; **arm's-length agent purchase e2e** (second wallet, scripted buyer) delivering the real file; **MCP client script completes list → get → checkout → pay against a live store**; feed validates against our committed JSON Schema derived from ACP spec 2026-04-17 (`openapi.feed.yaml`, pinned in repo); discovery endpoints tested.

### M8 — Payment methods expansion (built; per-rail settlement USER-gated)
Per-SKU payment-method declaration: `products.pricing_model` ∈ {one_time, batch, metered, subscription} + `pricing_params` (0005, additive), set via manage-key `POST /api/stores/<slug>/pricing`; surfaced additively in feed/MCP/llms (`pricing` block + enabled-schemes list). Rails:
- (a) `aggr_deferred` — second accepts-entry on batch SKUs, flag-gated `TILLA_AGGR_DEFERRED` (default OFF). Guard middleware strips it from a non-batch store's 402; pay-time handler 409s a non-batch aggr payment before settle. Server class is a thin `ExactEvmScheme` wrapper (installed); `pending` settle = success-settling.
- (b) MPP pay-as-you-go — `app/mpp.py`, `/s/<slug>/mpp/{open,voucher,topup,close}` for metered SKUs; sqlite channel state (`mpp_channels`), race-proof conditional-UPDATE transitions, local monotonic-voucher accounting; SA client (`okxweb3-app-mpp` 0.1.0, module `mpp_evm`) imported LAZILY behind the flag. Default OFF → every endpoint 503s.
- (c) Subscriptions — in-repo Node sidecar (`sidecar/`, x402 `period`) + `deploy/tilla-sidecar.service` + fail-closed FastAPI proxy `POST /s/<slug>/subscribe`. Default OFF → 503.

**Accept per rail (binary):** a rail is CLAIMED working ONLY if a real settlement tx hash is logged; otherwise it is honestly PARKED (scope cut), ROADMAP Phase 4 marked PARTIAL, and no claim about it appears anywhere. Never fake/simulate a settlement.

**Per-rail status (as shipped):**
1. **x402 exact — LIVE, PROVEN** (real settlement tx hashes already logged; unchanged).
2. **aggr_deferred — OPTION LIVE, SETTLEMENT UNPROVEN.** The second accepts-entry is offered on batch SKUs and the challenge is x402-check-valid (that much is stated, with the check output as evidence). NOT claimed working: a real settle requires an OKX **TEE agentic-wallet buyer** (plain EOAs are rejected at verify) — USER-gated; parked until an aggregated settlement tx hash is logged (the deferred settle may return success with no tx hash at serve time, so evidence polls `session/settle` status). A failed EOA attempt is recorded as **buyer-class-gated**, never as a rail result.
3. **MPP pay-as-you-go — CODE BUILT, DEPLOYED DORMANT (fail-closed 503), NO CHANNEL EVER OPENED.** A real proof needs a funded channel (real USDT into escrow `0x5E55…CE3b`) + SA creds — USER-gated. No claim of any kind; scope formally cut until then.
4. **subscription (period) — SIDECAR + PROXY BUILT; LOCAL DRY-RUN VERIFIED** (`sample-buyer` exit 0, challenge/verify only); **DEPLOYED DORMANT 503.** A real subscribe/charge needs OKX creds + a funded Permit2 buyer — USER-gated. No claim.

Evidence-integrity rule (restated): a settlement is only ever the facilitator's/chain's tx hash — nothing simulated, mocked, or self-marked may be presented as a settle.

### M9 — Merchant platform
Merchant accounts (wallet-signature auth + API key, same nonce/session scheme as M4); multi-store; dashboard (orders, revenue, per-product, order detail with tx links); **refunds**: merchant-initiated — non-custodial, so the merchant sends the on-chain refund from their own wallet and Tilla records + verifies the refund txhash → order transitions to `refunded` (also resolves M3's stuck-underpaid and overage cases); CSV export; merchant REST API + HMAC-signed webhooks (order.paid, order.delivered, order.refunded) with retry + replay-protection tests.
**Accept:** full merchant lifecycle test (create → 2 stores → sale → dashboard shows it → CSV → webhook received with valid signature); refund state-transition test (underpaid→refunded and confirmed→refunded with verified txhash).

### M10 — Marketplace citizenship (on-chain listings are approval-gated: propose, get OK, then execute)
**Committed (x402/HTTP only — no XMTP dependency):** additional services under #6961 (`upgrade-store`, `add-product`); store-as-ASP Option A (delta-service per store under one ASP — proven pattern from Warden 35484); agents-hiring-agents: Tilla pays Warden #3808 via x402 for each screening (plain HTTP 402 flow; real receipt, shown in dashboard). Option B (per-store ASP identity) as measured experiment (1 store, review-throughput logged).
**Gated on the XMTP dispatch runtime** (Node ≥22.14, `switch-runtime` provisioning — shared infra with Warden, provision once): task-board participation both directions, XMTP negotiation, post-task ratings. If the runtime isn't provisioned, these stay out of scope and out of claims.
**Accept:** each listing `validate-listing`-clean before submit **and approved by marketplace review (review latency logged)**; screening-hire receipt visible; scan results archived.

### M11 — On-chain depth
EAS attestation per sale (predeploy `0x4200…0021`, web3.py ABI call; schema: buyer, storeId, amount, payment txHash) after testnet spike; StoreRegistry contract (Foundry, OKLink contract-verify API) if time permits — DIFF, not gating.
**Accept:** attestation visible on explorer for a real sale; attestation UID on the receipt page.

### M12 — Ops (runs alongside, finishes last)
Health/readiness endpoints; systemd watchdog timer + **Telegram alerting on service-down** (Warden ops pattern — same bot channel); sqlite backup verification + **nightly off-VPS copy** (pull/push to a second location — VPS loss must not lose the database); log rotation; deploy runbook + rollback (previous release dir + symlink); Anthropic API noted as generation SPOF — clear 503 + retry on outage, daily spend logged.
**Accept:** kill-test (service auto-recovers + alert fires); restore-from-backup drill documented + executed once **from the off-VPS copy**.

---

## 4. Threat model (tested, not asserted)

| Threat | Defense | Test |
|---|---|---|
| LLM/prompt-injection → stored XSS | autoescape everywhere, URL allowlist, no LLM HTML as template (SSTI ban) | M1 corpus |
| Malicious store content (scams/piracy) | Warden screening fail-closed + takedown path + ToS | M1 |
| Payment replay / double-delivery | one-tx-one-order constraint, idempotent transitions | M3 |
| Concurrent-buyer confusion | unique per-order amounts | M3 |
| Forged download access | signed+expiring tokens, wallet-signature library auth | M4 |
| Webhook spoofing | HMAC signatures + timestamp window | M9 |
| DoS on free endpoints | slowapi + nginx limit_req + body caps | M1 |
| Path traversal / slug abuse | strict slug charset, reserved names, no user paths | M1 |
| Secrets leakage | .env only on VPS, no secrets in repo/tests, no echo | M0 review |
| VPS blast radius | never touch other services; per-file deploys | ground rules |

## 5. Sequencing

**M0 → M1 → M2 → M3 → M4** (the hardened core — strict order, each deployed live before the next) **→ M5 + M6** (parallel) **→ M7** (the differentiator) **→ M8 + M9** (parallel) **→ M10 → M11**, M12 alongside from M2. Spikes (§7) run early in idle slots since they gate M3/M5/M7/M8/M11 decisions.

**Rough effort (honest pricing, not a promise):** M0 ~0.5–1d · M1 ~1d · M2 ~1d · M3 ~1.5–2d · M4 ~1–1.5d · M5 ~1d · M6 ~0.5–1d · M7 ~1.5–2d — the M0–M7 chain prices at **~8–10 focused days**, which is *more* than the ~7 remaining before the checkpoint. Consequence, stated up front: the checkpoint bar is **M0–M5 + M7-core** (hardened checkout, real delivery, one agent-buyable store with feed) with M6/M7-rest following immediately after; anything unfinished is simply not claimed.

**Hackathon checkpoint Jul 27 22:59 UTC:** submit whatever is genuinely proven by then; the build continues past it (per the depth-first direction). Submission itself (X post, form, demo) = **user-owned, approval-gated**.

## 6. Definition of done (per module and overall)

Module: code + tests green in CI + deployed live + smoke-tested + committed + one-line CHANGELOG entry. Overall: ROADMAP Phases 1–3 fully live; every claim in SUBMISSION.md backed by a receipt, tx hash, or test run; zero known Sev-1 (security/fund-loss) issues; memory files updated.

## 7. Spikes (run before their dependent modules; log results in docs/spikes.md)

1. `eth_subscribe` WS end-to-end (gates M3 WS path; polling fallback regardless).
2. MPP session loop with our SA creds (gates M8b).
3. Node sidecar `period` support probe (gates M8c).
4. EAS attest on testnet 1952 (gates M11).
5. OKX mobile deep-link (gates M5 mobile).
6. Multi-ASP `pre-check`/review throughput (gates M10 Option B).
7. USDT0-vs-"USDT" settlement identity on MPP + task rail (fold into spikes 2 and any A2A run).
8. Dynamic per-request x402 accepts (per-store payTo from DB) in `okxweb3-app-x402` 0.1.0 (gates M7 mechanism; fallback = hand-built 402 + direct facilitator verify/settle).
