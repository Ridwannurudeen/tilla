# Tilla UI — Locked Art Direction ("Proof of Purchase")

> Signed off by the product owner. Hero = **real on-chain proof**. Flagship theme = **maximal & expressive**.
> Every design agent runs on **Fable 5**. Do not deviate from the contracts in §6.

## 1. The one idea
Every Tilla store is a **live, signed, on-chain object** — a page in one ledger that a human *or an AI agent*
can actually buy from, with settlement provable on X Layer. The design makes that real, un-fakeable fact the
hero. We are not decorating a checkout; we are dramatizing that this thing settles on-chain, right now, and
that an agent can buy it too (genuinely true — the stores are x402-purchasable).

## 2. The three signature moves
1. **Rail vs. Merchandise.** The merchant's LLM palette (`--primary`/`--accent`) colors ONLY the merchandise
   (product, CTA, price accent). Everything structural — type, hairlines, grid, receipt — is monochrome ink
   derived from `--bg`/`--text`. One fixed color, **OKX brand lime `#BCFF2F`** (verified in OKX production CSS
   as `--okd-color-branded-primary`), marks ONLY where money touches the chain: the X Layer chip, the
   confirmed step, the "verified on-chain" receipt line. Text on lime is always `#000` (`--on-xlayer`).
2. **Mono is Money.** OKX literally sets its "New Money Chain" in monospace. Every on-chain number, hash,
   address, price, and chain label uses the inlined mono face (Geist Mono). On-chain metadata is the luxury
   detail, not hidden plumbing.
3. **The Block Print.** OKX's logo grammar is a 3×3 square grid ("building blocks"). We take the *grammar*
   (never the logo): a per-store generative square-tile mosaic, deterministically seeded from `SLUG`, drawn
   once to a `<canvas>` in `--primary`/`--accent`/`--tint` at varying alpha. Flagship only.

