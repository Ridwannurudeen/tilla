# Tilla contracts — StoreRegistry (DEPLOYED on X Layer mainnet)

A minimal Foundry project for `StoreRegistry.sol`: an owner-gated, fund-less on-chain
index binding a Tilla store slug (`storeId = keccak256(slug)`) to its merchant wallet
and a content metadata hash.

> **Status: DEPLOYED (2026-07-23) on X Layer mainnet (chainId 196).**
> - Address: `0x4507701110396B8B4204698ABf760Dd5418BfCe6`
> - Deploy tx: `0x0ce676bc…8cc27056` (see explorer for the full hash)
> - Owner/registrar: `0x03d134c36425F312aEFE28Ab08BF471A61cf4ebb` (operator registrar for the demo)
> - Compiler: solc 0.8.24, optimizer on, runs 200 (matches `foundry.toml`)
> - 5 live stores registered on-chain (billable, invoice-flow, highland-roast, sync, leaf-ember), read-back verified.
>
> Nothing at runtime in the Tilla backend depends on it — it remains a public,
> verifiable index only. Source-verification on OKLink is an optional follow-up
> (paste this source, solc 0.8.24 / optimizer 200, constructor arg = the registrar address).

## Layout

- `src/StoreRegistry.sol` — the contract (`register`, `updateMetadata`, `storeId`,
  events `StoreRegistered` / `StoreMetadataUpdated`; owner set once in the constructor).
- `script/Deploy.s.sol` — user-run deploy script (reads `DEPLOYER_KEY` from env).
- `test/StoreRegistry.t.sol` — register / update / onlyOwner-revert / event tests.
- `foundry.toml` — solc 0.8.24, `xlayer` / `xlayer_testnet` RPC endpoints.

## Build & test (best-effort — requires Foundry)

Foundry is **not** verified installed on the build machine, so `forge build`/`forge
test` are best-effort and are **never a CI gate** (the contract ships as prepared
source, not as a live dependency of anything). To run locally, install Foundry
(<https://book.getfoundry.sh/getting-started/installation>), then from `contracts/`:

```sh
forge install foundry-rs/forge-std   # one-time: fetches lib/forge-std (test/script deps)
forge build
forge test -vvv
```

## Deploy (USER-RUN ONLY — approval-gated, spends gas)

```sh
# mainnet X Layer (chainId 196)
DEPLOYER_KEY=0x<key> forge script script/Deploy.s.sol:Deploy \
  --rpc-url https://rpc.xlayer.tech --broadcast

# optional: a distinct registrar/owner (defaults to the deployer)
DEPLOYER_KEY=0x<key> REGISTRAR=0x<registrar> forge script script/Deploy.s.sol:Deploy \
  --rpc-url https://rpc.xlayer.tech --broadcast
```

Recommended: dry-run on testnet 1952 first (`--rpc-url https://testrpc.xlayer.tech/terigon`,
fund the deployer from <https://web3.okx.com/xlayer/faucet>) before mainnet.

## Verify on OKLink (after deploy)

```sh
forge verify-contract <DEPLOYED_ADDRESS> src/StoreRegistry.sol:StoreRegistry \
  --chain-id 196 \
  --verifier oklink \
  --verifier-url https://www.oklink.com/api/v5/explorer/contract/verify-source-code-plugin \
  --constructor-args $(cast abi-encode "constructor(address)" <REGISTRAR_ADDRESS>)
```

(OKLink's verify endpoint/flags can drift — confirm the current OKLink X Layer
contract-verification flow before running. See BUILD.md §M11.)
