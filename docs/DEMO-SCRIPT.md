# Tilla — Demo Video Script (≤90s)

> ## ⚠️ HISTORICAL — SUPERSEDED. Do not record from this file.
> **Record from [`DEMO-VIDEO.md`](DEMO-VIDEO.md) instead** (retired 2026-07-26).
>
> This was the first pass at the submission video. It is stale in ways that would produce a weaker
> video: it shows only the single "Create Storefront" service at #6961 rather than the six now
> listed, and it has **neither** of the two beats that carry the submission — the six-services
> marketplace shot and the "Tilla *buys*" shot, where Tilla hires and pays another agent. `DEMO-VIDEO.md`
> names those two as the ones judges have not seen from anyone else, and says never to cut them.
>
> `DEMO-VIDEO.md` also has every surface re-probed against production on 2026-07-26, the correct
> post-close MPP language, and the corrected 22:59 UTC deadline. Kept here for history only.

OKX.AI Genesis Hackathon · deadline Jul 27, 2026 (see `DEMO-VIDEO.md` — treat the cutoff as **22:59 UTC**)

**Video title:** Tilla — One Sentence Becomes a Live Crypto Store

**Description (1 sentence):** Describe what you sell, pay a one-time 0.05 USDT0 fee, and Tilla builds, brands, and deploys a real storefront on OKX X Layer with crypto checkout — live in minutes, sellable to humans and agents alike.

*Every shot below is filmable against the live product exactly as it ships — no mockups, no staged screens.*

---

## SHOT LIST (0:00–1:30)

**[0:00–0:07] — THE PAIN**
Shot: Screen recording, fast jump-cuts across a cluttered desktop — a Stripe dashboard, a Webflow builder, a DNS settings page, a wallet integration doc, all flashing by in a blur.
Caption/VO: *"Launching a paid product used to take a weekend. Domain. Payments. Design. Code."*

**[0:07–0:12] — THE CUT**
Shot: Hard cut to black, then the Tilla landing page fades in: `tilla.gudman.xyz`. Cursor rests on the hero's primary button, **"Create your store."**
VO: *"Now it takes one sentence."*

**[0:12–0:14] — TITLE CARD**
On-screen text: **TILLA** — describe it. sell it. today.

**[0:14–0:36] — THE MAGIC: CREATING THE STORE**
Shot: Click **"Create your store"** → lands on `tilla.gudman.xyz/dashboard`. Click **"Connect wallet"**; OKX Wallet prompts for a signature (sign-in, not a payment) — approve it.
Shot: The dashboard opens on the **"Create a store"** panel. Cursor clicks into the description box.
On-screen action: Type, live, character by character:
> "single-origin coffee beans, roasted to order"
Then pick a **Theme** from the dropdown (Bold).
VO (as it types): *"Describe what you sell, pick a look..."*
Beat: click the create button. On-screen status reads **"Checking your description…"** — the safety screen runs *before* any payment is offered.
Shot: The pay step appears — **0.05 USDT0 · X Layer**. Click **"Pay with wallet"**; the wallet popup shows a single transfer of **0.05 USDT0**. Confirm.
VO: *"...pay five cents in USDT, once. Tilla's AI does the rest — brand, copy, palette, product page — generated the moment the payment verifies on-chain."*
Shot: brief on-chain verify + generation status (1–3s).

**[0:36–0:52] — THE REVEAL: A REAL LIVE STORE**
Shot: The dashboard hands back the live URL. Open it — full-screen scroll-through of the new store at `tilla.gudman.xyz/s/<your-slug>/`: branded hero, generated copy and palette, the product card, and the **"Buy now"** checkout button.
VO: *"This isn't a mockup. It's live — a real branded storefront on its own URL, with crypto checkout wired in, in under a minute."*
On-screen text overlay (bottom third): **tilla.gudman.xyz/s/&lt;your-slug&gt;**

