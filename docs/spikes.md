# Spikes — verification log (gates the modules they precede)

Each spike is a read-only verification run before its dependent module. Results
are facts, not claims; a rail is only ever CLAIMED working when a real settlement
tx hash is logged (BUILD.md M8 binary acceptance).

## Spike 8 — dynamic per-request x402 accepts (gated M7) — PASS

`okxweb3-app-x402` 0.1.0 resolves per-request `payTo`/`price` from async
`RouteConfig` hooks; used live for `POST /s/<slug>/buy`. No fallback needed.

## Spike M8a — `aggr_deferred` server class (gates M8a) — PASS (installed)

`AggrDeferredEvmScheme` is present at
`x402/mechanisms/evm/deferred/server.py` — a thin `ExactEvmScheme` wrapper
(`scheme = "aggr_deferred"`), so building payment requirements is identical to
exact; the facilitator handles the deferred verify/settle. Registered only behind
`TILLA_AGGR_DEFERRED`. **Settlement is USER-gated:** the buyer MUST be an OKX TEE
agentic wallet (a plain EOA is rejected at facilitator verify), and the deferred
settle may return success with **no tx hash at serve time**, so BINARY-ACCEPTANCE
evidence requires later polling `session/settle` status for the aggregated tx.

## Spike M8b — MPP session loop (gates M8b) — SDK VERIFIED, live-settle USER-gated

`okxweb3-app-mpp` 0.1.0 installs module **`mpp_evm`** (its base `pympp` installs
module **`mpp`**). The SA client is `mpp_evm.saclient.OKXSAClient` with async
methods `session_open` / `session_top_up` / `session_settle` / `session_close`
(POST `/api/v6/pay/mpp/session/*`) and a **read-only `session_status(channel_id)`**
(GET `/api/v6/pay/mpp/session/status?channelId=…`) — this resolves the earlier
"read-endpoint unverified" caveat: `session_status` IS the read-only probe. Import
stays lazy behind `TILLA_MPP_ENABLED`. **Live-settle USER-gated:** a real
open→voucher→close needs a funded channel (real USDT into escrow
`0x5E550002e64FaF79B41D89fE8439eEb1be66CE3b`) + SA creds.

## Spike M8c — Node sidecar `period` support (gates M8c) — PASS (dry-run)

`@okxweb3/app-x402-evm` 0.2.0 `PermitSubscriptionScheme` (`scheme = "period"`,
EIP-712 domain `A2APaySubscription`, contract `0x3b01…8032`) + facilitator REST
`/api/v6/pay/x402/subscriptions*` confirmed. `node sidecar/sample-buyer.js` runs
the full challenge→sign→verify dry-run and exits 0 without creds. **Live-settle
USER-gated:** a real subscribe/charge needs OKX creds + a funded Permit2 buyer.

---

## Read-only creds-check commands (orchestrator runs these; none can move funds)

All three are read-only GETs authenticated with the existing `/opt/tilla/.env`
OKX creds. Load the creds into the shell env first (NOT via ssh argv, to keep
secrets out of the process table):

```bash
cd /opt/tilla && set -a && . ./.env && set +a
```

### (1) x402 facilitator — gates `aggr_deferred` AND confirms `period`

```bash
.venv/bin/python - <<'PY'
import os, json
from app.payment import NoRedirectOKXFacilitatorClient
from x402.http import OKXAuthConfig, OKXFacilitatorConfig
fac = NoRedirectOKXFacilitatorClient(OKXFacilitatorConfig(
    auth=OKXAuthConfig(
        api_key=os.environ["OKX_API_KEY"],
        secret_key=os.environ["OKX_SECRET_KEY"],
        passphrase=os.environ["OKX_PASSPHRASE"]),
    base_url="https://web3.okx.com", sync_settle=True))
sup = fac.get_supported()   # GET /api/v6/pay/x402/supported (OK-ACCESS-* HMAC)
kinds = [k.model_dump() for k in sup.kinds]
print(json.dumps(kinds, indent=2))
def has(s): return any(k.get("scheme")==s and k.get("network")=="eip155:196" for k in kinds)
print("aggr_deferred@eip155:196:", has("aggr_deferred"))
print("period@eip155:196:", has("period"))
PY
```

