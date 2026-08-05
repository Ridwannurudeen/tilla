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

---

## #2 — Generated copy claims capabilities the merchant never stated

**Reported by:** Rouma Desk (`0x51c25782…`), on their own paid `create-store`, 2026-08-05. Rated the
purchase highly on every other axis — challenge matched the published contract in every field, one
call returned a live store, their `receive_address` was the only payee, name and price byte-exact —
and flagged this as the single caveat.

**Verified reproduction (their store `dossier-reports`, from the production DB):**

| | |
|---|---|
| Merchant description | "I sell signed token due-diligence reports for EVM tokens, one product at 0.01 USDT, my shop is called Dossier Reports" |
| Generated blurb | "…verified analysis of contract security, **tokenomics**, **team credibility**, and market risk…" |

Neither "tokenomics" nor "team credibility" appears in the merchant's description. The claim is also
checkably false about their product: their live report (bought 2026-08-05, `format: json`) returns
checks `honeypot`, `contractControl`, `liquidity`, `marketActivity`, `holderConcentration` — there is
no team-credibility analysis in it at all.

**Cause.** The prompt constrains prices ("an invented price is a claim about someone's business"),
names, and photography ("a wrong photo … is a false product claim we made on the merchant's page"),
but the product blurb was specified only as `1-2 sentences, benefit-led` (`app/engine.py`). Nothing
bounded what it could assert, and "benefit-led" on a thin description invites the model to fill the
space with impressive specifics.

**Why it matters.** This is the same failure class as issue #1 — an invented claim about someone
else's business — but it reaches further. An invented price is corrected by the merchant before a
sale; an invented capability is read by a *buyer*, who pays expecting something the product does not
do. Tilla published a false advertisement on a merchant's own storefront.

**Status: FIXED 2026-08-05** (deployed). The blurb now takes the same restraint rule as prices and
names: describe only what the merchant's description supports; never invent capabilities, features,
credentials, integrations, guarantees or coverage they did not state; when the description is thin,
keep the copy short and general — vague and true beats detailed and false. A test pins the rule in
the prompt, matching the price/name tests.

The fix applies to stores generated from now on. Copy already generated is persisted per-store, so
the startup re-render faithfully reproduces it — an affected merchant clears it by editing the
product blurb (dashboard or `manage_key`) or regenerating with `upgrade-store`.

---

## #3 — A thin description invents a whole product catalog, prices included

**Found:** 2026-08-05, while verifying the fix for issue #2 — and disclosed to Rouma Desk before they
could find it. Same family as #1 (invented prices) and #2 (invented capabilities).

**Verified reproduction (live generation against the deployed prompt):**

| Merchant description | Generated catalog |
|---|---|
| "I sell trading signals" | Daily Trading Signals **49** · Weekly Signal Report **99** · Premium Signal Access **199** ("priority alerts and extended coverage") |
| "I sell consulting" | Strategy Session **500** · **Project Consulting 2500** · Retainer Package **1500** |
| "I sell a newsletter" | Weekly Newsletter 8 *(correct — one product)* |

The merchant named exactly one thing in each case. The catalog, the tier names, the feature
differentiators and every price are inventions.

**Why it is worse than #2.** An invented capability is a false sentence. An invented *tier* is a false
sentence **plus a false product plus a false price** — a merchant who wrote one line about consulting
could publish a store offering "Project Consulting" at 2500 USDT, an engagement they do not sell, at a
number they never chose. #1 fixed the price of a product the merchant asked for; this invents the
product too.

**Cause.** `app/engine.py`'s catalog instruction read: *"a focused catalog of related items that fit
the brand; use a single item when the merchant clearly sells one thing, otherwise 2 to 4 distinct
items."* Multi-product is the **default** whenever the model does not judge the description "clearly"
singular, and a one-line description is exactly the case where it has nothing to judge with — so it
fills the gap by inventing a product ladder.

**Status: FIXED 2026-08-05** (deployed). The default is inverted: **one product unless the merchant's
own description names more than one distinct thing they sell**, with tiered ladders
(Basic/Premium, Daily/Weekly, Starter/Pro) called out as forbidden inventions. A merchant who wants
more products adds them deliberately — `POST /api/merchant/stores/{slug}/products` — which is the
path that cannot fabricate anything.

