# Design DNA — no two Tilla stores look alike (without ever rendering free CSS)

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
