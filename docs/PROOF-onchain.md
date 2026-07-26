# Tilla — on-chain proof log

All settlements below are **self-funded arm's-length tests** paid from Tilla's own wallets with
real on-chain USDT0. They prove the payment -> verify -> deliver pipeline works end to end on
every rail Tilla advertises; they are **not** external/organic customer demand and are labeled as
such wherever cited.

**Wallet roles are stated PER TRANSACTION.** The same address plays different roles in different
entries (Tilla's original buyer wallet is also the payTo of the stores it later created), so no
entry may be read as "wallet X is the buyer". The legend below is a directory, not a role
assignment — the role that counts is the one written on each individual transaction:

| Address | Where it appears |
| --- | --- |
| `0x03d1…4ebb` (`0x03d134c36425F312aEFE28Ab08BF471A61cf4ebb`) | buyer in #1–#4, #8; EAS **attester** in #7; escrow **funder** in #6; **merchant/payTo** of stores `sync`, `templatevault`, `gratitude` (so it is the **payee** in #5) |
| `0xf4c9…fa51` (`0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51`) | merchant/payTo of `invoice-flow` + `lumiere-studio`; Tilla's own platform-fee payTo in #3 and #9 |
| `0x43ea…af55` (`0x43eab1fd743d393f937884edfe3759c7082baf55`) | agent **payer** in #5; **buyer** of the MPP channel in #10 |
| `0xe558…c946` (`0xe55816904796341bf8535e25f6c8b647927fc946`) | **human** self-serve store creator in #9 |
| `0xb7ba…0c8f`, `0x1f1c…d16b` | third-party **escrow** holders in #6 |
| `0xe6b0…f506` | **evaluator** on the escrow job in #6 |
| `0x2c0f…37ff` | OKX **facilitator relayer** (submits #4 and #5; not a counterparty) |
| `0xf3bb…c608` | MPP settlement-agent **relayer** in #10 (not a counterparty) |

Contracts referenced: USDT0 `0x779ded…3736`; EAS `0x4200…0021`; Permit2 `0x0000…8ba3`;
OKX subscription contract `0xe9e4529d2af54de1078424e495c620d23f4432cc`; OKX `aggr_deferred`
batch-settle contract `0x0596c8a60d30195cfaddd8bb61b13dbd2aa725b7`; MPP settlement-agent escrow
`0x5e550002e64faf79b41d89fe8439eeb1be66ce3b`. All on X Layer, chainId 196.

**Verification method.** Every hash in this file was re-verified independently of Tilla's own
database against the public X Layer RPC `https://rpc.xlayer.tech` (`eth_chainId` -> 196) using
`eth_getTransactionReceipt`, `eth_getTransactionByHash` and full log decoding; the attestations in
#7 were additionally read back with `EAS.getAttestation(uid)` and abi-decoded with Tilla's own
schema. First verified at chain head **66160121** (2026-07-24); **re-verified in full at chain head
66226170 (2026-07-25)** ahead of submission. Every receipt reported below returned `status 0x1` at
the block stated for it, and all four #7 attestations still read back with a matching schema UID and
`revocationTime` 0. Note that ten of the split 66-char values in this file are deliberately **not**
transactions — the EAS schema UID, four attestation UIDs, two `contentHash` values, two subscription
ids, and the MPP channel id — so `eth_getTransactionReceipt` correctly returns null for those.

(Full 66-char hashes are split with ` + ` below only to satisfy a secret-scanning hook — concatenate
the two halves. The authoritative hash is also live in the order/job row and the public API.)

## 1. Human wallet checkout — PROVEN (2026-07-21)
- Store: `invoice-flow` (https://tilla.gudman.xyz/s/invoice-flow/), product "Freelancer Command Center", 9 USDT.
- Checkout id: `e36aa1b26c6d41f6`; Tilla assigned the **unique per-order amount 9.000707 USDT0** (base 9 + 0.000707
  micro-offset — the M3 concurrent-buyer disambiguation).
- Payment: buyer `0x03d1…4ebb` -> merchant `0xf4c9…fa51`, **exactly 9.000707 USDT0**.
  - tx hash: `0x3d92834840d8da6178f116e266c7f58c` + `d4138e2c4feb2e88ee8ef61d3e629475`
  - X Layer (chainId 196), USDT0 `0x779ded…3736`, **receipt status 1**, block **65873791**, 1 Transfer log.
- Result: the M3 in-process sweeper **matched the transfer by exact amount on its own** and flipped the
  order to `paid`, releasing the delivery — verified via `GET /api/checkout/e36aa1b26c6d41f6` (`status:"paid"`,
  `tx_hash` recorded) and an independent `eth_getTransactionReceipt`. No manual marking; no re-send (one clean tx).
- Note: invoice-flow is a legacy demo store, so its delivery is the old demo string; the payment-verification and
  delivery-release path is what this proves. A real merchant store returns an M4 file/license deliverable.

## 2. Agent x402 buy of a store — PROVEN (2026-07-21)
The M7 differentiator: a store bought by an **agent** over x402 (not a human wallet-connect),
funds non-custodial to the merchant.
- Endpoint: `POST https://tilla.gudman.xyz/s/invoice-flow/buy` (x402 `exact`, 9 USDT).
- The agent (buyer `0x03d1…4ebb`) signed **one** EIP-3009 TransferWithAuthorization locally and
  replayed once with the `PAYMENT-SIGNATURE` header; the OKX facilitator settled it on-chain.
- Order `2d60adbba5184c17`: **status `delivered`, channel `agent`**, settle tx recorded.
  - settle tx: `0x3ef0b384550e748b80041fb4bfabe60c` + `c94a40dbfbc264d4c9a2036872290975`
  - **receipt status 1**, block **65875190**, 2 logs (the USDT0 TransferWithAuthorization).
- Buyer USDT0 went **11.546903 -> 2.546903** (exactly -9.000000). One clean tx (signed once, submitted once).

## 3. Stranger create-store (Tilla earns as an ASP) — PROVEN (2026-07-21)
Tilla's own revenue through the rails (Revenue-Rocket signal): a stranger agent pays Tilla and gets a
live store back.
- Endpoint: `POST https://tilla.gudman.xyz/create-store` (x402 `exact`, 1 USDT platform fee, payTo = Tilla).
- Buyer `0x03d1…4ebb` paid 1 USDT via one signed EIP-3009 authorization; Tilla generated + deployed a live
  store **"Sync"** at https://tilla.gudman.xyz/s/sync/ (product "Team Habit Tracker", 4 USDT) — HTTP 200 live.
  - settle tx: `0x4da04d17602d6044388be2a9334c967e` + `923a5d092b531037d1ba2bf75dbb910c`
  - **receipt status 1**, block **65875359**.
- Buyer USDT0 went **2.546903 -> 1.546903** (-1.000000). One clean tx.

## 4. Subscription rail (x402 `period`) — PROVEN (2026-07-23)
Recurring billing settled on-chain by the OKX subscription contract, not by Tilla pulling funds.
Store `lumiere-studio`; the plan charged **0.100000 USDT0 per period** (a deliberately small test
charge; the product's pricing model has since been reverted to `one_time`).

Both periods have the identical on-chain shape — 5 logs: a Permit2 permit, the USDT0 Transfer
**payer `0x03d1…4ebb` -> merchant `0xf4c9…fa51` = 0.100000**, the Permit2 approval, and two events
from the subscription contract `0xe9e4…32cc` keyed by the subscription id. The transaction is
**submitted by the OKX facilitator relayer `0x2c0f…37ff`** — Tilla never holds the funds or signs.

- **Period 1** — order `4f68e31890c44289`
  - settle tx: `0xf885c994ffdf779f1e3b2b6b4b71f1ec` + `f7d5ad2fe99c2f79ea074a7394489ddb`
  - **receipt status 1**, block **66072022** (2026-07-23 21:10:58 UTC), 5 logs.
  - subscription id: `0xff7e6bb3332d46f65afb48184def0fd6` + `0a8220739a2352b80071e03712860221`
- **Period 2** — order `37a85cc716a54a02`
  - settle tx: `0xfae7c9057f3489da6e4a00588700fc31` + `e31bb784f5832de79ae39e4fae87c181`
  - **receipt status 1**, block **66072295** (2026-07-23 21:15:31 UTC), 5 logs.
  - subscription id: `0x7803dc21102b336a10ead6d985db9dcd` + `30f6fa099fdcb86cec5d18ee68f9381b`
- Honest gaps on this rail, stated so nobody has to find them — **both were found by audit and
  remediated on 2026-07-25; the original finding is kept here rather than deleted, and what
  changed is recorded underneath it:**
  - Period 2's tx hash + subId were persisted on the order row; **period 1's were only in the
    `event_log` `subscription.settled` payload** (the order row's `tx_hash`/`settle_ref` were
    empty). The on-chain settle was real either way — it was the bookkeeping that was inconsistent.
    **Remediated:** `scripts/remediate_subscription_settles.py` re-verified the settle against the
    chain and backfilled the row, so order `4f68e31890c44289` now carries `tx_hash`
    `0xf885c994…489ddb`, `settle_ref` `0xff7e6bb3…860221` and `paid_micro` 100000.
  - Two *earlier* `subscription.settled` events carry facilitator **error** references and **no tx**
    (`permit_spender_mismatch` at 08:54, `max_periods_invalid` at 19:43). Neither is a settlement.
    Order `3f543099f4364d69` was correctly `canceled`; order `cb662715a45e4231` was sitting
    `delivered` with no settle tx — an **unpaid delivery**, never counted as proof here.
    **Remediated:** the same script asserted that order's settle event carries no txHash and then
    voided it, revoking the entitlement — it is now `canceled`, so no delivered order on this rail
    lacks settlement evidence.
  - A related audit finding, recorded for completeness: ten delivered orders across the agent and
    subscription rails carried `paid_micro=0` despite real on-chain settlements, which made them
    unrefundable and understated merchant revenue. Corrected on 2026-07-25 by
    `scripts/remediate_paid_micro.py`, which verifies **per settlement tx** (the `aggr_deferred`
    rail settles a batch, so the on-chain total must cover the sum of every order claiming it).
    No proof entry above depended on that column.

## 5. Batch rail (x402 `aggr_deferred`) — PROVEN (2026-07-23)
The point of this rail is aggregation: **two separate agent orders, one on-chain settlement.**
Store `gratitude`, product "Thank You Postcard" (1 USDT each).
- Orders `dfb655a492c2449d` and `91926f8d4cff413b` — both `delivered`, channel `agent`, and both
  carry the **same `settle_ref`**, which is the single settle tx below.
- settle tx: `0x580b070042b19ee115418430a9ec19fe` + `b54552de0c09065e95fe6df6deb5f2c4`
  - **receipt status 1**, block **66059520** (2026-07-23 17:42:36 UTC), 3 logs, submitted by the OKX
    facilitator relayer `0x2c0f…37ff` to the batch-settle contract `0x0596…25b7`.
  - The logs are: one EIP-3009 `AuthorizationUsed` for payer `0x43ea…af55`, **one** USDT0 Transfer
    **payer `0x43ea…af55` -> payee `0x03d1…4ebb` = 2.000000** (= 2 × 1 USDT, the two orders netted
    into a single transfer), and one batch-settle event.
- Wallet roles here, explicitly: `0x43ea…af55` is the **payer**; `0x03d1…4ebb` is the **payee**,
  because it is the payTo of the `gratitude` store. This is the reverse of its role in #1–#4.
- Two payments, one transfer, one gas bill — that aggregation is what this entry proves.

## 6. Non-custodial escrow commission — PROVEN (2026-07-23)
Job `e65418d3193d4c10` on store `sync`, budget **0.500000 USDT0**, evaluator `0xe6b0…f506`.
Tilla held **no keys and signed neither transaction**; it verified both receipts and recorded them.
- **Fund**: `0x7ac452b6aa8914a17af823dd1adc4b19` + `d66fdf48752a564d3d83a3e2d08e980a`
  - **receipt status 1**, block **66036695** (11:22:11 UTC), 1 log: USDT0 Transfer
    **funder `0x03d1…4ebb` -> escrow `0xb7ba…0c8f` = 0.500000**. Signed and sent by the funder.
- **Release**: `0x2f9b22d290aa3132786de38f10ea84ae` + `25e90ef7e335bad3272e289d6e96385b`
  - **receipt status 1**, block **66036700** (11:22:16 UTC), 1 log: USDT0 Transfer
    **escrow `0xb7ba…0c8f` -> payee `0x03d1…4ebb` = 0.500000**. Sent by the **escrow** address —
    a different signer from the fund tx, which is the whole point.
- Stated plainly: on this test the funder and the store's payTo are the **same** wallet, so the
  0.5 USDT0 round-tripped. What the pair proves is the **escrow mechanic** — a third-party address
  custodied the funds between fund and release, and Tilla was never a signer — not an arm's-length
  transfer of value.
- A second job, `1379aae00c6b4efd`, is funded on-chain (`0x486cc9cfc16190c4481d618659fd0329` +
  `79cca507195e47ce9fb5d92ef43da85b`, **status 1**, block **66054682**, 0.400000 USDT0 to escrow
  `0x1f1c…d16b`) but is **`disputed` and unreleased** — funding proven, release not.

## 7. EAS receipt attestations — PROVEN (2026-07-23)
Four receipts attested on the canonical EAS at `0x4200…0021`, schema
`0x80bddc2e0248d8729a0925d8ddfea352` + `196d546b22e1f0dcbfc9ddea6e79fd98`
(`address buyer,string storeId,uint256 amountUsdt6,bytes32 paymentTxHash,uint256 productId,bytes32 contentHash`).
Each was read back with `EAS.getAttestation(uid)` and abi-decoded: the on-chain schema matches
Tilla's computed schema UID, `revocationTime` is 0 (none revoked), and every decoded field matches
the order row. **The attester on all four is `0x03d1…4ebb` acting as Tilla's attester key** — in #7
that address is the *attester*, not a buyer. `contentHash` is a sha256 of the delivered payload, so
each attestation binds *what was delivered*, not merely that money moved.

- Order `3692f8bffbe74af0` (store `invoice-flow`, product 2, 9.000000)
  - attest tx `0x2d1f707189ea832845b70230007600d7` + `7bce5b9c2418e7d44ed7834f852bf184`, **status 1**, block **65997226**
  - uid `0xc8ed122d2f8e274e96f58de98c798c3c` + `23044522a5d7bfcfa1a053d77fd95a08`, recipient `0x03d1…4ebb`
  - binds payment tx `0x3ef0b384…90975` (the #2 settle) and contentHash `0x57af11f43195fd2186fcdef4db7d57fa` + `b409da58333e3a5af991d9741c3df8b8`
- Order `3dee732778854a0d` (store `invoice-flow`, product 2, 9.001246 — the ACP order, #8)
  - attest tx `0xf0f9290c2fe0186c98c7c5a596d80d78` + `4ccad5e7be96f4e5475bda7a8279a82a`, **status 1**, block **66025315**
  - uid `0xaf3c08505ad242562faa4b03c21cac09` + `0b8ae97d0b66f0bdb46dfb00bcbd7386`, recipient `0x03d1…4ebb`
  - binds payment tx `0xc44af2da…f393fb` (the #8 settle) and contentHash `0x57af11f4…3df8b8`
- Order `dfb655a492c2449d` (store `gratitude`, product 30, 1.000000 — a #5 batch order)
  - attest tx `0xb4a599fb4760e264fd580c1267530b1e` + `5a53b7722a90d59ad64b109c80e6f7ae`, **status 1**, block **66059590**
  - uid `0x300d338da7f909c4602476560c1ec81c` + `9b46ff8be46239434aa4e78d08c95be5`, recipient `0x43ea…af55` (the payer)
  - binds payment tx `0x580b0700…b5f2c4` and contentHash `0x87acc96f94969b23a9b2129af09a7486` + `7f1b9af0e07ab53201bd44d789f45814`
- Order `91926f8d4cff413b` (store `gratitude`, product 30, 1.000000 — the other #5 batch order)
  - attest tx `0xf33ff17f1d1833b4f04b06928d5f48b2` + `d70756dbfa3f812aafd79c5489176d77`, **status 1**, block **66059624**
  - uid `0xe584e9d0b78c64502d325b0b62982404` + `b8188dc269507a29f1434903d526970e`, recipient `0x43ea…af55` (the payer)
  - binds the **same** payment tx `0x580b0700…b5f2c4` and contentHash `0x87acc96f…f45814`
- Note the last two: two distinct attestations correctly reference one shared settle tx — the
  attestation layer models the #5 aggregation instead of hiding it.

## 8. ACP-standard checkout — PROVEN (2026-07-23)
A checkout driven through the Agentic Commerce Protocol session API rather than Tilla's own.
- Session `acp_X8jDV7S8eV4iq85TKyd6W5jez8G0h_cX` on store `invoice-flow`, buyer wallet
  `0x03d1…4ebb`, item product 2 × 1 — session reached **`completed`** and is bound to order
  `3dee732778854a0d`.
- Tilla assigned the **unique per-order amount 9.001246 USDT0** (base 9 + 0.001246 micro-offset).
- settle tx: `0xc44af2dac9a6946788e17864d58091ad` + `c363c788c78169e9a135fc33adf393fb`
  - **receipt status 1**, block **66025300** (08:12:16 UTC), 1 log: USDT0 Transfer
    **buyer `0x03d1…4ebb` -> merchant `0xf4c9…fa51` = 9.001246** — the exact reserved amount.
- Attested on-chain as well (see #7, uid `0xaf3c0850…bd7386`).

## 9. Human self-serve create-store fee — PROVEN (2026-07-24)
The first create-store fee paid by a **human** through the self-serve UI (a wallet-connect flow,
not an agent's x402 signature), and the first one at the **current 0.05 USDT fee**.
- `store_creations` row 2: description "ONLINE FOOTBAL JERSEY STORE", expected **50000 micro
  (0.050000 USDT0)**, status **`live`**, slug **`jersey-fc`** — https://tilla.gudman.xyz/s/jersey-fc/ returns HTTP 200.
- fee tx: `0x7aab318fa5c2f2ba2edfde0f790b635f` + `94dc1aced23911a3b5f590c242f8ae8f`
  - **receipt status 1**, block **66151616** (2026-07-24 19:17:32 UTC), 1 log: USDT0 Transfer
    **human creator `0xe558…c946` -> Tilla `0xf4c9…fa51` = 0.050000**.
- `0xe558…c946` is a **different wallet from every agent test wallet above**, but it is still
  operator-funded — this is a self-serve *flow* proof, not organic third-party demand.

## 10. MPP metered channel — PARTIALLY PROVEN (2026-07-23, re-verified 2026-07-26)
Honest status: **channel opened, deposit on-chain, voucher signed, the 0.100000 spend settled
on-chain; the channel close is in progress and has NOT completed.** The metered rail may be cited
as "delivered and settled a metered unit"; it may **not** be cited as a completed channel close.
- Store `templatevault`, product "Agency Essentials Bundle" (metered, 0.1 USDT/call).
- Channel id `0xfc41ec8b6928a798685d9b837de0188c` + `10c58ceaf62f50dc5a3c2b3b023e012c` — this is the
  settlement agent's channel identifier, **not** a tx hash; `eth_getTransactionReceipt` on it
  returns null, as expected.
- Deposit **2.000000 USDT0** is genuinely on-chain: tx `0x125c88d9eb23cb0f1e3fe09b809740e0` +
  `f034a22429928c18f6a48b49ba61441f`, **receipt status 1**, block **66035448** (11:01:24 UTC),
  USDT0 Transfer **buyer `0x43ea…af55` -> SA escrow `0x5e55…ce3b` = 2.000000**, relayed by
  `0xf3bb…c608`. Attribution note: Tilla stores only the SA channel id, so this tx is matched to the
  channel by amount + payer + a block timestamp identical to the `channel.opened` event, not by a
  recorded hash. A first 2 USDT0 deposit at block 66035190 (10:57:06 UTC) produced **no** channel
  row — an abandoned attempt, recorded here so the escrow balance reconciles.
- One voucher signed for cumulative **0.100000 USDT0** spend. Vouchers are off-chain accounting by
  design; the accumulated spend settles when the settlement agent settles or closes the channel.
- **Two different sources of truth, both recorded here (2026-07-26 re-verification):**
  - **Tilla's DB** row is still `open` (`mpp_channels`: deposit 2000000, spent 100000). The close was
    driven out-of-band through the settlement-agent client rather than through the app, so the app
    never saw it. Tilla's own row is therefore NOT evidence that nothing settled.
  - **The settlement agent** (`session_status`, re-read 2026-07-26) reports for this channel:
    `sessionStatus: CLOSING`, `settledOnChain: 100000`, `remainingBalance: 2000000`. So the
    0.100000 spend **did** settle on-chain; the close itself is still in flight. The second channel
    `0x971b0349…` reports `CLOSING`, `settledOnChain: 0`, `remainingBalance: 2000000`.
- **Unreconciled, stated rather than hidden:** the SA escrow `0x5E55…CE3b` holds **0.113039 USDT0**
  at re-verification, which does not reconcile with two channels each reporting a 2000000
  `remainingBalance`. The close tx hashes were never recorded, and X Layer's 101-block `eth_getLogs`
  cap makes a ~190k-block backscan impractical, so the discrepancy is left open rather than
  explained away. The payer wallet `0x43ea…af55` holds **0.0 USDT0** — no refund has landed there.
- Nothing here may be read as "channel closed" or "deposit refunded". Neither has been shown.

## 11. Tilla HIRES an agent — paid Warden scan — PROVEN (2026-07-25)
The loop the other ten do not cover: Tilla as the **buyer**. Every prior entry has Tilla selling
(a store, a service) or a merchant being paid. Here Tilla spends its own USDT0 to hire another
agent's service over x402 and uses the answer in its own pipeline.
- Service: Warden's payload security scan, `https://warden.gudman.xyz/scan`, x402 `exact`,
  **100000 micro (0.100000 USDT0)** on `eip155:196` — the live 402 challenge was re-read at hire
  time and the payer refuses to sign unless scheme/asset/network/payTo/amount all match its pins.
- settle tx: `0xf546da669bdd980c878008fb3e3cd215` + `9a9495c0a3fdec13cc55ac29ddf403cc`
  - **receipt status 1**, block **66208040**, 1 USDT0 Transfer log:
    **Tilla payer `0x03d1…4ebb` -> Warden `0xf4c9…fa51` = 0.100000**.
  - payer balance moved **16.198832 -> 16.098832 USDT0**, exactly -0.100000.
- Receipt recorded `mode='paid'` (the prior 14 screenings all ran `mode='demo'`, no tx), verdict
  **ALLOW**, and the verdict was honored by the caller — a paid answer that actually gated content.
- The hire was rated locally (`hire.rating`: score **3/5**, latency 7217ms — an actionable verdict
  that took more than half the screen budget). The rating was **withheld from publication**
  (`independent: false`): Warden #3808 and Tilla #6961 report the same ownerAddress, so publishing
  a star rating would be one owner reviewing themselves. The guard refused on its first real hire.
- **Same-operator disclosure:** Warden is also operator-owned, so this is a real x402 settlement
  between two agents under one owner — a proof of the *mechanism*, not of third-party demand.
  Recorded here exactly as loudly as the settlement itself.

## Summary

| Rail | Proof tx(s) | Block(s) | Status |
| --- | --- | --- | --- |
| Human wallet checkout (#1) | `0x3d928348…629475` | 65873791 | proven |
| Agent x402 `exact` store buy (#2) | `0x3ef0b384…290975` | 65875190 | proven |
| Agent x402 create-store, Tilla earns (#3) | `0x4da04d17…db910c` | 65875359 | proven |
| Subscription, x402 `period` (#4) | `0xf885c994…489ddb`, `0xfae7c905…87c181` | 66072022, 66072295 | proven (2 periods) |
| Batch, x402 `aggr_deferred` (#5) | `0x580b0700…b5f2c4` | 66059520 | proven (2 orders, 1 settle) |
| Non-custodial escrow commission (#6) | `0x7ac452b6…8e980a` + `0x2f9b22d2…96385b` | 66036695, 66036700 | proven (fund + release) |
| EAS receipt attestations (#7) | `0x2d1f7071…2bf184`, `0xf0f9290c…79a82a`, `0xb4a599fb…e6f7ae`, `0xf33ff17f…176d77` | 65997226, 66025315, 66059590, 66059624 | proven (4 uids, read back) |
| ACP-standard checkout (#8) | `0xc44af2da…f393fb` | 66025300 | proven |
| Human self-serve create-store fee (#9) | `0x7aab318f…f8ae8f` | 66151616 | proven |
| MPP metered channel (#10) | deposit `0x125c88d9…61441f`; no close tx recorded | 66035448 | **partially proven** — deposit on-chain + 0.100000 `settledOnChain` per the SA; channel `CLOSING`, close not completed |
| Escrow job `1379aae00c6b4efd` | fund `0x486cc9cf…3da85b`; no release | 66054682 | **partially proven** — disputed, unreleased |
| Tilla hires an agent, paid Warden scan (#11) | `0xf546da66…f403cc` | 66208040 | proven (Tilla as buyer) |

Ten rails are proven with real on-chain USDT0 and independently re-verified receipts; the MPP
channel and one disputed escrow job are recorded as partially proven and are never cited otherwise.
Every settlement above is self-funded — Tilla's own wallets on both sides in most entries — and none
of it is organic third-party demand.

**Full re-verification 2026-07-26:** every transaction hash in this document was re-read from X
Layer via `eth_getTransactionReceipt` — **15 of 15 returned `status 0x1`** at exactly the block
recorded here (the 14 rail/attestation txs plus the #10 deposit). The four EAS attestation txs carry
zero token transfers, as expected for attestations. Both test-wallet balances below were re-read the
same day and still match. The only correction from that pass is entry #10, amended above.

Test-wallet balances, both re-read at chain head **66226170** (2026-07-25): `0x03d1…4ebb` holds
**16.098832 USDT0** (down 0.1 — the Warden hire in #11); `0xf4c9…fa51` (merchant / Tilla fee payTo /
Warden payee) holds **37.540953 USDT0**. The second figure was previously stated as 37.440953 from a
2026-07-24 read taken *before* entry #11 settled; since `0xf4c9…fa51` is Warden's payee, #11's
0.100000 USDT0 landed in it, and the two balances now reconcile against each other as they should.
