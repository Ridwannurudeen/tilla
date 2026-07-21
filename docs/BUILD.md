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

**BUILD STATUS (branch `m11/onchain-depth`):** code shipped DORMANT. `app/attest.py`
is the EAS receipt attester (schema `address buyer,string storeId,uint256
amountUsdt6,bytes32 paymentTxHash`, UID `0x09bb2adc…f9fe29`), drained by a background
`attest_loop` that is TRIPLE-GATED off (`SWEEP_ENABLED` + `ATTEST_ENABLED` +
`TILLA_ATTESTER_KEY`). Flag off / key unset ⇒ zero RPC, zero gas, `web3` never
imported. Orders queue `attest_status='pending'` in the same txn as delivery
(`checkout.deliver` for web, `agentic.record_settlement` on settling→delivered for
agent). Migration `0008_onchain_receipts` adds `attestation_uid`, `attest_tx`,
`attest_status` (+ `ix_orders_attest_status`) via plain ADD COLUMN (the M3 partial
unique index is untouched). Receipt UID + OKLink attest-tx link surface on the buyer
receipt (`_order_response`) and merchant order detail. `StoreRegistry.sol` +
Foundry scaffolding live under `contracts/` — PREPARED, NOT DEPLOYED.

**USER-GATED runbook (each an explicit approval step — build agents never sign/deploy/fund):**
1. Mint/choose an attester key; set `TILLA_ATTESTER_KEY` + `TILLA_ATTEST=1` in `/opt/tilla/.env` (chmod 600).
2. Fund it with OKB gas. Recommended: testnet dry-run first — `TILLA_ATTEST_CHAIN_ID=1952` + a testnet `TILLA_ATTEST_RPC`, reuse the spike throwaway key + <https://web3.okx.com/xlayer/faucet> — then mainnet 196 with a few dollars of OKB.
3. One-time mainnet schema register: the worker does it automatically on the first enabled tick (enabling IS the approval); bounded by `TILLA_ATTEST_MAX_GAS_WEI`.
4. Optional historical backfill: a runbook UPDATE flipping chosen delivered orders to `attest_status='pending'` (default: only post-enable sales are attested).
5. StoreRegistry: `forge script script/Deploy.s.sol --rpc-url https://rpc.xlayer.tech --broadcast` from `contracts/`, then OKLink contract-verify (see `contracts/README.md`).
6. Verify the attestation is readable — OKLink attest-tx link + `getAttestation(uid)` eth_call on `0x4200…0021`. No verified EAS explorer/indexer for X Layer is linked (rule 22); a WebSearch for one is a cheap user-gated follow-up.

### M12 — Ops (runs alongside, finishes last)
Health/readiness endpoints; systemd watchdog timer + **Telegram alerting on service-down** (Warden ops pattern — same bot channel); sqlite backup verification + **nightly off-VPS copy** (pull/push to a second location — VPS loss must not lose the database); log rotation; deploy runbook + rollback (previous release dir + symlink); Anthropic API noted as generation SPOF — clear 503 + retry on outage, daily spend logged.
**Accept:** kill-test (service auto-recovers + alert fires); restore-from-backup drill documented + executed once **from the off-VPS copy**.

**Build status — APP-SIDE BUILT + TESTED; VPS artifacts PREPARED (orchestrator applies, staged + reversible).** Shipped this milestone:
- App: new `GET /ready` (DB `SELECT 1` + migration-head check + zero-cost sweeper/RPC heartbeats; unauth, no limiter, never raises — 200/503). `/health` unchanged. RPC hardening — a `BoundedSemaphore(8)` fail-fast cap (`ChainBusy`), a shorter 5s request-path RPC timeout (sweeper keeps 10s), and a fixed 16-worker default executor. Anthropic outage → `GenerationUnavailable` → **503 + `Retry-After: 60`** (was an unhandled 500); one in-process retry on transient (429/5xx/529/connect/timeout), non-transient 4xx fails fast; 503 ≥ 400 so **x402 skips settle — zero funds move on outage**; LLM token spend logged to journald + `event_log`.
- Repo artifacts (files only, LF): `scripts/watchdog.sh`, `scripts/backup_offsite.sh`, `scripts/restore_drill.sh`, extended `scripts/backup_db.sh` (deliverables mirror + failure Telegram), `deploy/tilla-watchdog.{service,timer}`, `deploy/tilla-api-override.conf` (systemd hardening drop-in, `User=root` untouched), `deploy/logrotate-tilla`, `deploy/nginx-m12.snippet`, reconciled `scripts/deploy.sh` (M9–M11 FILES drift fixed + `/ready` smoke). Full ops runbook, restore runbook, drill log, and the honest coverage/residuals statement in `docs/OPS.md`.
- **VPS apply is orchestrator-owned** (build agents never touch the VPS): staged drop-in + watchdog + cron + logrotate + nginx (conf.d-gated), each with a pre-captured rollback artifact; acceptance = kill-test + restore drill. **USER-gated:** off-VPS backup destination (`/etc/tilla-backup.env`) and an external uptime probe (the single-VPS SPOF) — until provided, same-disk keep-7 stays the honest default and no HA is claimed. See `docs/OPS.md`.

