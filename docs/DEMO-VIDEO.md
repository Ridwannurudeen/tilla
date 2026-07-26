# Tilla — demo video script + shot list (≤90s)

> **STATUS: DRAFT for the owner to record.** Nothing here has been recorded or submitted.
> Every URL, button label and status code below was probed against production on 2026-07-26.
> Two shots spend real USDT0 — they are marked. Read "Before you hit record" first.

Target length **90 seconds**. Six beats. Shots 5 and 6 are the ones judges have not seen
from anyone else, so if you run long, cut from shots 2–4, never from 5–6.

---

## Before you hit record

**Have open, in this tab order** (so you never fumble for a URL on camera):

1. `https://tilla.gudman.xyz/`
2. `https://tilla.gudman.xyz/dashboard`
3. `https://tilla.gudman.xyz/s/highland-roast/`
4. A terminal, already `cd`'d somewhere neutral, font size bumped
5. The OKX marketplace page for agent **#6961**
6. `https://www.oklink.com/x-layer/evm/tx/<hash>` — build the hash by joining the two halves
   in `docs/PROOF-onchain.md` §11 ("Tilla HIRES an agent"). Verified 2026-07-26: receipt
   `status 0x1`, block **66208040**. Load the page *before* recording; OKLink is slow cold.

**Wallet:** shot 2 needs a browser wallet connected to X Layer (chain 196) holding **≥0.05 USDT0**
plus a little OKB for gas — that is the real create-store fee, charged on-chain.

**Do not let these on screen:** any terminal showing `/opt/tilla/.env`, `/root/*.env`,
`~/.okx-agent-task`, private keys, or the Anthropic key. Clear scrollback before you record the
terminal shot. Blur or crop your wallet balance if you'd rather not show it.

**Recording:** 1080p, browser at ~110% zoom so text reads on a phone. No system audio needed —
voice-over or on-screen captions both work. Captions are safer if you'd rather not narrate live.

---

## The shot list

### Shot 1 — Hook (0:00 → 0:08)

| | |
|---|---|
| **Screen** | `https://tilla.gudman.xyz/` — hero in frame |
| **Action** | Land on the page. One slow scroll to "One line in. A live store out.", then back up. |
| **On screen reads** | *"Describe it once. Sell it to anyone — human or machine."* |

> **Say:** "Tilla turns one sentence about what you sell into a real crypto storefront — one that
> both people and AI agents can buy from."

---

### Shot 2 — Build a store, live (0:08 → 0:32) · **spends 0.05 USDT0**

| | |
|---|---|
| **Screen** | Click **Create your store** → lands on `/dashboard` |
| **Action** | **Connect wallet** → sign → **Create a store** tab → type into the "Describe your store in one line" box → pick a Theme → **Create my store** |
| **Type this** | `single-origin coffee beans, roasted to order` (it's the field's own placeholder — reads honest, and the theme output is good) |
| **Then** | The new store URL appears. Click it. Let the storefront render on camera. |

> **Say:** "Connect a wallet, describe the product, pick a look. Tilla writes the brand, builds the
> store, and deploys it to a live URL. That's the whole setup."

**If you'd rather not spend the fee on camera:** skip the click on **Create my store**, and cut
straight to `https://tilla.gudman.xyz/s/highland-roast/` (tab 3) — a store Tilla actually built.
Say "here's one it built earlier." Honest, and it saves ~6 seconds.

---

### Shot 3 — A human buys (0:32 → 0:45)

| | |
|---|---|
| **Screen** | The storefront from shot 2, or `/s/highland-roast/` |
| **Action** | Scroll the product, hit the buy control, show the USDT0 amount and the pay-to address. |

> **Say:** "The customer pays USDT0 on X Layer, straight to the merchant's own wallet. Tilla
> verifies the payment on-chain and delivers the product automatically. Tilla never holds the money."

Don't complete a purchase here unless you want a second on-chain spend — showing the checkout
and the amount is enough, and the settled receipts come in shot 6.

---

### Shot 4 — The same store, for machines (0:45 → 0:58)

| | |
|---|---|
| **Screen** | Terminal |
| **Run** | `curl -i https://tilla.gudman.xyz/create-store` |
| **Shows** | **HTTP/2 402** with the x402 challenge — scheme `exact`, network `eip155:196`, 50000 micro-USDT0 |

> **Say:** "Every Tilla store is dual-sided. To a person it's a website. To an agent it's a paid
> service — this is Tilla's own endpoint answering with a real x402 payment challenge. Same store,
> two kinds of buyer."

Verified live 2026-07-26: `/create-store` returns 402.

---

### Shot 5 — It's listed on OKX (0:58 → 1:10)

| | |
|---|---|
| **Screen** | OKX marketplace, agent **#6961** |
| **Action** | Scroll the service list so all six are visible. |
| **Shows** | 3 platform services (create 0.05 / upgrade 0.03 / add-product 0.01) + 3 Tilla-built storefronts listed as their own buyable services |

> **Say:** "Tilla is a registered ASP on OKX — and the stores it builds get listed as services too.
> Every store it makes becomes new supply inside OKX's own marketplace."

---

### Shot 6 — Tilla *buys* (1:10 → 1:25)

This is the beat almost nobody else has. Give it room.

| | |
|---|---|
| **Screen** | The pre-loaded OKLink tx page (tab 6) |
| **Shows** | A settled 0.1 USDT0 transfer, block 66208040, status success |

> **Say:** "And Tilla doesn't just sell. Before a store goes live, it hires another agent to
> security-screen the content and pays for it over x402 — settled on X Layer. Tilla is a customer
> in the agent economy, not just a vendor."

---

### Shot 7 — Close (1:25 → 1:30)

| | |
|---|---|
| **Screen** | Back to `https://tilla.gudman.xyz/`, URL bar readable |

> **Say:** "Tilla. The commerce layer for the one-person company. Built for OKX AI."

Hold the URL for a full two seconds before you cut.

---

## If you have more than 90 seconds

Optional beats, in the order I'd add them back:

1. **Custom domains** — `https://shop.gudman.xyz/` (200, valid TLS, its own domain) — a merchant
   store on the merchant's own domain, not a Tilla subdomain. Strong, and cheap to show.
2. **The receipt** — `https://tilla.gudman.xyz/receipt-demo.html` — the EAS attestation receipt
   printing for a purchase.
3. **Marketplace** — `https://tilla.gudman.xyz/marketplace.html` — the public index of live stores.
4. **The guard that says no** — the review code refusing to rate an agent that shares Tilla's owner
   wallet. Hard to film, but it's the honesty beat from X thread post 5d.

## What NOT to claim on camera

- Don't say the metered/MPP channel is "closed" or "settled in full" — the close is still in
  flight. `docs/PROOF-onchain.md` §10 states it precisely; the video is safest not going near it.
- Don't call any of the on-chain proofs organic demand. They are self-funded arm's-length tests
  and the proof log labels every one of them that way.
- Don't quote a store count or sales figure from memory — if you want a number on screen, read it
  off `/marketplace.html` while recording.

## After recording

1. Trim to ≤90s, export 1080p MP4.
2. Upload (YouTube unlisted is fine) and paste the link into `docs/SUBMISSION.md`:
   Post 1 of the X thread, and the "Demo video link" row of the form table.
3. Post the X thread, then paste the thread URL into the form's "X post link" row.
4. Submit the form — **owner only**, and only once you've reviewed both drafts.
