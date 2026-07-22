# Product Plate — a designed, self-contained product visual for every store

## The idea
Stores today show only `{{EMOJI}}` as a small `.mark`. The Product Plate gives each store a real focal
**product image** without any external image, upload, AI-gen, or CDN — self-contained, unique per store, zero
merchant work (keeps the "describe → live in minutes" magic). The brand emoji is the focal SUBJECT; a
generative geometric composition — seeded from the store's own identity, in its palette, in the Block Print
grammar — is the DESIGN that elevates it from "an emoji" to "a product portrait."

## What we have to work with (NO new server tokens — use only these)
`{{SLUG}}` (validated), `{{EMOJI}}` (autoescaped text, ≤8 chars), `{{PRODUCT_NAME}}` (autoescaped),
palette `{{C_PRIMARY}}/{{C_ACCENT}}/{{C_BG}}/{{C_TEXT}}/{{C_ON_PRIMARY}}`, the fixed rail tokens, and the DNA
tokens `{{DNA_TEXTURE}}` (sparse|medium|dense) / `{{DNA_SCALE}}` etc. Do NOT add render.py tokens.

## Placement
In each theme's **product / buy module** (the palette-dominant zone), as the visual anchor of the product —
paired with `{{PRODUCT_NAME}}` / `{{PRODUCT_BLURB}}` / price / BUY. It replaces the "empty typography" feel
with a real focal image.

## Composition (shared DNA, per-theme execution)
- The **emoji** is the focal subject: large, deliberately placed (a chip, an emboss, a framed medallion — the
  theme decides), rendered as autoescaped TEXT or an SVG `<text>` node — NEVER via innerHTML; if drawn on a
  canvas use `fillText` reading the glyph from `textContent`/a `data-` attribute, never string-built markup.
- The **generative field** is seeded from `SLUG` with the SAME FNV-1a→mulberry32 hash the hero mosaic uses, so
  a store's plate and mosaic share one deterministic identity. Squares/Block-Print grammar, palette colors at
  varied alpha. Density scales with `DNA_TEXTURE` (sparse/medium/dense).
- Palette discipline unchanged: merchant palette OK here (this is the palette-dominant module); OKX lime only
  on chain/money moments (not the plate); everything else ink-derived.

## Per-theme direction (make them structurally distinct, like the themes themselves)
- **original.html** (flagship): a layered generative "portrait plate" — a framed field (SVG or a second small
  canvas) with the slug-seeded block composition and the emoji as a large centered medallion; a subtle
  scroll-reveal (behind `prefers-reduced-motion`). Feels like the product's portrait.
- **bold.html**: a brutalist **product block** — the emoji huge inside a thick-bordered, hard-offset-shadow
  frame over solid palette color blocks; zero radius; loud.
- **editorial.html**: a quiet **framed plate** — the emoji in a hairline-ruled panel with an index numeral and
  a single fine geometric mark; generous whitespace; restrained (money/plate stay subtle).

## Contracts — MUST all survive (verify each)
- 628-test suite green. If a test asserts the emoji appears exactly once and the plate intentionally shows it a
  second time, update THAT assertion minimally to the new intended count and flag it in notes — never weaken an
  XSS/SSTI/security assertion, and keep DEFAULT_PALETTE-present + no-"49" + no-`{{`-leak intact.
- Introduce NO substring "49" into rendered output (avoid 49 in any px/size/hex/coord you add).
- Keep verbatim: the `:root{ --primary:{{C_PRIMARY}}; --accent:{{C_ACCENT}}; --bg:{{C_BG}}; --text:{{C_TEXT}}; }`
  line; `{% include "_fonts.html" %}` + `{% include "_checkout.html" %}`; the favicon `<link rel="icon">`;
  `buyBtn` id + `onclick="startCheckout()"`; the theme fingerprint (original = mosaic canvas + per-letter hero;
  bold = `class="stamp mono"`; editorial = `class="folio mono"`); the DNA machinery (`data-hero`, `--dna-*`);
  autoescape (no `|safe`, no `autoescape false`); no CDN/external anything.
- The emoji in the plate must stay autoescaped/text-context safe (an emoji field is length-capped but still
  merchant-influenced — treat as untrusted: text nodes / fillText only, never innerHTML or a `<style>`/`<script>`
  context).
- Accessibility: the generative field is decorative (`aria-hidden="true"`); the product remains labeled by
  `{{PRODUCT_NAME}}`; any plate motion respects `prefers-reduced-motion`.
- The plate must render well on BOTH a dark and a garish light palette (test both), and must not cause
  horizontal overflow at mobile width.
