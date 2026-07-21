# M5 checkout UX — manual browser smoke

Live target: `https://tilla.gudman.xyz`. Run these against a real store page after
deploying `app/main.py`, the three themes, and `themes/_checkout.html`, then one
`systemctl restart tilla-api` (lifespan `rerender_stores()` repaints every live
store, including the two legacy demo stores, so page JS + API shape change in the
same restart window).

The one financial step — a funded on-chain transfer — is the **user's** step and
is never executed by the agent. It is called out explicitly below.

## 0. Pre-flight (server-side, no funds)

- `curl -s https://tilla.gudman.xyz/health` -> `{"ok": true, ...}`.
- Unauth-leak check on any already-delivered file/license order:
  `curl -s https://tilla.gudman.xyz/api/checkout/<cid>` -> response has **no**
  `download_url`, **no** `license_key`; `delivery` is the neutral message
  `"Paid - sign with your purchase wallet to claim your delivery."` plus
  `kind` + `claim: true`. A legacy no-deliverable order still returns its
  `store.delivery` text verbatim (no `kind`/`claim`).
- Calldata parity: the browser's `buildTransferData(payTo, micro)` must reproduce
  `0xa9059cbb` + `pad32(pay_to)` + `pad32(micro)`. The exact expected string for
  `pay_to = 0x779ded0c9e1022225f8e0630b35a9b54be713736`, `micro = 9004999` is
  pinned in `tests/test_api.py::test_erc20_transfer_calldata_parity_vector`
  (selector `a9059cbb`, then to-word `... 779ded0c 9e102222 5f8e0630 b35a9b54
  be713736`, then amount-word `... 008967c7`).

## 1. Setup a test store with a file deliverable

Via `curl` with the store manage key (returned once at create-store):

```
curl -s -X POST https://tilla.gudman.xyz/api/stores/<test-store>/deliverable \
  -H "Authorization: Bearer <manage_key>" \
  -F file=@./sample.pdf
```

Open `https://tilla.gudman.xyz/s/<test-store>/` in desktop Chrome with the OKX
extension installed.

## 2. Open checkout (no funds)

Click **Buy**. Confirm the panel shows:

- exact 6dp amount (e.g. `9.004999 USDT0`), `pay_to` address, a rendered QR;
- a ticking `Expires in mm:ss` countdown (~30:00);
- **DevTools -> Network shows ZERO external-origin requests** (CSP/inline check —
  the QR is drawn from an inline encoder, no CDN).

## 3. Copy fields

- **Copy amount** -> clipboard holds the full 6dp string; button flips to `Copied`
  for 1.5s.
- **Copy address** -> clipboard holds the `0x...` address; button flips to `Copied`.

## 4. Wallet connect + chain switch (no funds yet)

Set OKX to **Ethereum mainnet** first, then click **Pay with wallet**:

- connect prompt -> approve;
- chain-switch prompt to **X Layer (0xc4)** appears (or add-chain then switch) ->
  approve; wallet lands on X Layer.

Then **reject** the transfer in the wallet: status shows
`Transaction canceled - retry or pay manually below.`, the pay button re-enables,
and the QR/copy fallback stays visible.

## 5. USER STEP — funded purchase (arm's-length wallet, small amount)

> The user executes this; the agent never does. It moves real funds.

Click **Pay with wallet** -> approve the transfer. The page immediately shows
`Transaction sent - confirming on X Layer...` and (in the receipt, once paid) an
OKLink link. Within seconds status moves to `detected` (tx fast-path) then to the
delivered state. The receipt block shows store/item, order id, exact amount, network,
and the OKLink tx link, which opens the real transaction.

## 6. Claim the deliverable

Click **Claim your purchase** -> OKX `personal_sign` prompt shows the "Tilla sign-in"
message -> approve -> a **Download** link appears -> the file downloads with the
correct filename.

- **Negative claim:** repeat with a different OKX account ->
  `This wallet didn't pay for this order - sign with the paying wallet.`, no goods.

## 7. Unauth leak re-check (post-delivery)

- `curl -s https://tilla.gudman.xyz/api/checkout/<cid>` -> still no
  `download_url`/`license_key`, neutral `delivery`.
- Run the wallet session flow (`/api/auth/nonce` -> sign -> `/api/auth/verify` ->
  `/api/library` with `Authorization: Bearer <session>`) -> the library item carries
  the full goods (`download_url`/`license_key`/`delivery` payload).

## 8. Mobile / no-wallet

Open the store in a browser with no injected provider -> the **Pay with wallet**
button is absent; QR + copy fallback shown. Scan the QR with OKX mobile and **log
only** whether it prefills recipient/amount (EIP-681) — input to spike 7.5, no
prefill claim is made.

## 9. Expiry

Open a checkout and wait out the TTL (or use a short-TTL test store) -> the countdown
hits 0, polling stops, `Order expired - start a new checkout.` shows, and the Buy
button re-enables.

## 10. Legacy store regression

Run a checkout on a legacy demo store (e.g. `/s/invoice-flow/`) -> the panel renders
in that store's theme; the delivered state still shows the legacy text delivery
verbatim (proving the gating did not touch no-deliverable stores).

---

## Automated pre-verification already done (build-time)

- **QR encoder**: byte-identical to the reference `qrcode` npm lib across 18 inputs
  (incl. the real EIP-681 URI) and round-trip-decoded to the exact URI by an
  independent decoder (jsQR). Result for the sample URI: version 8, mask 2, 49x49.
- **In a real browser** (Chrome): the partial parses with no console errors;
  `buildTransferData` reproduces the pinned calldata via BigInt; `formatAmount`
  yields the exact 6dp string; the QR draws to canvas; with no provider the Pay
  button is hidden and the manual fallback shows; the countdown ticks; receipt
  (with OKLink), legacy-text delivery, and file/license/text claim rendering all
  work; the copy label flips to `Copied` and reverts.
- **Wallet flow** (mock provider): EIP-6963 okx preference; happy send builds the
  correct `to`/`value`/`data`; chain 4902 -> addChain -> switch-retry -> send; user
  reject (4001) -> "Transaction canceled" + button re-enabled.
- **pytest**: full suite green including the M5 gating, amount_micro/expires_at-Z,
  library delivery payload, redeliver payload, and the calldata parity vector.