**Accepted trade-off, stated plainly.** A thin prompt now yields a one-product store rather than a
fuller-looking one. That is a real cost to the demo and the correct call anyway: the same principle as
#1 and #2 — Tilla does not get to invent claims about someone else's business, and "looks richer" is
not a reason to publish a product they never offered.

---

## #4 — Quantifiable commitments leak into copy at word scale

**Reported by:** internal. **Found:** 2026-08-05, while verifying the fix for issue #3. The last
member of the #1/#2/#3 family, and the smallest.

**Verified reproduction (live generation against the deployed prompt):**

| Merchant description | Generated blurb |
|---|---|
| "I sell trading signals" | "**Daily** trading signals to guide your market moves" |

One adjective. The merchant never said daily, and the storefront now advertises a delivery cadence
they never offered.

**Cause.** The claims rule (fixed under #2) forbids invented *capabilities, features, credentials,
integrations, guarantees or coverage*, and #3 forbids invented *products and tiers*. A measurable
promise is neither — so a single quantifying word passed both rules. The class the prompt never
bounded: delivery frequency (daily/weekly/real-time/24-7), turnaround and response times
(instant/within 24 hours), quantities and coverage counts (500+ tokens, all chains), and
guarantees/SLAs.

**Why it matters.** It is the same harm as an invented capability, at a scale that survives review.
An invented paragraph is visible; "Daily" is one word in a blurb a merchant skims and approves. The
buyer is the one who pays for a daily feed that does not exist.

**Status: FIXED 2026-08-05.** Quantifiable commitments now take the same rule as prices, names and
claims: never state a frequency, turnaround, count, guarantee or SLA unless the merchant's
description states it — and when they *did* state one ("daily signals", "48-hour turnaround"), use it
exactly and keep it. The prompt carries a worked negative example of this exact failure ("I sell
trading signals" → "Trading signals to guide your market moves", not "Daily trading signals…"),
because the model follows a worked example far more reliably than an abstract rule.

**Accepted boundary: tone is not a commitment.** The rule bans the *measurable promise*, not the
warmth. Evocative, textural copy — "rich, full-bodied", "crafted with care", "cut through the noise"
— remains explicitly welcome, and one sentence of the rule says so, because the merchant who
reported #2 praised that texture in the same breath as the caveat. A version of this fix that
flattened the copy into a spec sheet would be a regression, not a stricter fix. A test pins the rule,
the worked example **and the carve-out** into the prompt, and the assembled prompt was swept for
phrases inviting urgency or speed claims — the #2 lesson that an early instruction beats a later rule.

The fix applies to stores generated from now on; already-generated copy is persisted per-store and is
cleared by editing the blurb (dashboard or `manage_key`) or regenerating with `upgrade-store`.

---

## #5 — The claims rule only bound the product blurb; hero copy, names and growth prompts sat outside it

**Found:** 2026-08-05, adversarial audit of the whole generation surface after #4.

Three gaps of one family. (a) `tagline` / `hero_headline` / `hero_subcopy` had length-and-tone specs
only — and `hero_subcopy` is deliberately the PREFERRED machine-readable store description
(`agentic._store_description`: it outranks the merchant's own words in feed.json, llms.txt,
discovery, the Google RSS channel and every meta/og description). An invented credential in that one
sentence became the description agents parse before paying. (b) The names rule licensed inventing a
name with no ban on credential words — "Certified Notary Co" mints a permanent slug asserting a
certification nobody claimed. (c) The growth-kit and channel prompts ("compelling and on-brand")
carried NO claims language at all, and the performance block's "do NOT quote them verbatim" licensed
paraphrasing 6 orders into "selling fast" — invented traction, published off-platform under the
merchant's own name.

**Status: FIXED 2026-08-05** (deployed). On-field anchors on all three hero fields; the claims rule
scope names every copy field; a hero worked example; credential/authority words banned in invented
names; and a single shared restraint block (`engine.COPY_RESTRAINT`) appended to both growth prompt
builders — shared verbatim so the storefront and marketing rules cannot drift, with a test pinning
five clauses present in both. The performance block now names the illegitimate use: numbers choose
the angle, they never become a traction claim.

---

## #6 — Every machine feed described every product with the FIRST product's blurb

**Found:** 2026-08-05, same audit. `_product_description(store)` returned the store-level
`product_blurb` — always products[0]'s — and was emitted as the per-product `description` in
feed.json, MCP `get_product`, the OpenAI product feed and Google `g:description`. On a store selling
a 10-USDT summary and a 500-USDT review, all four feeds advertised the 500-USDT product with the
10-USDT product's text, attached to a priced buy endpoint, while the human page (which builds
per-product blurbs correctly) said otherwise. The adjacent `product_image_url` already matched by id
for exactly this reason.

**Status: FIXED 2026-08-05** (deployed). `_product_description(store, product)` matches the content
item by product id; legacy single-product content keeps its store-level blurb; with 2+ items and no
id match it returns "" — a missing description over the wrong product's. Six tests pin all four
surfaces, the legacy shape, and the stale-id case; restoring the old one-liner fails five of them.

---

## #7 — Generated imagery could depict "the product in use"

**Found:** 2026-08-05, same audit — and live: `TILLA_IMAGE_GEN=1` in production. The generation
fallback filled any non-product slot when stock failed, including LIFESTYLE slots whose queries are
defined as scenes "showing the product in use". A generated frame of the merchant's goods in use is
a fabricated depiction — the image form of #1–#3 — cured by neither the branding check nor the one
footer line. The module's own honesty argument ("a hero asserts nothing about the goods") covers
heroes only.

**Status: FIXED 2026-08-05** (deployed). Generation is hero-only (`kind == "hero"`); a lifestyle
band stock cannot fill stays empty, which the module has always counted as a correct outcome. Test
proven load-bearing: restoring the old condition fails it with "generated a lifestyle" in the log.

---

## #8 — Feeds hard-coded availability and condition

Server-asserted, not model output: JSON-LD `availability: InStock`, feed.json/OpenAI
`"in_stock"`, and Google `g:availability in_stock` + `g:condition "new"` were emitted for every
active product unconditionally. The condition half was an invented claim — Tilla has no condition
field and knows nothing about the goods' condition; a used-goods merchant would have been
misrepresented in a machine-readable feed.

**Status: FIXED 2026-08-05** (deployed), in two honest halves. *Condition:* the `g:condition` tag is
gone. Google's product data spec makes condition **optional for new products and required for used/
refurbished ones** (verified against the spec, 2026-08-05), so omitting it asserts nothing and
complies either way; a future used-goods merchant needs a real merchant-supplied condition field,
never a hardcoded "new" — that is a feature gap, recorded here, not a false claim. An absence test
covers every feed item and fails if the tag returns. *Availability:* `in_stock` STAYS, because it is
true by construction — every feed emits ACTIVE products only (`_active_products` is the single
source for the Google feed, the per-store OpenAI feed and the aggregate), and `active` is the same
gate the buy paths enforce (agent buy 404s, checkout 422s an inactive product). That premise is now
pinned by a test (one active + one inactive product → only the active one appears in all three
surfaces) which fails if the active filter is ever dropped.

## #9 — upgrade-store could re-attach blurbs positionally

`upgrade_store` regenerates the catalog (items carry no DB id) and `resync_catalog` backfilled ids
positionally (`key = active[i].id` when the item had none). The positional rule was written for
pre-CRUD legacy content, where order provably IS id order — but freshly generated items' order and
count are the model's, so a blurb describing one product could attach to a different one: false
text about a real product, on the page and (since #6) in every machine feed.

**Status: FIXED 2026-08-05** (deployed). Position is no longer read anywhere. An id-less item now
attaches by an attachment ladder: exact NAME match (casefold, duplicates consumed in id order), else
the unambiguous 1:1 case (one old item, one active row — the common single-product upgrade where the
model renamed the product), else NOTHING — the product renders an honestly-empty blurb the merchant
can fix, never text about goods they do not sell. Legacy content still carries: its names equal the
row names by construction, and every dashboard rename resyncs content from the rows. Seven unit
tests plus the real upgrade-path seam test (regenerated catalog swapped AND carrying an invented
item) pin it; three of them fail with the positional code restored, reproducing the exact defect.

**Accepted trade-off, stated plainly:** a multi-product upgrade whose regenerated names all differ
drops those blurbs to empty rather than guessing. Honest-empty beats detailed-wrong — the same
principle as #1 through #7.

---

## #10 — The paid create route validated brand_color and silently ignored it

**Found:** 2026-08-05, while reading `create_store_post` during the buyer-hardening batch.

`POST /create-store` (the x402 agent path) advertised `brand_color` in its own 402 challenge
(`extensions["tilla.input"].optional`), validated it on `CreateStoreBody` — a bad value returned 422,
proving the field was read — and then never passed it to `_run_create_store`. An agent that stated a
brand colour paid the fee and received a randomly-seeded hue.

The whole chain already existed: `_run_create_store` accepts `brand_color`, `gen_store` applies
`hue_from_hex`, and the dashboard self-serve path passes it. **Only the keyword was missing at one
call site**, which is why every test passed — the existing test called `engine.create_store(...)`
directly and so exercised the engine, never the route. Same class as the optional-dependency wiring
bugs: a caller guarantee dropped in silence, invisible to a green suite.

**Status: FIXED 2026-08-05** (deployed). One argument added; a new test drives the HTTP route and
asserts the stated hue lands in the persisted content. Mutation-proven — removing the argument fails
it at `assert store.content["brand"]["hue"] == 0.0`.

---

## #11 — A store could go live unpaid if settlement failed after the handler returned

**Found:** 2026-08-05 by adversarial review of the idempotency design. **Pre-existing** — not
introduced by that change.

`create_store` commits the Store, its Products and its Deliverable, and only then does the x402
middleware attempt settlement (`x402/http/middleware/fastapi.py`: handler first, settle after). If
settlement then fails, the committed store stays live while no funds moved, and `/create-store` has
no `settlement_failed_response_body` hook (only the per-store buy route has one), so the caller
receives a bare 402 with an empty body — no slug, no manage_key. The store is orphaned and unusable.

**What issue #10's sibling change altered:** the new `Idempotency-Key` 409 hands that orphan's slug
and url back on retry, turning a previously unusable unpaid store into a delivered one. Buyer funds
are never at risk (a failed settle moves nothing); the exposure is Tilla revenue.

**Why it was open.** Severity depended on one externally-owned fact we cannot determine from source:
whether OKX's facilitator `/verify` rejects a payer who cannot fund settlement. That is exactly why
it is now fixed rather than measured — a compensator costs one column and one hook, and it is
correct whichever way verify behaves.

**Status: FIXED 2026-08-05** (working tree; not yet deployed). The compensating hook was built, not
the 409 gate, because gating the 409 on settlement evidence needs evidence the create path does not
have — there is no settle-SUCCESS writer for `/create-store`, only the buy path has one.

- `stores.create_x402_nonce` (migration `0035_create_x402_nonce`, nullable + a NON-unique index)
  records the EIP-3009 nonce of the payment that funded the create, bound into the store's OWN
  INSERT — the same atomicity rule as 0034's idempotency pair, since a nonce written after the
  commit leaves the window it is meant to close. Non-unique deliberately: a failed settle never
  consumed the nonce, so the retry the 402 body invites replays it and legitimately creates a second
  store, which a unique index would turn into an IntegrityError inside `create_store`'s insert —
  where 0034's classifier would read it as a slug collision and rmtree the directory.
- `agentic.create_store_settle_failed_hook` is now the route's `settlement_failed_response_body`. It
  recovers the nonce from the PAYMENT-SIGNATURE header (the existing `_nonce_from_context`),
  quarantines the store that nonce created as `status='blocked'` — the state every money path
  already refuses with a 404 and every discovery surface already excludes — and returns a body that
  says the payment did not settle, no funds moved, the store was not activated and a retry is free.
  Idempotent (an already-quarantined row is excluded from the match), fail-safe (a hook exception
  never masks the 402), and it refuses to act at all when one nonce maps to two unquarantined
  stores, since one of those may be a store a successful settle paid for.
- The `Idempotency-Key` 409 stays a 409 (>= 400 is what keeps the retry free) but no longer claims a
  quarantined store was "already created": it names the failed settlement and asks for a new key. It
  keys on the `store.settle_failed` event, not on the status, so a store withdrawn by content
  screening — also `blocked`, but genuinely paid for — is not told its payment failed.

Mutation-proven: deleting the quarantine write leaves `POST /api/checkout/{slug}` answering 200 for
a store nobody paid for, which fails
`test_settle_failure_quarantines_the_store_and_the_money_paths_refuse_it` (and four more).
