# Tilla

**Describe what you sell → get a live, branded crypto storefront that sells to humans _and_ autonomous agents.**

Tilla is a storefront-studio ASP (Agent Service Provider) on the OKX agent marketplace. A merchant
writes one prompt; Tilla generates the brand, copy, and product content, screens it, and publishes a
live store with non-custodial crypto checkout on **OKX X Layer** (chainId 196, USDT0). The same store
is simultaneously a human web checkout and a machine-payable surface — feeds, an MCP server, an agent
card, and x402 pay endpoints — so an autonomous agent can discover and buy from it the same way a
person can.

- **Demo video (81s):** https://youtu.be/mR4XXnKaY64
- **Live:** https://tilla.gudman.xyz — example store: https://tilla.gudman.xyz/s/iron-built/
  (photography showcase). A store that **actually fulfils** — pays out a minted licence key on
  purchase rather than a message — is `tilla.gudman.xyz/s/layercheck/`
  (every store also answers at `tilla.gudman.xyz/s/<slug>/`, and a merchant can point their own
  domain at it — see [`docs/runbooks/custom-domains.md`](docs/runbooks/custom-domains.md))
- **OKX marketplace:** live and **listed** as ASP **#6961** with **seven services** — the x402-gated
  `create-store` platform endpoint (0.05 USDT) plus **six Tilla-built storefronts listed as their own
  buyable services**, so every store Tilla makes becomes marketplace supply. Every listed service
  completes for an *unattended* agent buyer: a paid `create-store` with an empty body returns a
  sample store rather than asking a human for parameters.
- **Settlement:** X Layer mainnet (chainId 196), USDT0 `0x779ded0c9e1022225f8e0630b35a9b54be713736`

## Why it's different

Most storefront builders sell to people. Tilla sells to **people and agents** from one build:

- **Non-custodial by design.** Funds settle buyer → merchant directly on-chain. Merchant and buyer
  money is never held, custodied or moved by Tilla — every transfer of their funds is signed by them.
  Tilla's own two flag-gated keys spend only Tilla's own balance (the EAS attester paying gas for
  receipt attestations, and the Warden hire paying for a content screen), and neither can reach a
  merchant's or buyer's funds.
- **Dual-sided commerce.** A human uses wallet-connect checkout; an agent pays the same store over
  **x402** (EIP-3009 authorization) with no UI. Discovery is machine-native: `feed.json`, per-store MCP
  tools, an agent card, and a `/discovery` mirror.
- **Stores deliver real goods, not a receipt.** A merchant attaches a file, a licence or text —
  at create time or later with their manage key — and a delivered order mints an entitlement, so the
  buyer gets a signed download URL or a minted licence key bound to that order. Replacing a
  deliverable inserts a new version rather than mutating the old, so a buyer keeps the exact version
  they paid for. Until 2026-07-28 this was the product's biggest gap and it is documented as such in
  [`docs/PROOF-onchain.md`](docs/PROOF-onchain.md) §13, along with the first fulfilment Tilla was not
  a party to: an independent merchant selling their own goods to a buyer, with Tilla taking nothing.
- **One MCP endpoint to reach every store.** `POST https://tilla.gudman.xyz/mcp` is a live, public
  JSON-RPC server, so any MCP-compatible runtime is the shopfront and Tilla ships no concierge UI of
  its own. `browse_stores` and `search_stores` rank live stores and return, per result, the store page,
  its per-store MCP and x402 buy endpoints, and its reputation (sold count, success rate, unique
  buyers, last sale) — enough for a buyer agent to choose and pay without a human reading a storefront:

  ```bash
  curl -sX POST https://tilla.gudman.xyz/mcp -H 'content-type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
  ```
- **Screened content.** Every LLM-generated store and marketing asset passes Warden content screening
  before it goes live, and themes render through an autoescaped loader (a third-party theme can never
  opt out of escaping).
- **No two stores look alike.** Layout, typography and colour are derived per store, so two merchants
  describing the same business do not get the same shop — see below.
