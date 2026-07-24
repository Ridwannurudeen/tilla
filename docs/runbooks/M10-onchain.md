# M10 on-chain runbook — marketplace citizenship (APPROVAL-GATED)

**This file is documentation only. No build agent, test, or workflow executes any
step here.** The orchestrator runs these steps on the VPS by hand, one at a time,
**only after explicit user approval of each on-chain / funds action**. `onchainos`
is never invoked from application code; the app never SSHes, never moves funds.

What the app already ships LIVE (no on-chain action, no funds path reachable):
`/upgrade-store` + `/add-product` (ordinary x402 HTTP services, same class as the
live create-store), the `GET /s/:slug/buy` 402-challenge registration, migration
0007 + demo-mode screening receipts, `app/warden_hire.py` shipped DORMANT
(`TILLA_WARDEN_PAID` off, no payer key on the VPS) together with its local-only
rating write-back, the read-only dashboard marketplace panel, and the
`app.mark_listed` command (`describe` = read-only listing prep, `<slug> <status>` =
listing state). Everything below is PARKED for user approval.

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

## STEP B1 (USER-GATED) — the FIRST THREE listings, prepared end to end

Never hand-write the service JSON. `app.mark_listed describe` builds it from the same
live numbers `/discovery/resources` publishes — product, price, trust tier, sales,
clean-delivery rate, buyer rating — so a listing can never claim a number the public
API contradicts. It is read-only: no `onchainos`, no chain, no DB write.

Candidate stores, in listing order. Re-confirm against the live API before running
(`curl -s 'https://tilla.gudman.xyz/discovery/resources?sort=sold'`) and list the
three with the strongest live reputation — the table below is the repo's evidence,
not a substitute for the live numbers:

| # | slug | evidence it is a real, listable store |
|---|---|---|
| 1 | `invoice-flow` | real settled sales both ways — human wallet checkout AND an agent x402 buy (docs/PROOF-onchain.md 1 + 2), so its trust tier is earned |
| 2 | `sync` | generated live by a stranger's paid `create-store` (docs/PROOF-onchain.md 3) |
| 3 | `highland-roast` | registered on-chain in StoreRegistry, read-back verified (contracts/README.md) |

### B1.1 Prepare — SAFE, read-only, run freely

```
cd /opt/tilla
.venv/bin/python -m app.mark_listed describe invoice-flow
.venv/bin/python -m app.mark_listed describe sync
.venv/bin/python -m app.mark_listed describe highland-roast
```

Each prints `serviceName` / `serviceDescription` / `fee` / `endpoint`, then the exact
`onchainos agent update --agent-id 6961 --service '[…]'` line to run. Shape of the
output (invoice-flow, 4 sales, trust tier `established`):

```
serviceName:        Buy Freelancer Command Center from Invoice Flow
serviceDescription: Freelancer Command Center from Invoice Flow, delivered as soon as the payment clears. Price 9 USDT. Sold 4 times, 100% of sales delivered with no dispute or refund, buyer rating 4.5 out of 5, seller trust tier established.
fee:                9
endpoint:           https://tilla.gudman.xyz/s/invoice-flow/buy
```

A store with no sales yet prints the first two sentences only — a new store reads as
new, never as `None`. A store that is not publicly live, or has no active product, is
refused outright rather than half-listed.

### B1.2 Validate — SAFE, read-only

```
onchainos agent x402-check --endpoint https://tilla.gudman.xyz/s/invoice-flow/buy --body '{}'
onchainos agent x402-check --endpoint https://tilla.gudman.xyz/s/sync/buy --body '{}'
onchainos agent x402-check --endpoint https://tilla.gudman.xyz/s/highland-roast/buy --body '{}'
onchainos agent service-list --agent-id 6961 > /root/tilla-services-before-B1.json
```

All three must report `valid:true`. Then run the CLI's `validate-listing` on each
drafted service JSON until CLEAN: `describe` already follows the content rules above
(no links, no prompt-like text, no tech-stack terms), but the store and product NAMES
are merchant-authored, so they still have to pass.

### B1.3 Submit — OWNER-RUN, ON-CHAIN, one approval cycle

Merge the three `--service` arrays from B1.1 into ONE array and send a single update,
so all three ride one review cycle and one ID reshuffle:

```
onchainos agent update --agent-id 6961 --service '[<service 1>,<service 2>,<service 3>]'
onchainos agent activate --agent-id 6961 --preferred-language en-US   # only if the CLI asks for re-submission
```

Expect `approvalStatus 2` (under review, <=24h). Log the submit timestamp; do NOT poll
aggressively. Afterwards re-run `service-list` — every service ID was reassigned.

### B1.4 Record the state — SAFE, the only write in this section

After submitting, then again once the services show live:

```
.venv/bin/python -m app.mark_listed invoice-flow submitted
.venv/bin/python -m app.mark_listed sync submitted
.venv/bin/python -m app.mark_listed highland-roast submitted
# … after approval lands:
.venv/bin/python -m app.mark_listed invoice-flow listed
.venv/bin/python -m app.mark_listed sync listed
.venv/bin/python -m app.mark_listed highland-roast listed
```

**Who runs what:** B1.1, B1.2 and B1.4 are ordinary local commands — no chain, no
funds, and B1.4 only writes Tilla's own DB. **B1.3 is the only on-chain action in this
section and is OWNER-RUN by hand**, after approving the exact service JSON text. No
build agent, test, or workflow ever runs it.

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

### Rating write-back (automatic, local, no transport)

Every SETTLED paid hire also writes one append-only `hire.rating` `event_log` row
(`app.warden_hire.record_rating`): the agent id (#3808), the score, whether the
verdict was actionable, the hire's wall-clock latency, the amount, and the settle tx
it is evidence for. The score is 5 for an actionable verdict inside the latency
budget, 3 if it was slow, 1 if the settled hire returned nothing Tilla could act on.
Whether the verdict was *correct* is deliberately not scored — Tilla cannot check it.

The write-back is fail-open by design: it runs after the verdict is already decided
and can never change, block, or slow a screening result.

**No rating is submitted anywhere.** There is no documented OKX / OnchainOS rating
endpoint or `onchainos` subcommand, and ERC-8004 is read-only from this app, so the
submission half is a DRY RUN only: setting `TILLA_RATE_HIRES=1` logs the exact payload
Tilla *would* send and sends nothing. Do not wire a transport until the real request
shape is verified against OKX's published surface.

To read the ratings back:

```
sqlite3 /opt/tilla/tilla.db "SELECT ts, data FROM event_log WHERE event='hire.rating' ORDER BY id;"
```

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
- (STEP B1) The first three store listings — approve the three slugs and the exact
  service JSON text `describe` produced. B1.3 is the on-chain call.
- (STEP C) Whether to run the single Option B per-store ASP experiment.
- (STEP D) Funding the payer wallet + flipping `TILLA_WARDEN_PAID` (Tilla spends
  USDT on Warden hires).
- (STEP E) Marking stores listed (follows A/B approval).

## Out of scope (per BUILD.md M10)

XMTP task-board participation / negotiation / ratings — the dispatch runtime is not
provisioned, so these stay out of scope and out of claims.

Submitting a rating to an external reputation graph is likewise out of claims: the
`hire.rating` rows are LOCAL evidence, and `TILLA_RATE_HIRES=1` is a dry run. No
rating has ever left this machine, and none can until a real submission surface is
verified.
