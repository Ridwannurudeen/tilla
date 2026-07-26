# Design DNA — no two Tilla stores look alike (without ever rendering free CSS)

> ## Revision 2026-07-26 — the axes existed; the LLM was not using them
> Everything below shipped. Then the live catalogue was measured, and the result was
> the opposite of the promise: **the model visited about three points in a 972-point
> space.** `hero` was `stacked` on every editorial and original store and `offset` on
> every bold one; `scale` was `balanced` on ten of fourteen; four of five `original`
> stores carried **no `design_dna` at all**. Asked to choose freely, a model returns
> its modal answer — so more axes would only have added coordinates nothing visited.
>
> Four things changed as a result. See `app/engine.py` and `app/palette.py`.
>
> **1. The axes are seeded, not requested.** `resolve_design(slug, content)` derives
> them from an FNV-1a + mulberry32 draw on the slug — the same construction the
> mosaic already ran client-side, so one slug now seeds a store's whole identity.
> Measured over 4000 slugs: each persona lands within 9.5–10.6% and the most common
> full combination holds 2.5%. Three effective looks became 81.
>
> **2. Seeded picks are personas, not independent rolls.** Every axis value is
> individually safe, but independence is not taste — `monumental` + `tight` + `dense`
> is valid CSS and a cramped mess. Ten curated bundles someone would actually design,
> with two axes seed-jittered inside the chosen one so stores sharing a persona still
> differ. The LLM may still NAME a persona; that is one judgement it makes well,
> unlike five independent ones.
>
> **3. Typography became an axis.** It was the loudest remaining tell: all three
> themes hardcoded one typeface, so 900+ layouts still read as one brand. Four
> pairings built from the two already-inlined variable faces plus a system serif
> stack — zero new bytes, zero extra requests, no licensing. `grotesk` is today's
> stack exactly, so it is the default and pre-axis stores are untouched.
>
> **4. Colour is derived, not collected.** Four free hex values had no relationship
> to each other and only their *syntax* was checked; an accent could sit at 1.4:1
> against the ground and vanish. `app/palette.py` now derives the whole palette from
> one hue plus a named harmony and mood, and enforces text ≥ 7:1, brand ≥ 3:1, and —
> perceptually, via CIE76 ΔE rather than a contrast ratio, because a red and a green
> of equal luminance score 1.0 — an accent ≥ 22 ΔE from both the primary and the
> text. Verified across 2880 hue/harmony/mood combinations with zero failures.
>
> **What is unchanged, deliberately:** the renderer and the themes still read the
> same five axis tokens plus the new type token, all still server-validated enums,
> and there is still no free CSS anywhere. `design_dna` and `palette` are resolved at
> GENERATION time and persisted into `content`, never derived at render time — which
> is what makes a re-render (`resync_catalog` fires on any pricing or product edit)
> byte-stable for every store that already exists.

## The problem
Today the LLM controls 14 degrees of freedom (9 copy strings, 4 palette hex, 1-of-3 theme). Two stores on
the same theme are structurally identical — same hero, same type scale, same rhythm. That reads as a template.

## The principle (why this is safe)
We do NOT give the LLM free CSS. We give it a handful of **style axes**, each a server-validated ENUM with a
safe default — exactly how `_safe_hex` validates palette colors and `theme` is validated 1-of-3. The LLM
describes the brand's *personality* by picking enum values; the server maps each validated value onto CSS the
theme already owns. No combination can be broken, ugly, or unsafe, because every value passes a whitelist and
no attacker-controlled string ever reaches a style/script context. The slug-seeded mosaic already guarantees
no two stores share the generative texture even at identical DNA.

## The axes (v1 — 5 axes, ~small integer options each)
All optional in `content` (`content.get(...)` with a default), so **stores/tests that omit `design_dna`
render exactly as before** — this is what keeps the 594 suite green.

| axis | enum values | maps to | applies to |
|------|-------------|---------|------------|
| `scale`   | `compact` `balanced` `dramatic` `monumental` | `--dna-scale` ratio 1.18 / 1.25 / 1.34 / 1.5 driving the type scale | all themes |
| `weight`  | `light` `regular` `heavy` | display font-weight 300 / 450 / 700 (VF axis already inlined) | all themes |
| `rhythm`  | `tight` `roomy` `airy` | `--dna-space` spacing multiplier 0.82 / 1 / 1.35 (section padding) | all themes |
| `hero`    | `stacked` `split` `offset` | `body[data-hero=...]` layout variant | per theme (may collapse) |
| `texture` | `sparse` `medium` `dense` | mosaic fill probability + tile size (original); block-cluster count (bold); rule weight (editorial) | per theme |

## Server contract (app/engine.py + app/render.py)
- `GeneratedContent` gains `design_dna: DesignDNA | None`, where `DesignDNA` is a pydantic model whose fields
  are `Literal[...]` of the enums above — invalid values are coerced to the default (validator with fallback,
  never a 422 that blocks a sale; mirror the fail-closed spirit of `_safe_hex`).
- `render.py` `_dna_ctx(content)` returns validated tokens with defaults: `DNA_SCALE`, `DNA_WEIGHT`,
  `DNA_SPACE`, `DNA_HERO`, `DNA_TEXTURE`. A store with no `design_dna` gets the current look (defaults =
  balanced / regular / roomy / the theme's current hero / medium).
- The generation prompt asks the LLM to choose `design_dna` to fit the brand (loud product → heavy/dramatic/
  dense/offset; refined → light/airy/sparse/stacked), with the enum lists spelled out.

## Theme contract (each of original/bold/editorial)
- Read the tokens as `:root{ --dna-scale:{{DNA_SCALE}}; --dna-space:{{DNA_SPACE}}; }` +
  `<body data-hero="{{DNA_HERO}}" data-texture="{{DNA_TEXTURE}}" style="--dna-weight:{{DNA_WEIGHT}}">` — all
  values are server-validated enums/numbers, safe in these contexts.
- Express the variants in the theme's OWN css (`body[data-hero="split"] .hero{...}`) and by multiplying
  existing sizes/spacing by the vars. NO new free CSS, NO new fonts, NO new external anything.
- The generative scripts (mosaic) read `--dna-texture` via a `data-texture` attribute or a CSS var.

## Hard contracts that MUST survive (verify each after build)
- 594/594 tests green; a render WITHOUT design_dna is visually unchanged (add a test asserting the default
  path still emits the same key markers).
- The `_fonts.html` `4%39` percent-encoding is untouched; rendered html stays free of `"49"` in the SSTI test.
- checkout + dashboard `<script>` blocks byte-identical; all 15 checkout ids present; autoescape on; no CDN;
  the exact `:root{ --primary... }` line + both includes present in every theme; mosaic + receipt-print intact.
- New test: two different valid `design_dna` inputs on the same theme produce structurally different html
  (e.g. different `data-hero` / `--dna-scale`) — the "no two alike" guarantee, enforced.

## The demo line this buys
"Create two stores from the same one-line description — live — and get two visibly different designs, both
palette-safe, both agent-buyable." That's the anti-template proof no free-CSS builder can match safely.