**[0:52–1:06] — THE BUYER MOMENT**
Shot: Cursor clicks **"Buy now."** The hosted checkout opens: exact USDT0 amount, QR code, countdown. Wallet popup appears, buyer confirms the transaction.
VO: *"A buyer pays in USDT, on-chain, on X Layer..."*
Shot: Quick cut to the on-chain confirmation, then back to the page flipping itself to paid.
On-screen text: **✅ Verified on X Layer**
Beat: delivery unlocks; the receipt shows the tx with its OKLink explorer link.
On-screen text: **🔓 Delivery unlocked**
VO: *"...verified on-chain, and the product unlocks automatically. No manual fulfillment. No middleman."*

**[1:06–1:16] — AGENTS TOO**
Shot: Split-screen or quick cut to the OKX AI marketplace listing: **#6961 "Tilla"** — service "Create Storefront," 0.05 USDT via x402. Optional insert: a terminal `curl` of `POST /create-store` returning a real **HTTP 402** challenge.
VO: *"And it doesn't just sell to people. Tilla sells to agents too — an agent pays the same 0.05 USDT over x402 and gets a store back."*
On-screen text: **Listed on OKX AI — Agent #6961**

**[1:16–1:24] — THE PUNCHLINE**
Shot: Return to the Tilla landing page, zoom out.
VO: *"One person. One sentence. A store an agent can hire, and a store agents can buy from. This is the one-person company — the unlimited workforce, built in."*
On-screen text: **One person. Unlimited workforce.**

**[1:24–1:30] — CLOSE**
Shot: Final card — Tilla logo/wordmark, URL, ASP ID.
On-screen text:
> **TILLA**
> tilla.gudman.xyz
> OKX AI Agent #6961
VO (final line): *"Tilla. Describe it. It's live."*

---

## KEY ELEMENTS TO CAPTURE ON SCREEN (exact, in order)
1. `tilla.gudman.xyz` — landing page, hero button **"Create your store"**
2. `tilla.gudman.xyz/dashboard` — **"Connect wallet"** → wallet signature sign-in
3. **"Create a store"** panel: typed description (verbatim) `single-origin coffee beans, roasted to order` + Theme = Bold
4. Screening status **"Checking your description…"**, then the pay step: **0.05 USDT0 · X Layer**
5. **"Pay with wallet"** → wallet popup showing a single 0.05 USDT0 transfer → confirm
6. The new store live at `tilla.gudman.xyz/s/<your-slug>/`, with its **"Buy now"** button
7. Checkout: exact USDT0 amount + QR + countdown → wallet confirm
8. Reveal text: **✅ Verified on X Layer** / **🔓 Delivery unlocked** + receipt tx link
9. OKX AI marketplace entry: **#6961 "Tilla" — Create Storefront — 0.05 USDT (x402)**
10. Closing punchline: **One person. Unlimited workforce.**

---

## PRODUCTION NOTES
- Both purchases on screen (the 0.05 USDT0 creation fee and the buyer's checkout) are **self-funded arm's-length tests** — paid from our own second wallet, distinct from the receiving wallet. If either is cited anywhere as proof, it carries that label.
- Nothing is sped up in a way that misrepresents timing; if a clip is time-compressed, mark it on screen.
- Amounts are small and real. No simulated wallets, no fake tx hashes, no re-shot "success" screens.

---

## CAPTION / HOOK OPTIONS (pick one for the post)

1. **"I described a product in one sentence, paid five cents of USDT, and had a live store with a crypto checkout."**
2. **"This AI doesn't just chat — it builds you a real crypto storefront. Then sells itself to other agents on OKX AI."**
3. **"One person, unlimited workforce: watch an AI turn a sentence into a live, paid, on-chain store."**

**Recommended pick: #1** — it's the most concrete and leads with the demonstrable "magic moment" (sentence + a real on-chain payment → live store) rather than abstraction, which performs best as a hook before the payoff is shown.
