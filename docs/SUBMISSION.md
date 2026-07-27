# Tilla — OKX.AI Genesis Hackathon Submission Materials (DRAFT)

> **STATUS: DRAFT ONLY.** Nothing in this file has been posted or submitted anywhere.
> The owner must review, edit as needed, and personally post the X thread and
> submit the form before the deadline. **Treat the cutoff as 2026-07-27 22:59 UTC.** The HackQuest
> page is self-contradictory — it displays "Jul 27th, 23:59 UTC" as the form closing time, but states
> the submission period as "Jul 2,2026 11:00 - Jul 27,2026 22:59", and the page JSON's
> `submissionClose` also reads 22:59 (re-checked 2026-07-26; see ROADMAP §1.1). Submit by 22:59 and
> the ambiguity costs nothing; submit at 23:30 and it may cost everything.
> All facts below are restricted to what is verified live/real as of 2026-07-25 — no invented features, no fabricated metrics.
> Every URL, status code and price in this file was re-probed against production on 2026-07-25 06:2x UTC.

---

## 1. X Thread (draft) — `#OKXAI`

*Post as one thread, in order. Reply-chain each post to the previous one. Insert the demo video link into Post 1 (or a later post) once recorded — currently a placeholder.*

**Post 1 (hook)**
> I built the first store-builder on OKX AI: one sentence → a live crypto storefront that sells to humans AND agents.
>
> "I sell a Notion template for $9" → branded store → real URL → USDT checkout on X Layer → auto-delivery.
>
> 🧵 how it works
>
> Live: https://tilla.gudman.xyz/
> Demo: [≤90s video link — TBD]

**Post 2 (the problem)**
> Right now, going from "I have something to sell" to "I have a real store that accepts crypto" takes hours of setup — hosting, design, wallet integration, payment verification.
>
> Tilla collapses that to one prompt.

**Post 3 (how it works)**
> You describe what you sell. Tilla:
> 1. Generates your brand (name, tagline, palette, copy)
> 2. Deploys a real premium storefront to a live URL
> 3. Wires crypto checkout — buyer pays USDT on @XLayerOfficial, payment is verified on-chain, product delivers automatically
>
> No code. No hosting. No wallet setup.

**Post 4 (the OKX-native part)**
> The differentiator: every Tilla store is dual-sided from day one.
>
> It's a normal website a human can buy from — AND an ASP an *agent* can pay via x402.
>
> Same store, two buyers. Only possible because it's built on OKX's Agent Payments Protocol rails.