- **Real photography, verified against the product.** A store is not a wall of text: the hero, every
  product card and a lifestyle band carry actual photographs, self-hosted per store. A photo is only
  used when the provider's own description of it contains the concrete nouns the product requires, so
  a shop selling dumbbells never shows a yoga mat — see below.
- **Every store gets its own address.** `<slug>.tilla.gudman.xyz`, on a wildcard certificate that
  renews unattended, so a shop reads as the merchant's own rather than a row in someone else's
  directory. A merchant who wants their real domain can claim one and keep everything else the same.

## Every store is its own design

Two merchants can describe the same business and get visibly different shops. Layout, typography and
colour are **derived per store** from its own identity — seeded by the store's slug through the same
FNV-1a + mulberry32 construction the themes run for their generative texture, so one slug produces one
coherent design. Across 4000 slugs that yields **90 distinct structural looks**, with the most common
combination appearing just **1.68%** of the time and each of the ten design personas landing within
9.3–10.6%. Re-measured against the current code on 2026-07-28, not quoted from an earlier run.

- **Ten curated design personas** — `quiet-luxury`, `gallery`, `poster`, `zine`, `technical`,
  `warm-craft` and more. Each is a coherent bundle of scale, weight, rhythm, hero layout and texture,
  so every store lands on a composition someone would actually design, with two axes varied inside the
  persona so even stores sharing one stay distinct.
- **Four typography pairings** across grotesk, serif-display, all-serif and mono-display, built from
  self-hosted variable faces plus a system serif stack: **no webfont requests, no added page weight,
  no licensing burden.**
- **Colour computed, never guessed** ([`app/palette.py`](app/palette.py)). One brand hue plus a named
  harmony and mood generate the whole palette, and nothing is emitted until it clears real floors:
  body text **≥ 7:1** contrast, brand colours **≥ 3:1**, and the accent held **≥ 22 ΔE** from both the
  primary and the body text — a perceptual measure, because two colours can share a contrast ratio and
  still be impossible to tell apart. **All 5760 hue/harmony/mood combinations verified — 360 hues x 4
  harmonies x 4 moods — zero failures.**
  Every merchant gets a palette that is legible by construction.
- **The model picks the hue that suits the product** — 25 for roasted coffee, 210 for a productivity
  tool, 45 for beeswax — and the engine turns that single judgement into a complete, accessible system.

**No free CSS anywhere.** Every value is a server-validated enum or a computed colour, so merchant copy
can never reach a style context. Design identity is resolved at generation time and persisted with the
store, so a shop's look never drifts — verified stable across the public catalogue at measurement time. Full design system in
[`docs/DESIGN-DNA.md`](docs/DESIGN-DNA.md).

## Photography that matches what is being sold

Describe a gym-wear brand and the store shows leggings on a person in a gym — not a headline over a
gradient. Every generated store resolves real photographs and **self-hosts them in its own directory**
([`app/imagery.py`](app/imagery.py)), so pages stay fast, need no third-party image CDN, leak no buyer
to anyone, and keep working forever. Resolution runs **in the background**: a paid `create-store`
returns a live store in seconds and the photographs land on the page moments later, because a buyer —
human or agent — should not wait on an image pipeline to find out whether their purchase worked.

The hard part is not fetching a picture, it is fetching the *right* one, and that is enforced rather
than hoped for:

- **The model writes a query per product**, not one per store — "black athletic leggings on a woman",
  not "gym". It also declares the **concrete nouns a photo must contain** to depict that item.
- **Every candidate is scored against the provider's own description of the photo**, so a search that
  returns something off-topic scores zero and is discarded. Highest coverage wins; ties break on the
  store's slug seed, so a store's photography is varied across stores and fixed for any one store.
- **No match means no photograph.** A store selling a template or a service keeps its generative
  texture instead of borrowing a stranger's laptop — the same fail-closed discipline as settlement
  detection. A product's photo travels with its **product id**, so a catalog edit can never slide a
  picture onto the wrong item.
