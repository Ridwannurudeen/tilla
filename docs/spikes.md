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
- **subscription (TILLA_SUBSCRIPTIONS_ENABLED):** payer-binding — a subscription replay is now store-scoped, but a same-store replay of a public terms-signature still delivers that store goods to a non-signer. Before flag-flip: record the payer (recovered signer) on the subscription Order and require the replaying request signer == that payer.
- **aggr_deferred (TILLA_AGGR_DEFERRED / AGGR_DEFERRED_ENABLED):** build the get_settle_status reconciliation poller so a deferred (async) settle is only claimed once its aggregated tx hash is confirmed; today a no-tx deferred settle correctly stays `settling` (never falsely delivered) but there is no poller to finalize it.
- **All rails:** a real settlement tx hash must be logged before the rail is claimed working (BUILD.md binary acceptance).

## M11 attester (TILLA_ATTEST) flag-flip hardening
- **crash-window double-attest:** _attest_one broadcasts before the pending->sent DB claim, so a process crash between broadcast and claim can re-attest (wasteful OKB gas, NOT fund loss). Before enabling the attester on mainnet: reorder to an intermediate sending state + nonce-based dedup on reconcile, OR accept the rare double-attest as bounded-gas. Dormant (flag off + no key) until then.
