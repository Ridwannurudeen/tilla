# Known issues

Open defects with a verified reproduction, newest first. Fixed items move to the changelog in
`docs/BUILD.md`. Nothing here is a fund-safety or availability defect; those would be fixed
immediately rather than logged.

---

## #1 — Identical descriptions produce different products and prices

**Reported by:** Risingtell (`0x9ea2d10c…`), first external customer-reported issue, 2026-07-27.
Rated 5.0 on the listing, calling it a "minor rough edge"; the core purchase worked both times.

**Verified reproduction (their own two purchases, from the production DB):**

| Store | Products generated | Prices (USDT) |
|---|---|---|
| `rising-technology` | Custom Business Software · Software Maintenance & Support · Digital Transformation Consultation | **5000** · 500 · 1500 |
| `rising-tech` | Custom Business Application · Mobile App Development · Web Platform · Software Maintenance & Support | **2500** · 3500 · 1800 · 300 |

Same `description` input, same payer, minutes apart. The product **names**, **prices** and even the
**count** (3 vs 4) all differ. The customer's headline example — 5000 vs 2500 for the equivalent
flagship item — is exactly right.

**Cause.** Store *design* is deterministic — layout, palette and typography are seeded from the slug
via FNV-1a + mulberry32, which is the "no two stores look alike, but one slug is one design"
property working as intended. Store *copy* is not: `engine.generate()` posts to the model
(`app/engine.py`, `payload` has no `temperature` and no seed) and normal sampling makes each call
produce different catalog text. When the merchant's description does not state prices, the model
invents them, and the invented figure is unanchored — hence 5000 one run, 2500 the next.

**Why it matters.** An invented five-figure price on a real business is not cosmetic. A merchant who
does not read the generated catalog closely could publish a store selling their work at a number
Tilla made up. `MIN_PRICE_USDT` clamps the floor (`engine.py`) but nothing constrains the ceiling
beyond the field's `le=10000`.

**Fix direction (decide, do not rush — this is a product decision):**
1. *Honor stated prices, never invent unstated ones* — parse prices out of the description; when a
   product has none, mark it "price on request" or apply one documented default instead of a guess.
   Best for merchant trust; changes what a bare description produces.
2. *Deterministic copy per description* — seed generation from a hash of the description so the same
   input yields the same catalog. Removes the surprise without removing invented prices.
3. *Surface it* — return the generated prices in the create response with an explicit "review these"
   note, leaning on the `manage_key` edit path. Cheapest; least protective.

Options 1 and 2 compose. Whatever ships needs a test that runs the same description twice and asserts
the contract chosen.

**Interim mitigation (already true, no deploy needed).** Every store ships with a one-time
`manage_key`; a merchant can correct any product name or price immediately, and `upgrade-store`
regenerates the whole presentation. Tell affected merchants to check their catalog before sharing
the URL.

**Status: FIXED 2026-07-28** (`39631f2`, deployed). Both halves shipped:

1. *Prices belong to the merchant.* A price stated in the description is now used exactly; an
   unstated one must be modest, coherent across the catalog, and never a large headline figure.
   This is the half that mattered — an invented 5000 is a claim about someone's business.
2. *The catalog stops rerolling.* Sampling is greedy (`temperature: 0`), so the same description
   no longer yields different names, prices and product counts. Deliberately **not** described as a
   determinism guarantee: temperature 0 has never guaranteed byte-identical output.

`temperature` is model-gated — accepted on the configured `claude-haiku-4-5`, a 400 on Opus 4.7+ /
Sonnet 5 / Fable 5 — and `TILLA_LLM_MODEL` is env-settable, so the request drops the field and
retries once if the model refuses. A future model swap degrades to the old behaviour instead of
failing every paid `create-store`. Four tests pin the request shape, the retry, and that an
unrelated 400 still fails loudly.

The reporter's two stores are unchanged and still live; the fix applies to stores created from now
on. Either can be regenerated with its `manage_key` via `upgrade-store` if they want the new
pricing behaviour applied.