### M13 — Growth (post-hackathon; Phase 8)
Affiliates, external feeds, embed button, ACP checkout. Additive-only: no existing route, 402 challenge, `feed.json` byte, or theme contract changes when unused; **zero fund-moving code introduced anywhere.** One migration `0009_growth` (additive, `render_as_batch`, pre-tested up/down/up on a prod-shape DB — the M3 partial unique index `ux_orders_active_amount` provably survives).
**Accept:** full suite green (existing + new); no signer in `app/affiliates.py` (grep-asserted); self-referral blocked; feeds leak no `pay_to`; embed source XSS-safe; ACP tx-hash complete verifies exactly like the human `/tx` path.

**Build status — APP-SIDE BUILT + TESTED; nginx PREPARED (orchestrator applies).** Shipped:
- **Affiliates** (`app/affiliates.py`): bare-EVM-address attribution captured first-write-wins + immutable across web body / agent `?ref=` / MCP arg / ACP object; accrual ledger at the delivered/settled seam (`accrued_micro = basis * TILLA_AFFILIATE_BPS/10000`, default 2%); self-referral guard; full-refund void; merchant read surface + `affiliate_owed_micro`; referrer self-balance (`GET /api/affiliate/summary`, wallet-gated). **Payout = verify-and-record only** (`POST /api/merchant/affiliates/{addr}/payout`): confirms an on-chain USDT0 Transfer to the referrer (≤ owed, `UNIQUE(tx_hash,log_index)`) and flips accruals `paid` — **no signer, no fund movement anywhere** (the M9 refund philosophy).
- **External feeds** (`app/external_feeds.py`): `GET /s/{slug}/feed/openai.json` (validated vs new `docs/openapi.external-feeds.yaml`), `/feed/google.xml` (RSS `g:` namespace, every node XML-escaped), `/feeds/openai.json` aggregate. Live-status-only 404; no wallet/buyer/order data; nosniff + 5-min cache.
- **Embed** (`app/embed.py` + `assets/embed.js`): `GET /embed.js` serves a self-contained shadow-DOM "Buy with USDT" button (textContent-only, hard-coded base, popup not iframe, strict slug/ref validation).
- **Email capture**: public `POST /api/stores/{slug}/waitlist` (CRLF-strip + `valid_email` + 255-cap, dup-silent, per-store cap + 5/min limiter, live-only); merchant list/`DELETE`/`subscribers.csv` (formula-injection guarded). **Sending stays dormant** (SMTP unset no-ops with an `event_log` row); no broadcast endpoint ships.
- **ACP** (`app/acp.py`): five `/s/{slug}/checkout_sessions` endpoints over the M3 order machinery, **mounted dormant-503 behind `TILLA_ACP_ENABLED`**; `Idempotency-Key` per store, `API-Version` echoed, optional HMAC signature (`TILLA_ACP_SIGNING_SECRET`). Complete ships the **tx-hash / `onchain_usdt0`** mode (exact `verify_txhash`); the **x402-middleware complete mode is spike 9 — PARKED**, so tx-hash mode alone is claimed.
- Repo artifacts (files only, LF): `deploy/nginx-growth.snippet` (regex alternation extension + `/feeds/` + `/embed.js` + `X-Frame-Options: SAMEORIGIN`, nginx-m7 discipline), reconciled `scripts/deploy.sh` (FILES + growth smoke), theme `?ref=` forwarding (`themes/_checkout.html`, re-rendered on restart), `docs/acp-checkout.md`.
- **USER-gated / PARKED (never claimed until a real artifact exists):** affiliate payout execution (manual operator send); ChatGPT/Instant-Checkout + Perplexity + Google Merchant Center *listings* (external approval/enrollment); SMTP sends + broadcasts; ACP x402-complete (spike 9); affiliate-rate UI + non-address codes. Flip `TILLA_ACP_ENABLED` only after a live tx-hash-complete smoke. See `docs/acp-checkout.md`.

