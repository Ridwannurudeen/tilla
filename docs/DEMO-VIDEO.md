# Tilla — demo video script + shot list (≤90s)

> **STATUS: DRAFT for the owner to record.** Nothing here has been recorded or submitted.
> Every URL, button label and status code below was probed against production on 2026-07-26.
> Two shots spend real USDT0 — they are marked. Read "Before you hit record" first.

Target length **90 seconds**. Shots 5, 6 and 3b are the ones judges have not seen from anyone
else, so if you run long, cut from shots 2 and 4, never from those three.

> **Updated 2026-07-26 (later).** Two things landed after the first draft and both are worth
> screen time, so shot 3b is new and the store URLs changed:
> * **Stores now serve at their own subdomain** — `highland-roast.tilla.gudman.xyz`, valid TLS.
>   Use that on camera rather than `/s/highland-roast/`. It reads as the merchant's shop instead
>   of an entry in someone else's directory, which is the whole point of the product.
> * **Two stores from the same sentence now come out visibly different** — different layout,
>   typography and palette, from a slug-seeded design system rather than a template. That is the
>   anti-template proof no free-CSS builder can make safely, and it is shot 3b.

---

## Before you hit record

**Have open, in this tab order** (so you never fumble for a URL on camera):

1. `https://tilla.gudman.xyz/`
2. `https://tilla.gudman.xyz/dashboard`
3. `https://highland-roast.tilla.gudman.xyz/` — the store on its OWN subdomain (new; verified
   200 with valid TLS against `CN=*.tilla.gudman.xyz`)
3b. `https://lumiere-studio.tilla.gudman.xyz/` — a second store, for the side-by-side in shot 3b
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

**The 90 seconds are now fully committed** (8 / 24 / 10 / 10 / 10 / 10 / 13 / 5). Shot 2 is the
only loose one: taking its no-spend fallback saves about six seconds, which is your whole margin if
a take runs long. Do not sacrifice 3b, 5 or 6 to keep shot 2 whole.

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
straight to `https://highland-roast.tilla.gudman.xyz/` (tab 3) — a store Tilla actually built.
Say "here's one it built earlier." Honest, and it saves ~6 seconds.

---

### Shot 3 — A human buys (0:32 → 0:42)

| | |
|---|---|
| **Screen** | The storefront from shot 2, or `https://highland-roast.tilla.gudman.xyz/` |
| **Action** | Let the URL bar read for a beat — it is the merchant's own subdomain, not a path under ours. Then scroll the product, hit the buy control, show the USDT0 amount and the pay-to address. |

> **Say:** "Every store gets its own address. The customer pays USDT0 on X Layer, straight to the
> merchant's own wallet — Tilla verifies the payment on-chain, delivers automatically, and never
> holds the money."

Don't complete a purchase here unless you want a second on-chain spend — showing the checkout
and the amount is enough, and the settled receipts come in shot 6.

---

### Shot 3b — Same sentence, different store (0:42 → 0:52) · **the anti-template proof**

This is the beat that answers the obvious objection: *isn't this just three templates with the
words swapped?* Do not cut it.

| | |
|---|---|
| **Screen** | Two browser windows side by side, or a quick cut between tabs 3 and 3b |
| **Action** | Show `highland-roast.tilla.gudman.xyz` next to `lumiere-studio.tilla.gudman.xyz`. Let the difference land — different layout, different typography, different colour. |

> **Say:** "And two merchants describing the same thing don't get the same shop. Layout,
> typography and colour are all derived per store — so nobody gets a template with their name
> dropped into it."

**Why this is true, if a judge asks:** each store's design is seeded from its own slug across ten
curated design personas, four typography pairings and a palette derived from a single hue — 81
distinct structural looks measured across 4000 slugs, with the most common holding 2.5%. Colour is
computed rather than picked, with enforced contrast floors: body text at 7:1, brand colours at 3:1,
and the accent kept perceptibly distinct from both the primary and the text. It is all
server-validated enums — a merchant never gets to inject CSS. See `docs/DESIGN-DNA.md`.

*Honest framing:* say **derived per store**, not "AI designs it." The colour hue is the model's
only aesthetic input; the structure is seeded, precisely because the model kept returning the same
three answers when it was asked to choose freely.

---

### Shot 4 — The same store, for machines (0:52 → 1:02)

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

### Shot 5 — It's listed on OKX (1:02 → 1:12)

| | |
|---|---|
| **Screen** | OKX marketplace, agent **#6961** |
| **Action** | Scroll the service list so all SEVEN are visible. |
| **Shows** | Create Storefront (0.05) + 6 Tilla-built storefronts listed as their own buyable services |

> ⚠️ **Check this shot before you record it (updated 2026-07-27).** The listing is in re-review, so
> the marketplace card may read **"not listed"** — if it does, this shot will not show what the
> script describes. **Substitute:** run `onchainos agent service-list --agent-id 6961` in the
> terminal and film that output instead — it lists all seven services with their fees and endpoints
> and is true regardless of the card's state. Do not film a card that says "not listed" while
> narrating that it is listed.

> **Say:** "Tilla is a registered ASP on OKX — and the stores it builds get listed as services too.
> Every store it makes becomes new supply inside OKX's own marketplace."

---

### Shot 6 — Tilla *buys* (1:12 → 1:25)

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

## New material since the last draft (2026-07-27) — use if you have room

Two things landed that are strong on camera. Both are cuttable; neither is worth losing shot 5 or 6.

**Photography (best candidate — it is the most visible change).** Between shots 3 and 3b, put two
stores side by side: `iron-built.tilla.gudman.xyz` (real product photography) and
`focusflow.tilla.gudman.xyz` (drawn atmosphere, because it sells software).

> **Say:** "Every store gets real product photography — and Tilla refuses any picture it can't
> prove shows the thing being sold. A store selling software gets drawn atmosphere instead,
> labelled as illustration, because a photo of a real desk would be a claim about goods that
> don't exist."

Verified 2026-07-27: 26 live stores, all carrying imagery, 219 photographs. The "Some imagery is
generated illustration" line renders in the footer of the generated ones — worth a beat on screen.

**The A2A round trip (only if shot 6 feels thin).** Job `0xcbe0ce6e…a005` — an OKX.AI user task
designated to #6961 that ran connect → x402 agreement → 0.05 USDT paid → deliverable → completed,
producing the live store at `tilla.gudman.xyz/s/checkpoint/`. Say it plainly as "an agent hired
Tilla through OKX's own task rail and got a working store back." **Label it as a self-funded test —
buyer 4844 and Tilla share an owner, and the marketplace itself blocked the rating for that reason.**

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

- The metered/MPP channel *is* fully settled — closed on-chain, spend paid out, remainder refunded
  (`docs/PROOF-onchain.md` §10). You can say all four payment schemes settled. What you must not say
  is that OKX's own API agrees: it still reports the channels as `CLOSING`. The chain is right and
  the API is stale, and the proof log explains why. Safest on camera is "all four settled on-chain"
  and nothing further.
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

**Deadline: treat it as 2026-07-27 22:59 UTC**, not 23:59. The HackQuest page shows both times in
different places (see the note at the top of `docs/SUBMISSION.md`); the earlier one is the safe read.
