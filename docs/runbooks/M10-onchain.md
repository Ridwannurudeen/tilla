# M10 on-chain runbook — marketplace citizenship (APPROVAL-GATED)

**This file is documentation only. No build agent, test, or workflow executes any
step here.** The orchestrator runs these steps on the VPS by hand, one at a time,
**only after explicit user approval of each on-chain / funds action**. `onchainos`
is never invoked from application code; the app never SSHes, never moves funds.

What the app already ships LIVE (no on-chain action, no funds path reachable):
`/upgrade-store` + `/add-product` (ordinary x402 HTTP services, same class as the
live create-store), the `GET /s/:slug/buy` 402-challenge registration, migration
0007 + demo-mode screening receipts, `app/warden_hire.py` shipped DORMANT
(`TILLA_WARDEN_PAID` off, no payer key on the VPS), the read-only dashboard
marketplace panel, and the `app.mark_listed` command. Everything below is PARKED
for user approval.

---

## ENV (run every step as root on the VPS)

```
XDG_RUNTIME_DIR=/run/user/0 HOME=/root/warden-agent PATH=/root/.okxbin:/usr/local/bin:/usr/bin:/usr/sbin:/bin
```

## CONTENT RULES for every serviceName / serviceDescription (learned on #3808)

Plain buyer-vocabulary text describing what the buyer gets. **NO** links/URLs, **NO**
prompts or prompt-like text, **NO** tech-stack terms. `fee` is a JSON **string**.
Run the CLI's `validate-listing` on every drafted service until CLEAN before submit.

---

## STEP 0 — read-only pre-checks (no chain writes, run freely)

```
onchainos agent x402-check --endpoint https://tilla.gudman.xyz/upgrade-store
onchainos agent x402-check --endpoint https://tilla.gudman.xyz/add-product
onchainos agent x402-check --endpoint https://tilla.gudman.xyz/s/<slug>/buy --body '{}'
```

All must report `valid:true`. (`/s/<slug>/buy` needs `--body '{}'` to probe POST;
the new GET-402 registration removes the GET-probe failure mode that stalled #3808.)

Snapshot services BEFORE any update — **service IDs WILL be reassigned** by any
`--service` update:

```
onchainos agent service-list --agent-id 6961 > /root/tilla-services-before.json
```

Run `validate-listing` on every drafted service JSON until CLEAN.

---

## STEP A (USER-GATED) — list the two platform services in ONE update call

One review cycle, one ID reshuffle:

```
onchainos agent update --agent-id 6961 --service '[{"operation":"create","serviceName":"Upgrade a storefront","serviceDescription":"Refresh an existing Tilla store: new copy and look for a store you own, applied only after a safety screen passes.","serviceType":"A2MCP","fee":"1","endpoint":"https://tilla.gudman.xyz/upgrade-store"},{"operation":"create","serviceName":"Add a product to a storefront","serviceDescription":"Add another product with its own price to a Tilla store you own.","serviceType":"A2MCP","fee":"0.5","endpoint":"https://tilla.gudman.xyz/add-product"}]'
```

If the CLI reports re-submission needed:

```
onchainos agent activate --agent-id 6961 --preferred-language en-US
```

Expect `approvalStatus 2` (under review, <=24h). Do NOT poll aggressively. **Log the
submit + approval timestamps — review latency is an M10 acceptance metric.**

> WARNING: after ANY `--service` update, ALL of #6961's service IDs are reassigned
> and the agent drops to `approvalStatus 2` (temporarily invisible in marketplace
> search while its endpoints keep working via Agent ID). Re-run `service-list`,
> never hardcode service IDs anywhere.

---

## STEP B (USER-GATED, per approved store — store-as-ASP Option A)

One delta-service per store under #6961 (the pattern proven on Warden escrow service
35484). A store already HAS the required paid x402-valid endpoint (`/s/<slug>/buy`,
non-custodial payTo=merchant, proven `x402-check valid:true` in M7). Batch multiple
stores into one call when possible:

```
onchainos agent update --agent-id 6961 --service '[{"operation":"create","serviceName":"Buy <Product> from <StoreName>","serviceDescription":"<one plain sentence: what the buyer receives on payment>","serviceType":"A2MCP","fee":"<exact product price as string, e.g. \"9\">","endpoint":"https://tilla.gudman.xyz/s/<slug>/buy"}]'
```

Same review-cycle side effects as STEP A (ID reshuffle + approvalStatus 2). Re-run
`service-list` afterward.

---

## STEP C (USER-GATED, Option B experiment — ONE store, only after A is listed)

Per-store ASP identity (own listing card, own reputation). Feasible under the one
Agentic Wallet (multiple agents per wallet is proven: #3808 + #4844 share
0xf4c9…fa51), but costs a per-store on-chain create + human review and has UNKNOWN
multi-ASP review throughput. Run as a **measured experiment on exactly ONE store**:

```
onchainos agent create --role asp        # guided flow: store name / description / avatar
onchainos agent activate --agent-id <newId> --preferred-language en-US
```

Log review latency. **STOP after one store.**

---

## STEP D (USER-GATED, FUNDS) — enable the paid Warden hire

This is Tilla spending its own USDT0 to hire Warden #3808's paid scan (0.01
USDT/scan). One-clean-tx rule: never re-fire to verify.

1. Fund the Tilla payer wallet with a few USDT0 on X Layer.
2. On the VPS, add to `/opt/tilla/.env` (then `chmod 600 /opt/tilla/.env`):
   ```
   TILLA_WARDEN_PAID=1
   TILLA_WARDEN_PAYER_KEY=<hex private key of the funded payer wallet>
   ```
   (Optional overrides if defaults are wrong: `TILLA_WARDEN_SCAN_URL`,
   `TILLA_WARDEN_PAYTO`, `TILLA_WARDEN_MAX_MICRO`.)
3. `systemctl restart tilla-api` (allow ~10s to bind).
4. Create ONE test store, then verify **exactly one** 0.01 USDT settle tx in
   `screening_receipts` (a `mode='paid'` row with a `tx_hash`) and on OKLink.

Funds-safety already built into the client (`app/warden_hire.py`): it refuses to
sign unless the 402 challenge's scheme/asset/network/payTo match the pins and the
amount is within `TILLA_WARDEN_MAX_MICRO`; it signs at most once and never retries a
signed authorization; any paid-path failure degrades to the free demo scan.

> Evidence integrity: Tilla's payer and Warden's payTo share an operator wallet. The
> receipt is real on-chain evidence of the x402 agents-hiring-agents mechanism — it
> must NEVER be presented as external demand. Demo-mode receipts are labeled 'demo'.

---

## STEP E (after approvals land) — record listing state for the dashboard

```
cd /opt/tilla && .venv/bin/python -m app.mark_listed <slug> listed
```

Writes `stores.marketplace_status` + `marketplace_listed_at` + an event_log row; the
read-only dashboard marketplace panel then shows the store as listed. Valid statuses:
`unlisted | prepared | submitted | listed | rejected`.

---

## What the USER approves

- (STEP A) Listing `/upgrade-store` + `/add-product` on #6961 — on-chain agent
  update + review cycle. Approve the exact service JSON text.
- (STEP B) Which store(s) to list as delta-services (Option A) + the exact service
  JSON text.
- (STEP C) Whether to run the single Option B per-store ASP experiment.
- (STEP D) Funding the payer wallet + flipping `TILLA_WARDEN_PAID` (Tilla spends
  USDT on Warden hires).
- (STEP E) Marking stores listed (follows A/B approval).

## Out of scope (per BUILD.md M10)

XMTP task-board participation / negotiation / ratings — the dispatch runtime is not
provisioned, so these stay out of scope and out of claims.