### M14 — Phase 3–4 vision: the ONE buildable headline + growth-agent seed (post-hackathon)
The full Phase 3–4 "commerce OS" (plugin ecosystem, cross-chain, B2B/federation, autonomous growth agent) is explicitly **multi-quarter vision**, designed **not** built — see `docs/VISION.md` (DESIGNED-NOT-BUILT banner on the doc and every section). This milestone ships only the two slices that are buildable and honest today; no schema change (alembic head stays `0009`).

**Build status — BUILT + TESTED.** Shipped:
- **Open SDK — Python (`sdk/python/`, `tilla-sdk`):** a separately-installable, sync, typed, dependency-light (`httpx` only; `[signer]` extra = `eth-account`) client wrapping only endpoints live today — discovery/search, `feed.json`, `llms.txt`, agent card, human checkout + `wait_for_paid`, the MCP JSON-RPC tool surface, and the two x402 pay paths (`create_store`, `buy`). It mirrors the production `warden_hire` payer invariant-for-invariant: **refuse-before-sign** on any scheme/network/asset/cap mismatch, **sign at most once**, **never re-fire a signed authorization** (post-sign transport failure → `SettlementUnknown`). The SDK **never bundles, reads, defaults, or logs a key** — the caller supplies a `PaymentSigner` hook (`LocalEip3009Signer` reproduces `warden_hire`'s EIP-712 signing; a remote signer never exposes the key). Hand-rolled `base64(JSON)` x402 codec (verified against `okxweb3-app-x402` 0.1.0 — plain base64(JSON), so no runtime x402 dep). **Never part of the app deploy:** root pyproject stays `packages=["app"]`, `deploy.sh` FILES never lists `sdk/`. Own respx-mocked pytest suite (28 tests, zero network/funds) wired as an isolated CI job; funds-safety matrix asserts the signer is provably never called on a pin/cap veto and exactly one signed replay on a post-sign transport failure. Codec golden tests decode REAL x402-encoded fixtures. Runnable examples: `examples/browse_and_checkout.py` (no key/funds) and `examples/agent_buy.py` (user-gated, real funds, interactive confirm, never in CI).
- **Growth-agent seed (`app/growth.py`):** merchant-gated `POST`/`GET /api/stores/{slug}/growth-kit` (reuses the `_require_store_key` seam; live-only 409). POST (6/hour) builds a prompt EXCLUSIVELY from the persisted, already-screened `store.content` (no new prompt surface), calls the existing `engine._post_generation` LLM seam (outage/malformed/oversize → 503 + `Retry-After`, the create-store contract; token spend logged), validates a strict `extra="forbid"` `GrowthKit` (3 social posts ≤280, launch tweet ≤280, email subject ≤78), **re-screens fail-closed** via `screening.screen()` (BLOCK → 422, unavailable → 503), and persists to the append-only `event_log` (`growth.kit_generated`) — which serves the GET read-back for free. **No fund-moving, no SMTP, no social/webhook fan-out**; XSS-safe by construction (JSON + nosniff only). `/api/` is already proxied by nginx — **no nginx change**.

**Accept:** full suite green (existing + growth); SDK suite green in its own job; `ruff check`/`format --check` clean; `import app.main` with no OKX/LLM/DB env; growth-kit generates for an owned store, IDOR-blocked for a non-owner, re-screened, rate-limited; SDK x402 buy/create-store build the right signed payload without a real key/network (funds-safety matrix). **USER-gated / PARKED (never claimed until a real artifact):** the live SDK x402 buy/create-store e2e (operator's funded second wallet, small amount, labeled self-trade — not claimed until its settle tx hash is logged); PyPI publish of `tilla-sdk`; everything in `docs/VISION.md` §§1–3 and the TS port in §5.

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