- **Agents see them too.** `feed.json`, the OpenAI product feed and Google Merchant `g:image_link` all
  carry the photograph, and `schema.org/Product` uses the real product shot rather than the brand card —
  a catalog entry with a picture is worth more to a buying agent deciding what to purchase.

- **A vision check before the photo ships.** Caption scoring cannot see what a caption never mentions,
  so the chosen image is shown to a vision model and rejected if it does not depict the product or
  carries someone else's branding — a maple-watch store was offered a flat-lay containing a Casio, a
  Canon and AirPods with none of them named in the alt text, and a hot-sauce store a Tabasco bottle.
  Both refused. Rejection hands the slot to the runner-up rather than emptying it.
- **Stores with nothing photographable get drawn atmosphere, labelled as such.** Software, templates
  and memberships correctly return no image query at all — a stock photo of a stranger's desk would be
  a claim about goods that have no physical form. Those stores get a generated hero instead, which is
  never sent to the stock provider, never used on a product card, held to the same branding rule, and
  disclosed on the page as generated illustration.

Photography is additive and fails open: with no provider key, an outage, or nothing relevant found, the
store is created exactly as before. Photographs are credited to their photographer with a link back, as
their licence requires.

## Buying from Tilla as an agent

Two routes. Direct x402 needs nothing but the endpoint:

```bash
curl -sX POST https://tilla.gudman.xyz/create-store \
  -H 'content-type: application/json' \
  -d '{"description":"what you sell","deliverable":{"kind":"license"}}'
# 402 + a PAYMENT-REQUIRED challenge; pay it and replay the same request
```

Passing `deliverable` is what makes the store sell **real goods** on day one: a delivered order mints
an entitlement and the buyer receives a licence key (or a signed download URL, once a file is
uploaded) instead of a delivery message. `kind` is `text` or `license` here; files go to
`POST /api/stores/<slug>/deliverable` with the `manage_key` as a Bearer token, since multipart cannot
ride a JSON create call. Omit it and the store still works — buyers just get the delivery message
until fulfilment is configured.

### A store can refuse money it cannot honour

Some things cannot be delivered without asking the buyer something first. A due-diligence report has
to know *which token*; an engraving has to know the text. A merchant declares that on the product:

```bash
curl -sX PATCH https://tilla.gudman.xyz/api/merchant/stores/<slug>/products/<id> \
  -H "authorization: Bearer $SESSION" -H 'content-type: application/json' \
  -d '{"buyer_inputs":[{"name":"token_address","label":"Token to research","required":true}]}'
```

The declaration is published in that product's `input_schema` in `feed.json`, so a buying agent reads
it **before** it pays. A buy that omits a required value returns **422 before settlement** — the x402
middleware skips the settle, so no funds move and no order row is created. There is nothing to refund
because nothing was taken.

That inverts the usual failure. A storefront that cannot ask a question will happily take payment for
work it can never start; this one declines the sale instead. The property belongs to the endpoint
rather than to a merchant's discipline, which is the point — it was reported by a merchant whose
store *"can take 0.01 and deliver nothing, because its checkout can't collect a token address."*

A product that declares nothing is unchanged: a bodyless paid `POST` still succeeds, byte for byte,
because an unattended marketplace reviewer sends no body and a `422 needs-params` on a listed service
is functionally a timeout.

### Discovery says whether a store can deliver automatically

Every store publishes a `fulfilment` field in `/discovery/resources` and in its own `feed.json`:

| Value | What a buyer gets |
|---|---|
| `automatic` | goods are attached — paying mints a licence key or a signed download link |
| `merchant` | the buyer receives the merchant's delivery message and the merchant fulfils out of band |

It is **descriptive, not a score**. A consultancy that writes a report by hand fulfils perfectly well,
and calling that "cannot fulfil" would be false. What a buyer is owed is knowing which of the two they
are about to buy, before paying — discovery already published sold count, success rate and trust tier,
and none of them answered it.

Via the OKX task rail, hire ASP **#6961** and pass the **`serviceId`**:

```
Create Storefront → serviceId d2039e5a-9e3c-472f-abcf-329138b3da1d
```

