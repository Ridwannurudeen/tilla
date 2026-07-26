# Tilla

**Describe what you sell → get a live, branded crypto storefront that sells to humans _and_ autonomous agents.**

Tilla is a storefront-studio ASP (Agent Service Provider) on the OKX agent marketplace. A merchant
writes one prompt; Tilla generates the brand, copy, and product content, screens it, and publishes a
live store with non-custodial crypto checkout on **OKX X Layer** (chainId 196, USDT0). The same store
is simultaneously a human web checkout and a machine-payable surface — feeds, an MCP server, an agent
card, and x402 pay endpoints — so an autonomous agent can discover and buy from it the same way a
person can.

- **Live:** https://tilla.gudman.xyz — example store: https://tilla.gudman.xyz/s/invoice-flow/
- **OKX marketplace:** listed as ASP **#6961** with **six services** — three platform endpoints
  (x402-gated `create-store` 0.05, `upgrade-store` 0.03, `add-product` 0.01) plus three Tilla-built
  storefronts listed as their own buyable services, so every store Tilla makes becomes marketplace supply
- **Settlement:** X Layer mainnet (chainId 196), USDT0 `0x779ded0c9e1022225f8e0630b35a9b54be713736`

## Why it's different

Most storefront builders sell to people. Tilla sells to **people and agents** from one build:

- **Non-custodial by design.** Funds settle buyer → merchant directly on-chain. Tilla never holds,
  custodies, or moves merchant or buyer money — every transfer of *their* funds is signed by them.
  Stated precisely, because it is greppable: Tilla does hold two of its own keys behind feature
  flags, and both spend only Tilla's own balance — the EAS attester broadcasts receipt attestations
  (`app/attest.py`, costs Tilla gas) and the Warden hire signs Tilla's own x402 payments when it buys
  a screening (`app/warden_hire.py`). Neither can touch a merchant's or buyer's funds.
- **Dual-sided commerce.** A human uses wallet-connect checkout; an agent pays the same store over
  **x402** (EIP-3009 authorization) with no UI. Discovery is machine-native: `feed.json`, per-store MCP
  tools, an agent card, and a `/discovery` mirror.
- **Screened content.** Every LLM-generated store and marketing asset passes Warden content screening
  before it goes live, and themes render through an autoescaped loader (a third-party theme can never
  opt out of escaping).

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
  engine.py       LLM store generation (prompt → brand/copy/products)
  render.py       Jinja2 autoescaped theme rendering
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
contracts/        StoreRegistry.sol (deployed on X Layer, 0x4507…BfCe6)
sdk/python/       tilla-sdk (Python) — shipped
sdk/typescript/   TypeScript SDK
docs/             ROADMAP, BUILD, protocol spec, runbooks, on-chain proof
```

- **Stack:** Python 3.12, FastAPI, SQLAlchemy 2 / Alembic (SQLite WAL), Jinja2 (`autoescape=True`),
  `okxweb3-app-x402`, itsdangerous signed tokens, ruff.
- **On-chain index:** `StoreRegistry.sol` binds `keccak256(slug)` → merchant wallet + content hash,
  deployed on X Layer at `0x4507701110396B8B4204698ABf760Dd5418BfCe6` (a public, fund-less index —
  nothing at runtime depends on it).

## On-chain proof

Real, self-funded, arm's-length settlements on X Layer mainnet (buyer wallet distinct from merchant),
each a single clean transaction — full receipts in [`docs/PROOF-onchain.md`](docs/PROOF-onchain.md):

- **Human wallet checkout** — exact-amount sweeper match flips the order to paid and releases delivery.
- **Agent x402 store buy** — one EIP-3009 authorization, facilitator settles, order delivered.
- **Stranger create-store** — an agent pays Tilla's create-store fee and gets a live store back, so
  Tilla earns as an ASP. That settle was 1 USDT0, the fee at the time; the fee is 0.05 USDT0 today,
  and a human paying the current 0.05 is a separate, later proof.
- **aggr_deferred** — the facilitator relayer settles batched orders on-chain; Tilla's reconciler
  detects the transfer and finalizes the orders (settling → delivered) from on-chain evidence.

Self-trades are labeled as self-trades everywhere; nothing here claims organic external demand.

## Development

```sh
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest -q                # 1017 tests
```

Migrations: `alembic upgrade head`. The local repo is the source of truth; the VPS is a deploy target
(file-by-file scp + `systemctl restart tilla-api` + smoke test), never edited directly.

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
| [`docs/runbooks/`](docs/runbooks/) | Rail enablement, custom domains, on-chain marketplace ops |
| [`docs/VISION.md`](docs/VISION.md) | Forward "commerce OS" design + what's built (M15–M18) |

## Security & invariants

- Non-custodial: `pay_to` is always the merchant; every write that moves a merchant's or buyer's
  funds is user-signed. The two server-signed paths (EAS attester, Warden hire) spend only Tilla's
  own balance and are flag-gated.
- Secrets never in the repo (`.env` lives on the VPS, chmod 600); test fixtures use fake creds.
- Autoescape is mandated in the theme loader, not the theme.
- Every settlement transition is idempotent and requires a confirmed on-chain tx hash.