**Post 5 (proof it's real)**
> This isn't a mockup. Tilla is a registered OKX ASP — agent #6961 — and it lists
> SEVEN services on the marketplace:
>
> the Create Storefront platform service
> + 6 merchant storefronts Tilla built, each listed as its own buyable service
>
> Every store Tilla creates can become supply inside OKX's own marketplace.
>
> Live 402: 0.05 USDT0 on eip155:196 ✅
> Example store: https://tilla.gudman.xyz/s/invoice-flow/

**Post 5b (the rails aren't a diagram — they settled)**
> All four x402 schemes are live on X Layer and all four have settled on-chain, with every
> receipt in Tilla's public proof log (docs/PROOF-onchain.md):
>
> exact · aggregated/deferred (one tx settling two orders) · period subscriptions ·
> metered payment channels, full lifecycle — open, voucher, delivery, close, with the
> spend paid out and the remainder refunded
>
> Plus EAS attestation receipts and non-custodial escrow, both settled.
>
> Non-custodial throughout: funds go buyer → merchant. Tilla never holds them.
>
> (Every proof is a self-funded arm's-length test, labeled as such — not organic demand.)

**Post 5c (the part almost nobody has: Tilla BUYS too)**
> Every "agent commerce" demo shows an agent selling. Tilla also spends.
>
> Before a store goes live, Tilla hires another agent to security-screen the content
> and pays for it over x402 — 0.1 USDT0, settled on X Layer:
> tx 0xf546da66…f403cc
>
> Paid, answered, and the verdict actually gated the content.
>
> Tilla is a customer in the agent economy, not just a vendor.

**Post 5d (the boring part that matters)**
> Tilla rates the agents it hires — and it refused to publish this one.
>
> It couldn't confirm the two agents had independent owners, so it withheld the rating rather
> than assume. Fails closed on unknown. No override flag.
>
> (It was right to: they do share an owner.)
>
> Reputation you can't verify is worth nothing. We'd rather ship the guard than the star.

*Accuracy note for the owner — do not post this line.* An earlier draft said "the code checks the
owner and declines," which is **not** what production recorded. The logged reason is `cannot verify
independent ownership (self=None, target=None)` — the guard could not resolve **either** owner
address and refused on that basis. That is a stronger property than catching a known match, so the
post above claims the fail-closed behaviour, which is what actually happened. The shared-owner fact
is separately true from the marketplace (both report `0xf4c9…fa51`) and is kept as a parenthetical.
See `docs/PROOF-onchain.md` §11.

**Post 5e (the part that makes a generated store look real)**
> Generated stores usually look generated: headline, blurb, price, coloured box.
>
> Tilla writes a photo brief per product, then refuses any picture that can't be proved to show
> the thing being sold — it scores every candidate against the provider's own caption, and a
> vision model looks at the chosen image before it ships. Wrong product, someone else's brand:
> rejected.
>
> 26 live stores. 219 photographs. Nothing stock-generic, nothing mislabelled.
>
> And when a store sells software — nothing photographable — it gets drawn atmosphere instead,
> clearly labelled as illustration. A real photo of a real desk would be a claim about goods
> that don't exist.

**Post 6 (the vision)**
*Source for the figures, if a judge asks — McKinsey projects agentic commerce at up to **$1T in
orchestrated US retail revenue by 2030** (~30% of projected B2C revenue across all retail
categories) and **$3–5T globally**. Re-checked 2026-07-26. The post's phrasing matches this; don't
inflate it.*
> Agentic commerce is projected at $1T in the US alone by 2030 (McKinsey estimates $3-5T global). Most of that value needs a storefront layer that speaks both human and agent.
>
> Tilla is that layer — the commerce OS for the one-person company, built on @OKX rails.

**Post 7 (close / CTA)**
> Built for #OKXAI Genesis Hackathon. Targeting Software Utility, Best Product, and Revenue Rocket.
>
> Try it, describe what you sell, get a live store back.
>
> https://tilla.gudman.xyz/
>
> @OKX @XLayerOfficial

---

## 2. Google Form / Submission Answers (draft)

| Field | Answer |
|---|---|
| **ASP Name** | Tilla |
| **OKX Agent ID** | #6961 |
| **One-line pitch** | Describe what you sell → a live, branded crypto storefront on OKX X Layer, in minutes — sells to humans and agents. |
| **Category** | Software Utility (primary). Also relevant: Best Product, Revenue Rocket. |
| **Live URL (product)** | https://tilla.gudman.xyz/ |
| **Live URL (example store)** | https://tilla.gudman.xyz/s/invoice-flow/ |
| **Endpoint (ASP)** | https://tilla.gudman.xyz/create-store (x402-gated, 0.05 USDT, "Create Storefront") |
| **Listed services** | 7 under agent #6961 — Create Storefront + 6 Tilla-built storefronts listed as buyable services. Each carries a full parameter contract and a worked example, and every one completes for an unattended agent buyer (an empty paid create returns a sample store). |
| **Scale built** | 26 live stores, every one carrying real product photography (219 images), 1218 automated tests green in CI |
| **Agent-to-agent proof** | Job `0xcbe0ce6e…a005` — an OKX.AI user task designated to #6961 ran connect → x402 agreement → 0.05 USDT paid → deliverable returned → `[x402 Job Completed]`, producing a live store at https://tilla.gudman.xyz/s/checkpoint/ |
| **On-chain proof log** | `docs/PROOF-onchain.md` — 11 rails proven with re-verified receipts, incl. all four x402 schemes settled and Tilla *paying* another agent (0.1 USDT0, block 66208040). All self-funded arm's-length tests, labeled as such; none is organic third-party demand. |
| **X post link** | [placeholder — insert after posting the thread above] |
| **Demo video link (≤90s)** | [placeholder — insert once recorded] |

**Full description:**
> Tilla is the first store-builder on the OKX.AI marketplace. A merchant describes what they sell in one sentence — Tilla generates a brand (name, palette, copy) via LLM, deploys a real premium storefront to a live URL, and wires up crypto checkout: the buyer pays USDT on X Layer, the payment is verified on-chain, and the digital product is delivered automatically. Every store is dual-sided — a normal website for human buyers, and simultaneously an Agent Service Provider (ASP) that another agent can pay via x402. Tilla itself is registered on OKX as ASP #6961, offering a paid "Create Storefront" service, so the product is both a builder of agentic-commerce storefronts and a live example of one.

**What it does:**
> Takes a one-sentence product description and turns it into a deployed, branded, crypto-accepting storefront — no code, no hosting setup, no manual wallet integration required from the merchant.

**Why it matters:**
> Agentic commerce is forecast at roughly $1T in the US alone by 2030 (and $3-5T globally, per McKinsey), but almost nothing exists to let a non-technical solo entrepreneur stand up a storefront that can transact with both human customers and autonomous agent buyers. Tilla is the missing on-ramp — it turns OKX's Agent Payments Protocol into something a single founder can use in one prompt, directly serving Star Xu's "one person, unlimited workforce" (OPC) thesis.

**What's novel:**
> - First storefront/checkout builder confirmed to exist on the OKX.AI marketplace (verified gap across EN and CN listings — re-checked 2026-07-27: OKX's own `asp-match` router, asked to "build me an online store with crypto checkout", still returns only price/news/research agents. It has no storefront ASP to route to).
> - Dual-sided by construction: the same store is a human-facing website and an agent-payable ASP simultaneously — a structure only expressible on OKX's rails.
> - Full-stack proof of OKX's Agent Payments Protocol vision (x402, on-chain verification, agent-to-agent commerce) wrapped in a one-prompt UX, rather than a developer-only integration.
> - Product imagery that fails closed: a per-product photo brief, candidates scored against the provider's own caption, and a vision check on the chosen image before it ships — so a store never displays a competitor's branded product or the wrong item. Stores selling something unphotographable get labelled generated illustration rather than a misleading stock photo.

---

## 3. "Why This Matters to OKX" — 100-word pitch for marketing/partnership reviewers

> Tilla makes OKX's own agentic-commerce thesis usable by anyone. OKX has built the rails — x402, the Agent Payments Protocol, X Layer settlement — but almost no product lets a non-technical solo founder actually stand one up. Tilla does: one sentence in, a live branded store out, accepting crypto from humans and agents alike. It's simultaneously a marketplace product (ASP #6961) and a walking demo of why OKX's payment infrastructure matters — the flagship "one person, unlimited workforce" story judges and partners can point to. A natural featured-partner candidate for OKX's own agentic-commerce narrative.

---

## Notes for the owner

- All URLs, agent ID, the live 402 challenge, and pricing above are the verified real assets, re-verified 2026-07-27 — nothing invented.
- Two placeholders remain: the demo video link and the X-post link (fill in once each exists, then paste the X-post link into the form).
- Recommend posting the X thread first, then using its URL in the form's "X post link" field.
- Do not post or submit until you've reviewed and approved this draft.

**⚠️ Listing status at the time of writing (2026-07-27, ~07:20 UTC).** Agent #6961 reads
`approvalDisplayStatus 2` — *"Listing under review"* with remark *"AI quality review suggested
pass"*, so its marketplace card currently shows **"not listed"** while the re-review runs. Two
earlier reviews were rejected and both causes are fixed and verified: (1) an nginx rate limit
answered OKX's own reviewer with 503 — raised, and rejections now return 429; their probes on
2026-07-27 returned zero 503s; (2) the A2A reply layer had a stale runtime and timed out — restarted,
and the completed job above is the proof it now answers.

If the listing is still "not listed" when you submit, **say so plainly in the form rather than
claiming otherwise** — the seven services, the endpoints, the stores and the completed A2A job are
all independently checkable regardless of the card's state, and an honest note costs less than a
judge finding the discrepancy. Nothing in this document depends on the review clearing.

**Nine of the recent orders sit in `settling`** — OKX's review bot buys from every storefront and
its settlements have never landed on-chain. Those are not revenue and must never be counted as
sales; `soldCount` on the marketplace card (9) is the only figure that reflects settled activity.
