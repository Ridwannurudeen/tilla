# M18 — Cross-chain checkout (VISION §2 → buildable spec)

**Status: SPEC.** Derived from `docs/VISION.md` §2. **Verified against code
2026-07-21:** what the facilitator supports today, per `app/payment.py` +
`docs/spikes.md`, is exactly **`exact` (+ flag-gated `aggr_deferred`) on
`eip155:196` with USDT0 `0x779d…3736`** — a single hard-pinned rail.
`payment.load_payment_rail` REJECTS any divergent `TILLA_PAYMENT_*` env and any
non-default `OKX_BASE_URL`; `_FIXED_CONFIGURATION` freezes scheme/network/asset.
The spikes.md `/supported` probe (`fac.get_supported()`,
`GET /api/v6/pay/x402/supported`) is the ONLY honest source of truth for a second
network. So the buildable subset is: the accepts-list/config abstraction, the
probe-driven gate, and chain-guard tests — **settlement on any other chain is
EXTERNALLY-BLOCKED** (see Parking).

**Hard rules carried forward (VISION §2, non-negotiable):**
- INV-1: never advertise an unsettleable rail (the M8 lesson) — a chain enters an
  `accepts` list ONLY after the read-only `/supported` probe confirms
  `(scheme, network)` and the flag is flipped in the VPS `.env`.
- INV-2: X Layer USDT0 (`eip155:196`) stays the canonical settlement ledger.
- INV-3: no custody, no bridge code, no in-code fund movement; every on-chain
  step user-gated.

## Threat model (wrong-chain / wrong-asset fund loss)

| Threat | Defense | Test |
|---|---|---|
| Buyer pays the right amount on the WRONG chain (funds stranded/lost) | Tx-hash verification (`checkout.verify_txhash` path) already pins the USDT0 contract + Transfer topic on the 196 RPC; extend: per-order `network` recorded at creation, and verification runs ONLY against that network's pinned RPC+asset — a receipt from any other chain can never confirm an order. Human checkout page renders chain name + auto-switch (`wallet_switchEthereumChain 0xc4`, M5) and warns explicitly. | `test_wrong_chain_receipt_never_confirms` |
| Wrong-asset payment (same chain, different token) | Already enforced: log-decode requires `config.USDT0` contract; generalize to `chain_config.asset` and keep the assert. | `test_wrong_asset_ignored` |
| Advertising an unsupported network in a 402 | `build_store_payment_options` gains a per-chain gate: an accepts-entry for network N is appended ONLY if `TILLA_CHAIN_<id>_ENABLED` AND the startup `/supported` snapshot lists `(exact, N)`; flag on + probe miss ⇒ refuse to start the entry (loud log), challenge stays 196-only. | `test_402_never_lists_unprobed_chain` |
| Config drift creating a mixed-rail order | Order records `(network, asset, amount)` at creation; settle/verify re-derives from the order row, never from current env — an env change mid-flight cannot re-point an open order. | `test_open_order_pins_original_chain` |
| Hostile "bridge in" link injection | The bridge affordance (18.3) is a hard-coded operator-configured URL constant, never merchant/LLM content; rendered with the M1 `https?://` allowlist + third-party-risk label; zero bridge state in Tilla. | `test_bridge_link_operator_only` |
| Sentinel abuse on unknown chains | The dynamic-hook sentinel rule (`payment._dynamic_store_hooks`: dead store ⇒ ≥400 pre-settle ⇒ middleware skips settle) must hold per-chain; conformance test parametrized over configured chains. | `test_sentinel_skips_settle_per_chain` |

## Increments