⚠️ `service-list` returns **two** identifiers on each service — `id` (numeric, `35929`) and
`serviceId` (the UUID above). `create-task --service-id` wants the **UUID**. A buyer reported
grabbing `id` because it is the more obvious field name, so read `serviceId` explicitly rather than
the first identifier you see.

## Payment rails (x402)

All four x402 schemes are built, tested, and **settled on-chain** — `exact` (including the agent buy
and create-store flows), `aggr_deferred` (two orders netted into one settle tx), `period`
subscriptions (two periods settled by the OKX subscription contract), and the MPP metered channel
through its full lifecycle including the on-chain close. Every claim below has a re-verified receipt
in [`docs/PROOF-onchain.md`](docs/PROOF-onchain.md):

| Rail | What it is | Status |
|---|---|---|
| `exact` | Fixed-price checkout (human sweeper match + agent EIP-3009 settle) | **Live, proven on-chain** |
| `aggr_deferred` | Batched/deferred settle — the OKX facilitator relayer settles buyer→merchant ~30s later, batching orders | **Proven on-chain**; Tilla auto-detects the facilitator-relayed settlement and finalizes orders |
| `period` | Subscription billing via a Permit2 sidecar + proxy | **Proven on-chain**; two periods settled by the OKX subscription contract, relayed by the facilitator (blocks 66072022, 66072295) |
| MPP metered | Pay-as-you-go metered payment channels (open → voucher → close/settle) | Built + tested + **proven end-to-end on-chain** — channel opened and funded (2 USDT0 into the settlement-agent escrow), voucher signed, metered unit delivered, and the channel **closed on-chain**: 0.1 USDT0 paid to the merchant and the 1.9 remainder refunded to the payer. Receipts in `docs/PROOF-onchain.md` §10, which also records that OKX's settlement-agent API still reports the channel as `CLOSING` and why the chain is cited instead |

Settlement detection is **fail-closed**: an order is only marked delivered against a real, confirmed
on-chain tx hash; during an RPC outage the reaper never voids a paid-but-slow order.

## Architecture

```
app/
  main.py         FastAPI assembly + middleware wiring
  engine.py       LLM store generation + slug-seeded design personas
  render.py       Jinja2 autoescaped theme rendering
  palette.py      colour derived from one hue, with WCAG + perceptual floors
  imagery.py      real photography, verified against the product, self-hosted
  checkout.py     order state machine + on-chain verification
  chain.py        X Layer RPC (balanceOf, receipts, getLogs)
  payment.py      x402 rail (per-store dynamic accepts)
  agentic.py      agent buy, feeds, MCP server, agent card, discovery, reaper
  reconcile.py    aggr_deferred chain-settlement detection
  mpp.py          metered payment channels
  delivery.py     files, signed links, buyer library, license keys
  screening.py    Warden content-screening client
  b2b.py          ERC-8004 owner-gated wholesale tiers
  federation.py   mirror-of-mirrors feed ingest
  growth.py       merchant growth kit (draft → approve → publish; publish is a user action)
themes/           autoescaped Jinja2 store themes
contracts/src/    StoreRegistry.sol (deployed on X Layer, 0x4507…BfCe6)
sdk/python/       tilla-sdk (Python) — installable from source, not published to PyPI
sdk/typescript/   tilla-sdk (TypeScript) — installable from source, not published to npm
docs/             ROADMAP, BUILD, protocol spec, runbooks, on-chain proof
```

- **Stack:** Python 3.12, FastAPI, SQLAlchemy 2 / Alembic (SQLite WAL), Jinja2 (`autoescape=True`),
  `okxweb3-app-x402`, itsdangerous signed tokens, ruff.
- **On-chain index:** `StoreRegistry.sol` binds `keccak256(slug)` → merchant wallet + content hash,
  deployed on X Layer at `0x4507701110396B8B4204698ABf760Dd5418BfCe6` (a public, fund-less index —
  nothing at runtime depends on it).

## On-chain proof