- 200 + `aggr_deferred@eip155:196 True` ⇒ safe to set `TILLA_AGGR_DEFERRED=1` and
  restart (the OPTION goes live; still no settlement claim).
- HTTP 401/403 ⇒ creds-gated (flag stays off). 200 without the scheme ⇒
  facilitator-gated (flag stays off).

### (2) subscription facilitator — JS side (requires the sidecar running w/ creds)

```bash
curl -s 127.0.0.1:8790/health/creds | python3 -m json.tool
```

- `{"configured": false}` (HTTP 503) ⇒ creds not in the sidecar's env.
- `configured: true` + `periodSupported: true` + a structured `subscriptionLookup`
  (not an auth error) ⇒ creds authenticate against the subscriptions family
  (funds-gated only). A 401-shaped `subscriptionLookup` ⇒ creds-gated.

### (3) MPP SA API — read-only session status with a nonexistent id

```bash
.venv/bin/python - <<'PY'
import os, asyncio
from mpp_evm.saclient import OKXSAClient
async def main():
    c = OKXSAClient(base_url="https://web3.okx.com",
        api_key=os.environ["OKX_API_KEY"],
        secret_key=os.environ["OKX_SECRET_KEY"],
        passphrase=os.environ["OKX_PASSPHRASE"])
    try:
        r = await c.session_status("mpp_probe_nonexistent_0000")  # GET /session/status
        print("status:", r.model_dump())
    except Exception as e:
        print("session_status ->", type(e).__name__, str(e))
    finally:
        await c.aclose()
asyncio.run(main())
PY
```

- A structured not-found (200 with an empty/`not_found` status, or a mapped
  not-found error) ⇒ creds authenticate against `/api/v6/pay/mpp` (funds-gated).
- HTTP 401/403 ⇒ creds-gated (`TILLA_MPP_ENABLED` stays off).

None of the three probes can move funds. The fund-moving acts (TEE-buyer settle,
channel-open deposit, Permit2 charge) are USER-owned by construction.

## M8 go-live (flag-flip) BLOCKERS — do NOT enable these flags until fixed