### 18.1 — Per-chain config schema + rail refactor (no behavior change) (M)
Introduce `ChainConfig` (frozen dataclass in `app/payment.py`): `chain_id,
caip2 ('eip155:196'), rpc_url, ws_url, asset, asset_symbol, decimals,
eip712_name, eip712_version, explorer_tx_base, canonical: bool`. A registry
`CHAINS: dict[str, ChainConfig]` seeded with EXACTLY ONE entry — X Layer 196,
built from today's constants (`PAYMENT_NETWORK/ASSET`, `config.RPC_URL/USDT0/
OKLINK_TX_BASE`) — plus a commented-out-shape example documenting the schema.
`load_payment_rail`, `build_*_payment_option(s)`, `chain.py` reads, and
`checkout` verification take the config object instead of module constants.
`Order` gains `network` (migration `0014_crosschain`, additive, default
`eip155:196` backfill; renumber to next free head at build — 0010 pending
elsewhere, M15–M17 take 0011–0013).

**Accept (binary):** claimed ONLY if the 402 challenge, feed bytes, and full
existing suite are UNCHANGED (golden test `test_402_challenge_byte_identical`),
migration up/down/up passes prod-shape, and new tests
`test_single_chain_registry_default`, `test_open_order_pins_original_chain` pass.

### 18.2 — `/supported` probe gate + accepts-list abstraction (M)
> **Carry-over from the 18.1 review:** the sweeper (`sweep_tick`/`_current_head`/
> `_active_addresses`/`_match_order`) is pinned to `CANONICAL_CHAIN`, so the moment a
> second chain can mint orders, 18.2 MUST make sweeping per-chain (own cursor + own
> head for maturity) or non-canonical orders orphan (funds-received-no-goods unless
> the buyer submits a txhash). `_promote_matured` already skips unregistered-network
> rows without killing the tick (18.1 review fix) — keep that invariant.
>
> **GO-LIVE BLOCKER from the 18.2 review (must fix before ANY second-chain go-live;
> harmless while every `TILLA_CHAIN_<id>_ENABLED` is OFF and only 196 exists):** the
> store `_store_route` accepts list is built ONCE at import time (`app/main.py`,
> `build_store_payment_options`), before the lifespan probe runs — so a flag flipped
> on + a probe that lists the chain still leaves the live accepts frozen 196-only.
> Fixing the flag path means (a) rebuilding the accepts list AFTER the probe, and
> (b) `_srv.register(<caip2>, ExactEvmScheme())` per enabled chain — today only 196's
> scheme is registered, so an accepts entry for chain B would make `initialize()`
> raise → 502 on every protected route. Both must land together. Fail direction is
> safe (never advertises a chain it can't serve), which is why this is a go-live
> blocker, not a live bug.
Startup (lifespan) read-only probe using the exact spikes.md client
(`NoRedirectOKXFacilitatorClient(...).get_supported()`); snapshot cached with the
process (probe failure ⇒ 196-only, never a crash — 196 is grandfathered as the
proven rail). `build_store_payment_options` becomes: for each `ChainConfig` with
`TILLA_CHAIN_<id>_ENABLED`, append an exact accepts-entry iff the snapshot lists
`(exact, caip2)` — sharing the dynamic pay_to/price hooks (per-store payTo works
unchanged; price converts via each chain's `decimals`, same-6dp USDT-family only
in v1, no FX). The chain-guard test-set from the threat table lands here.

**Accept:** `test_402_never_lists_unprobed_chain` (flag on + snapshot without the
network ⇒ challenge unchanged + loud log), `test_probe_failure_falls_back_196`,
`test_sentinel_skips_settle_per_chain`, respx goldens of the probe; live smoke:
probe output logged from the VPS (creds read-only, spikes.md §creds-check
pattern). **No claim of a second chain — today's real probe shows only
`eip155:196`; this increment's artifact is the gate working, proven by tests +
the logged probe.**

### 18.3 — Human-checkout chain honesty + "bridge in" affordance (S)
Checkout page (M5 surface, `themes/_checkout.html` + wallet JS): display the
order's chain name/id prominently; keep auto-switch to `0xc4`; add an explicit
"paying on a different network will NOT be detected and may lose funds" warning.
Optional operator-configured `TILLA_BRIDGE_URL` (default empty ⇒ absent): renders
a clearly-labeled third-party "Bridge funds to X Layer" outbound link. Zero
bridge code, zero bridge state (INV-3).

**Accept:** `test_bridge_link_operator_only`, `test_bridge_absent_by_default`,
template escaping tests green across all themes; manual smoke logged (M5 script
extended with the warning + link checks).

### 18.4 — Wrong-chain refusal hardening (S)
Buyer-submitted tx-hash verification: if the submitted hash resolves on the
order's chain but with the wrong asset/recipient — already refused; add the
explicit cross-chain case to the corpus: a syntactically valid hash that does NOT
exist on the order's pinned RPC returns the existing "not found" path with a
buyer-facing hint ("was this sent on X Layer?"), and NEVER triggers a lookup on
any other RPC (no cross-chain search — that would legitimize wrong-chain sends).

**Accept:** `test_wrong_chain_receipt_never_confirms`,
`test_wrong_asset_ignored`, `test_no_cross_chain_rpc_search` (mock asserts a
single RPC host contacted), full suite green.

## Parking (honest, exact missing dependency)

- **EXTERNALLY-BLOCKED — settlement on any second chain:** missing dependency =
  **the OKX x402 facilitator listing `(exact, <second network>)` in
  `GET /api/v6/pay/x402/supported`** (today it lists only `eip155:196` for our
  schemes, per spikes.md). Everything in 18.1–18.2 builds up TO this boundary;
  the day the probe shows a second network, go-live is a `.env` flag + a real
  settlement tx hash on that network (M8 binary acceptance — no hash, no claim).
- **EXTERNALLY-BLOCKED — bridge-in liquidity/route:** missing dependency = a
  vetted third-party bridge the operator is willing to link (URL is the
  artifact); until then `TILLA_BRIDGE_URL` stays empty.
- **PARKED — non-USDT-family assets / FX pricing:** pricing conversion beyond
  same-decimals stablecoins is out of scope until a real second asset exists.
- **USER-gated:** flipping any `TILLA_CHAIN_*` flag; the first real
  second-chain settlement (funds).

## Build order + size
1. 18.1 config refactor (M) → 2. 18.2 probe gate + accepts abstraction (M) →
3. 18.4 wrong-chain hardening (S) → 4. 18.3 checkout honesty + bridge link (S).
Total buildable-now: ~2–2.5 focused days.