Real settlements on X Layer mainnet, each a single clean transaction, buyer wallet always distinct
from merchant — full receipts in [`docs/PROOF-onchain.md`](docs/PROOF-onchain.md), where the funding
source is stated **per entry** rather than blanket-labelled:

- **Human wallet checkout** — exact-amount sweeper match flips the order to paid and releases delivery.
- **Agent x402 store buy** — one EIP-3009 authorization, facilitator settles, order delivered.
- **Stranger create-store** — an agent pays Tilla's create-store fee on-chain and gets a live store
  back: Tilla earning as an ASP, with a separate receipt for a human paying the current 0.05 USDT0.
- **aggr_deferred** — the facilitator relayer settles batched orders on-chain; Tilla's reconciler
  detects the transfer and finalizes the orders (settling → delivered) from on-chain evidence.

**Most** rail proofs are self-funded arm's-length tests — Tilla's own wallets on both sides — and
those prove *mechanism*, not demand. The batch rail and the MPP channel are not: both were paid by
`0x43ea…af55`, a wallet outside our control that appears nowhere in Tilla's own transaction history.
The proof log says which is which per entry, so anything cited here holds up to being checked.

### Who has actually paid Tilla

Strangers, not just us. Since launch on 2026-07-20, **six wallets outside our control have settled
10.55 USDT0** for Tilla's services — five distinct buyers, since Risingtell paid from two wallets.
Audited by paging the full transaction history of Tilla's `payTo` and cross-checking every order
that settles to a Tilla-owned merchant address:

| External payer | Settled | What they bought |
|---|---|---|
| `0xc385e2df…` (darrel) | 3.15 USDT0 / 6 txs | storefronts, incl. two 1-USDT buys during OKX's listing review; **2 public reviews** |
| `0x43eab1fd…` | 7.10 USDT0 / 8 orders | store products over the batch + metered rails — three settlements batched 2 orders per tx |
| `0xe5581690…` | 0.15 USDT0 / 3 txs | three storefronts (`jersey-fc`, `kit`, `iron`) |
| `0xfc9b58e8…` (AgentForge) | 0.05 USDT0 | `create-store` → `tilla.gudman.xyz/s/agentforge/`; **review 94/100** |
| `0x9f67a13c…`, `0x9ea2d10c…` (Risingtell) | 0.10 USDT0 / 2 txs | two storefronts, paid from two wallets 8 minutes apart; **review 100** |

Reputation follows the money. ASP #6961 carries **seven public reviews from four independent buyer
wallets** — distribution `{5★: 6, 4★: 1}`, `securityRate` **4.86** — and each reviewer appears in the
table above as a wallet that paid first. None was solicited in exchange for anything, and the
marketplace's own self-feedback block means Tilla cannot review itself.

**The 4★ is the one worth reading.** Rouma Desk scored 80 for a price mismatch, then posted a
correction retracting it as a propagation delay, then corrected *that* — "my retraction was the error,
not the original finding" — after re-testing and confirming the defect had been real: Tilla's engine
floored every product at 1.0 USDT on persist while the receipt reported the price asked for. They
amended the review in place rather than adding a third, so the count reflects reviews written, not
reputation padding. The 4★ stands because it was earned. That exchange, and the price bug behind it,
is written up in [`docs/ISSUES.md`](docs/ISSUES.md) alongside Risingtell's finding that identical
descriptions produced different prices.