> **RESOLVED — historical (marker added 2026-07-26).** All three flags below were subsequently
> fixed, enabled in production by the human, and proven with real on-chain settlements
> (`docs/PROOF-onchain.md` §§4, 5, 7). The imperative "do NOT enable" no longer describes
> production and is kept only as the record of what had to be true first. The engineering
> reasoning below is still accurate and worth reading.
- **subscription (TILLA_SUBSCRIPTIONS_ENABLED):** replay goods-leak — RESOLVED by gating, NOT by binding to `terms.payer`. (An earlier attempt compared `from_addr` against `terms.payer` — a review PROVED that worthless: `terms.payer` is plaintext in the PUBLIC envelope, so a replayer just resubmits the victim's own address and the compare passes. There is no local ecrecover of `termsSignature`→`payer` anywhere in the SDK; only the on-chain facilitator settle binds them.) The sound fix mirrors the M5 cid-bearer pattern: `_subscription_body(include_gated=...)`. First settle is authenticated by the on-chain facilitator settle (so `from_addr` is recorded from a real bound signer and the gated goods are returned inline). A REPLAY is NOT re-authenticated (the envelope is public/re-submittable), so for an **entitlement-backed** deliverable the license key / download token / text secret is WITHHELD and replaced by the neutral claim message + `claim:true`; the real payer retrieves the goods via the wallet-session `/api/library` (personal_sign as `from_addr` — a replayer cannot forge that signature). A legacy `store.delivery` text order (no entitlement, not a per-buyer secret) passes through unchanged, exactly as M5 does. No migration (reused `from_addr`). Still NOT live: a real subscribe/charge needs OKX creds + a USER-funded Permit2 buyer, so the flag stays OFF (USER-gated).
- **aggr_deferred (TILLA_AGGR_DEFERRED / AGGR_DEFERRED_ENABLED):** ~~build the get_settle_status reconciliation poller so a deferred (async) settle is only claimed once its aggregated tx hash is confirmed; today a no-tx deferred settle correctly stays `settling` (never falsely delivered) but there is no poller to finalize it.~~ **RESOLVED (still USER-gated for live settle).** `app.reconcile` is a DORMANT background poller (own flag gate: starts ONLY under `SWEEP_ENABLED AND AGGR_DEFERRED_ENABLED AND OKX creds`, time-boxed off-loop tick, bounded batch, no network in tests). A deferred settle that returns success with an unconfirmed aggregated ref (no tx, or `status=="pending"`) now stays `settling` and `record_settlement` persists the pollable ref on `Order.settle_ref` (migration `0019_settle_ref`, additive). The poller queries `OKXFacilitatorClientSync.get_settle_status(settle_ref) -> SettleStatusResponse` and finalizes ONLY on `success + status=='success' + a confirmed tx` (settling→delivered via `_finalize_settled`, recording the tx); `failed`/definitive-failure voids (settling→canceled via `_void_settling`); pending / success-without-tx leaves it settling. Idempotent (conditional `settling->*` UPDATEs). **Review MINOR fixes:** (a) only an EXPLICIT `status=='failed'` voids — a missing/None status on a transient facilitator error no longer voids a paid order (fail-safe: stays settling, retries); (b) the 15-min agent-order reaper now EXEMPTS `settle_ref`-bearing (aggr) orders, since an aggregated tx can confirm after the reap window and the poller owns their lifecycle — otherwise a genuinely-paid-but-slow order could be reaped-voided without refund. Enabling the rail for a LIVE settle remains user-gated (TEE-buyer settle moves funds; flag flip after the `/supported` probe confirms `aggr_deferred@eip155:196`).
- **All rails:** a real settlement tx hash must be logged before the rail is claimed working (BUILD.md binary acceptance).

## M11 attester (TILLA_ATTEST) flag-flip hardening
- **crash-window double-attest — RESOLVED (0019).** The attest tx is now BUILT + SIGNED first (yielding its hash + nonce), the order is claimed `pending -> sending` recording `attest_tx` + the new `attest_nonce` column BEFORE the broadcast, then broadcast, then `sending -> sent -> attested`. A crash in the send window therefore only ever leaves a reconcilable `sending` row (broadcast intent never lost). On restart `_reconcile_sending` checks the chain by the recorded hash + nonce (nonce = dedup key, at most one tx per nonce mines): found => `attested`; nonce consumed by a different mined tx => definitively not on chain, re-attest fresh; nonce still free => re-broadcast the SAME-nonce tx (idempotent) and advance to `sent`. No blind re-broadcast, gas-safe. All UPDATEs are conditional (single-writer). **Review MAJOR fix:** the receipt read and the nonce read are not atomic, so a tx mining in the gap (or an RPC flake, since `_get_receipt` swallows errors to None) could make `confirmed > attest_nonce` misfire a fresh re-attest = a double-attest; reconcile now RE-CHECKS the receipt once before any fresh re-attest, so only a confirmed real not-found re-attests. **Also:** `ATTEST_MAX_PER_TICK` defaults to 1 — two orders in one tick would fetch the same account nonce and collide (churn, or a gas-bump replacement stranding one order in a terminal `failed`). Still DORMANT (3-gate: SWEEP_ENABLED + TILLA_ATTEST + key); live attesting remains USER-gated on key mint + OKB funding + a testnet-faucet dry-run before mainnet flip.