## 3. Fixed rail tokens (NEVER palette-derived — hardcode in each theme's :root)
```
--xlayer:#BCFF2F; --on-xlayer:#000; --xlayer-tint:#e6ffb0;
```
OKX semantic colors available for the dashboard only: profit `#31bd65`, danger `#f5384f`, warning `#ffb117`.
[Unverified: OKX publishes no official name for the lime; no public brand book. Treat #BCFF2F as observed.]

## 4. Type system (all SIL OFL-1.1, inlined as subsetted woff2 data-URIs — NO CDN)
- **Display / body:** Space Grotesk (headings, body). **Money / chain:** Geist Mono (all numbers, hashes, labels).
- Pipeline: download each font's source + its `OFL.txt`; `pyftsubset` with
  `--unicodes="U+0020-007E,U+00A0,U+00B7,U+2013-2014,U+2018-201D,U+2026,U+2192,U+2713,U+2261"`
  `--layout-features="*" --flavor=woff2`; base64-inline into a shared `themes/_fonts.html` partial that every
  theme `{% include %}`s. **License step (mandatory):** read each `OFL.txt`, and if it declares a Reserved Font
  Name, rename the internal `name` table family to a non-reserved name (e.g. `TillaGrotesk`, `TillaMono`) —
  subsetting creates a Modified Version, and the RFN may not be reused. Keep name IDs 0/13/14. Record the
  license verdict + source URL in a comment at the top of `_fonts.html`.
- Type scale: 1.25 modular, 16px base. Body line-height 1.5–1.6. Tabular figures for all money/data.

## 5. Per-surface direction (make the three themes STRUCTURALLY distinct, not three fonts)

### `original.html` — "The Proof" — FLAGSHIP, maximal & expressive
1. Thin fixed top rail: store name (display) + emoji inside a bordered square (block-print mark, not a floating
   emoji); right side a live **`X LAYER · chainId 196`** mono chip with a lime dot + an **Agent-buyable** badge.
2. Maximal hero: oversized `HERO_HEADLINE` (display, huge clamp) with a per-letter load reveal (split
   `textContent` in vanilla JS on load — NEVER split in Jinja, it fights autoescape). The **Block Print mosaic**
   canvas sits behind/beside it. Oversized `PRICE` in mono + "USDT" + a lime "settles on X Layer" microline.
   `HERO_SUBCOPY` below.
3. **The Proof strip (the hero moment):** a monochrome ledger band with lime accents — "This store is a signed
   page in one ledger" + three mono facts: settlement token (USDT on X Layer), **agent-buyable via x402**, and
   "verified on-chain" with the lime check. Dot-leaders + hairlines. This is where the un-fakeable edge lives.
4. Product/buy module — the ONLY place the merchant palette dominates: `PRODUCT_NAME` (display),
   `PRODUCT_BLURB`, `PRICE` (mono, oversized), pill **BUY** button filled `--primary` with `--on-primary` text,
   then `{% include "_checkout.html" %}`.
5. Footer: quiet mono, hairline top border, "Powered by Tilla · settled on X Layer".
- Motion (maximal but tasteful, ALL behind `prefers-reduced-motion`): hero per-letter stagger on load;
  IntersectionObserver scroll-reveal on the proof strip + product; mosaic draws once. Micro-transitions
  100–200ms on hover/press. `transform`/`opacity` only.

### `bold.html` — "The Drop" — brutalist, structurally distinct
Massive uppercase display; hard offset shadows (offset in `--primary`); solid flat color blocks; the square
grammar as a few BIG tiles (no generative canvas); heavy grid rules; mono prices; a loud **AGENT-BUYABLE**
stamp. Loud statement. Same tokens, same checkout include.

### `editorial.html` — "The Ledger Page" — quiet magazine, structurally distinct
Asymmetric multi-column, generous whitespace, oversized index numerals, hairline rules, a drop-cap, restraint.
Money is present but subtle/elegant (mono, smaller). No mosaic. Quiet luxury. Same tokens, same checkout include.

### `_checkout.html` — the money moment (shared, included by all themes)
Restyle the panel to the system. **The receipt prints on confirmation:** add `@keyframes print-receipt`
(top-to-bottom `clip-path`/height reveal, ~1.1s, subtle tear edge, lime "verified on-chain" line, tx hash in
mono) on `.co-receipt`. It plays automatically when the existing JS flips `#coReceipt` from `display:none` to
`display:block` (CSS animations restart on display change — VERIFIED). **Do NOT change any JS logic**; only CSS
+ markup classes the existing IDs already carry. Preserve every element ID (§6). reduced-motion → instant.

### `_dashboard.html` — merchant ops (information design, NOT editorial)
OKD-style surface ladder (`--bg → --surface → --pressed`), data tables with tabular mono numbers, lime for
on-chain/settlement states, OKX semantic colors for order states. Preserve the data-free/textContent-only shell.

## 6. CONTRACTS — breaking any of these fails the build
- **Tokens:** themes may reference only the 15 render tokens already supplied + the fixed rail tokens (§3) +
  derived CSS (`color-mix`, Baseline since 2023) + the one new `C_ON_PRIMARY` token. Do NOT invent tokens that
  render.py must supply. Keep the `:root{ --primary:{{C_PRIMARY}}; --accent:{{C_ACCENT}}; --bg:{{C_BG}};
  --text:{{C_TEXT}}; }` line in every theme.
- **render.py:** the ONLY change is adding `C_ON_PRIMARY` (pure `#000`/`#fff` by `--primary` relative
  luminance, WCAG formula) to `_palette_ctx`, exposed as `{{C_ON_PRIMARY}}`. Keep `_safe_hex` + fallbacks and
  autoescape env exactly. No auto-WCAG-repair. Existing render tests MUST stay green.
- **Checkout JS:** untouched. Preserve IDs: `co, coAmount, coAddr, coStatus, coDelivery, coCountdown, coQR,
  coManual, coReceipt, coClaim, coClaimHint, claimBtn, copyAddrBtn, copyAmountBtn, payWalletBtn`. Keep
  `{% include "_checkout.html" %}` in every store theme.
- **Security:** autoescape stays on; never drop untrusted copy into a `<style>`/`<script>` context; palette
  only via the validated hex vars; NO external requests (fonts inlined, no CDN, CSP-safe); the merchant
  `{{EMOJI}}` and all copy render through autoescaped text nodes only.
- **Self-contained:** everything inline; no network at render or view time except same-origin `/api` calls the
  checkout already makes (that is the app's own API, not a CDN — the live money-moment is allowed and expected).