**Hired through OKX's own task rail, not just the endpoint.** Six user tasks have been designated to
#6961. **Four completed for three client agents Tilla does not own** — darrel (#1757) twice, for a
coffee shop at 1 USDT and a candle shop at 0.05; Abiola/AgentForge (#5632); and Risingtell (#6034) —
each running the full connect → x402 agreement → payment → deliverable → `[x402 Job Completed]`
loop, returning a live store. That is **1.15 USDT0 of the 10.55 above**, arriving through the task
rail rather than a direct endpoint call — a channel, not extra revenue, and not double-counted. A
fifth completed task was Tilla's own operator agent (#4844) calling its own service, so it proves
the rail works and nothing about demand; it is excluded from the three-client count. A sixth is
still open. Verify with `onchainos agent tasks --agent-id 6961`.

**A counter is not a receipt.** The marketplace `soldCount` includes *served* orders including
unsettled ones — one wallet alone ran **36 orders that were cancelled with zero on-chain
settlement**, probing the rails without ever paying. Tilla's revenue claims cite settlement
receipts, never the counter, and the 10.55 USDT0 above excludes every unpaid probe. It also excludes
pre-launch traffic: `payTo` is shared with an earlier project, so only transfers from 2026-07-20
onward are counted as Tilla's.

**Tilla also buys.** Most entries in an agent marketplace only sell. Tilla hires other agents and
pays them over x402: a Warden security screen before every store goes live (0.1 USDT0 — but Warden
is operator-owned, so that proves the mechanism, not third-party demand), and — closing exactly that
gap — three purchases from agents Tilla does **not** own: Argus (#5246), VigilOK (#6032) and
Oddsmith (#9639), 0.01 USDT0 each, all settled on X Layer, all delivering real responses. All three
are run by one operator, `0x9ea2d10c…` — the same Risingtell who bought two storefronts from Tilla
and reviewed it 100. So the relationship runs **both ways**: they paid Tilla for a service they
needed, Tilla paid them for services it needed, and every leg settled on-chain. One purchase is
independently verifiable and verifies: Oddsmith quoted OKB at 86.3605 against OKX's own feed at
86.2412 the same minute, with exact conversion arithmetic. Receipts as
[`docs/PROOF-onchain.md` §12](docs/PROOF-onchain.md).

## Development

```sh
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest -q
```

Migrations: `alembic upgrade head`. The local repo is the source of truth; the VPS is a deploy target
(full manifest staging + a short service stop + migration + smoke test), never edited directly.

## SDKs

- **Python** — [`sdk/python/`](sdk/python/) (`tilla-sdk`): sync, typed, dependency-light client for
  discovery/search, feeds, the agent card, human checkout, the MCP tool surface, and the x402 pay paths.
  The caller supplies a signer hook; the SDK never reads, defaults, bundles, or logs a key.
- **TypeScript** — [`sdk/typescript/`](sdk/typescript/): same client surface; the signer hook maps to an
  EIP-1193 provider or a raw signer callback.

## Documentation

| Doc | What |
|---|---|
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Full feature map + verified rail constraints |
| [`docs/BUILD.md`](docs/BUILD.md) | Committed build scope, pinned toolchain, per-module acceptance |
| [`docs/PROOF-onchain.md`](docs/PROOF-onchain.md) | On-chain settlement proof log (real tx receipts) |
| [`docs/specs/tilla-protocol-v1.md`](docs/specs/tilla-protocol-v1.md) | The open feed / agent-card / 402 conventions |
| [`docs/runbooks/`](docs/runbooks/) | Rail enablement, custom domains, photography, on-chain marketplace ops |
| [`docs/DESIGN-DNA.md`](docs/DESIGN-DNA.md) | Why no two stores look alike, and how it is enforced |
| [`docs/VISION.md`](docs/VISION.md) | Forward "commerce OS" design + what's built (M15–M18) |
| [`docs/ISSUES.md`](docs/ISSUES.md) | Known defects with reproductions — including the first customer-reported bug and its fix |

## Security & invariants

- Non-custodial: `pay_to` is always the merchant; every write that moves a merchant's or buyer's
  funds is user-signed. The two server-signed paths (EAS attester, Warden hire) spend only Tilla's
  own balance and are flag-gated.
- Secrets never in the repo (`.env` lives on the VPS, chmod 600); test fixtures use fake creds.
- Autoescape is mandated in the theme loader, not the theme.
- Every settlement transition is idempotent and requires a confirmed on-chain tx hash.

## Licence

[MIT](LICENSE) © 2026 Ridwan Nurudeen.

The product photographs Tilla resolves at runtime are **not** part of this repository — they are
fetched per store, self-hosted on the deployment, and credited to their photographer with a link
back as their provider's licence requires. Only Tilla's own brand assets are committed here.
