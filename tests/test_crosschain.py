"""M18.1 cross-chain foundation: the ChainConfig registry, per-order chain pin,
and the wrong-chain / wrong-asset fund-loss guards.

The golden gate is byte-identical 402 challenge + checkout behaviour after the
registry refactor. The adversarial guards prove a tx on any other chainId, of any
other asset, can NEVER confirm or refund an order — verification is pinned to the
chain recorded on the order at creation, and no cross-chain RPC lookup ever runs.
All RPC is respx-mocked httpx — no real network, no funds.
"""

import asyncio
import json
import secrets

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import chain, checkout, config, payment
from app.db import SessionLocal
from app.models import Order, Product, Store
from app.payment import (
    CANONICAL_CHAIN,
    CHAINS,
    PAYMENT_ASSET,
    PAYMENT_EIP712_NAME,
    PAYMENT_SCHEME,
    ChainConfig,
    build_fee_payment_option,
    build_payment_option,
    build_store_payment_options,
    chain_enabled,
    chain_for,
    enabled_chains,
    load_payment_rail,
    probe_supported,
    set_supported_snapshot,
    supported_snapshot,
)

client = TestClient(main.app)

PAY_TO = "0x" + "c" * 40
BUYER = "0x" + "b" * 40

# A synthetic SECOND chain used ONLY to prove the per-order pin adversarially. It
# is injected into the registry via monkeypatch for the guard tests; it is NEVER a
# real settlement claim (18.2 gates a real second chain on the /supported probe).
CHAIN_B = ChainConfig(
    chain_id=8453,
    caip2="eip155:8453",
    rpc_url="https://rpc.chain-b.test",
    ws_url="wss://ws.chain-b.test",
    asset="0x" + "b7" * 20,
    asset_symbol="USDC",
    decimals=6,
    eip712_name="USD Coin",
    eip712_version="2",
    explorer_tx_base="https://chain-b.test/tx/",
    canonical=False,
)


# --------------------------------------------------------- registry shape
def test_single_chain_registry_default():
    assert list(CHAINS.keys()) == ["eip155:196"]
    c = CANONICAL_CHAIN
    assert c.chain_id == 196
    assert c.caip2 == "eip155:196"
    assert c.rpc_url == config.RPC_URL
    # PAYMENT_ASSET and config.USDT0 are the same address — the one USDT0 contract.
    assert c.asset == PAYMENT_ASSET == config.USDT0
    assert c.asset_symbol == "USDT"
    assert c.decimals == 6
    assert c.eip712_name == PAYMENT_EIP712_NAME
    assert c.eip712_version == "1"
    assert c.explorer_tx_base == config.OKLINK_TX_BASE
    assert c.ws_url == "wss://ws.xlayer.tech"
    assert c.canonical is True


def test_chain_for_pins_and_rejects_unknown():
    assert chain_for("eip155:196") is CANONICAL_CHAIN
    # Never falls back to canonical — an unknown network is a hard error, so a
    # mis-recorded order can never be silently re-pointed at a different ledger.
    try:
        chain_for("eip155:8453")
    except KeyError:
        pass
    else:
        raise AssertionError("chain_for must raise for an unknown network")


# --------------------------------------------------------- golden gate
def _opt_dict(opt) -> dict:
    return {
        "scheme": opt.scheme,
        "network": opt.network,
        "pay_to": opt.pay_to,
        "max_timeout_seconds": opt.max_timeout_seconds,
        "price": {
            "amount": opt.price.amount,
            "asset": opt.price.asset,
            "extra": dict(opt.price.extra),
        },
    }


def test_402_challenge_byte_identical():
    """The exact accepts-entries the 402 challenge is built from are byte-for-byte
    what they were before the registry refactor (pinned snapshot)."""
    rail = load_payment_rail({"PAY_TO_ADDRESS": PAY_TO})

    expected_create = (
        '{"max_timeout_seconds":300,"network":"eip155:196",'
        '"pay_to":"0xcccccccccccccccccccccccccccccccccccccccc",'
        '"price":{"amount":"1000000",'
        '"asset":"0x779ded0c9e1022225f8e0630b35a9b54be713736",'
        '"extra":{"name":"USD₮0","version":"1"}},"scheme":"exact"}'
    )
    got = json.dumps(
        _opt_dict(build_payment_option(rail)),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert got == expected_create

    # Platform-fee options: same shape, only the amount differs.
    assert _opt_dict(build_fee_payment_option(rail, "500000"))["price"]["amount"] == (
        "500000"
    )

    # The store accepts list is [exact] with the flag off (byte-identical to today);
    # its network is the canonical rail, its resolvers stay dynamic.
    store_opts = build_store_payment_options(rail)
    assert len(store_opts) == 1
    assert store_opts[0].scheme == "exact"
    assert store_opts[0].network == "eip155:196"


def test_live_buy_still_402(make_store):
    make_store(slug="cc-live", pay_to=PAY_TO)
    r = client.post("/s/cc-live/buy")
    assert r.status_code == 402
    assert r.content == b'{"detail":"payment required"}'


# --------------------------------------------------------- per-order pin
def _product_of(session, store_id):
    return session.scalar(select(Product).where(Product.store_id == store_id))


def test_open_order_records_canonical_network(make_store):
    sid = make_store(slug="cc-net", pay_to=PAY_TO)
    with SessionLocal() as s:
        store = s.get(Store, sid)
        order = checkout.create_order(s, store, _product_of(s, sid))
        s.commit()
        assert order.network == "eip155:196"


def test_open_order_pins_original_chain(make_store, monkeypatch):
    """An order verifies against the chain recorded at creation, re-derived from the
    order row — a registry/env change mid-flight cannot re-point an open order."""
    sid = make_store(slug="cc-pin", pay_to=PAY_TO)
    with SessionLocal() as s:
        store = s.get(Store, sid)
        order = checkout.create_order(s, store, _product_of(s, sid))
        s.commit()
        oid = order.id

    # Mid-flight: a second chain appears in the registry. The open order stays
    # pinned to its recorded canonical network, so verification uses X Layer's RPC.
    monkeypatch.setitem(CHAINS, CHAIN_B.caip2, CHAIN_B)
    with SessionLocal() as s:
        o = s.get(Order, oid)
        assert o.network == "eip155:196"
        assert payment.chain_for(o.network).rpc_url == config.RPC_URL


# --------------------------------------------------------- wrong-chain guards
def _log(to_addr, value, tx_hash, contract, log_index=0, block=100):
    return {
        "address": contract,
        "topics": [
            config.TRANSFER_TOPIC,
            chain.pad_address(BUYER),
            chain.pad_address(to_addr),
        ],
        "data": hex(value),
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
        "blockNumber": hex(block),
    }


def _receipt(logs, status="0x1", block=100):
    return {"status": status, "blockNumber": hex(block), "logs": logs}


def _rpc_handler(receipts):
    def handler(request):
        body = json.loads(request.content)
        method = body["method"]
        result = None
        if method == "eth_getTransactionReceipt":
            result = receipts.get(body["params"][0].lower())
        elif method == "eth_blockNumber":
            result = hex(10**9)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    return handler


def _order_on(session, store_id, network, expected, pay_to=PAY_TO):
    product = _product_of(session, store_id)
    order = Order(
        id=secrets.token_hex(8),
        store_id=store_id,
        product_id=product.id,
        pay_to=pay_to,
        network=network,
        amount_micro=expected,
        expected_micro=expected,
        status="pending",
    )
    session.add(order)
    session.flush()
    return order


@respx.mock
def test_wrong_chain_receipt_never_confirms(make_store, monkeypatch):
    """A receipt that exists only on chain A can never confirm an order pinned to
    chain B: verification queries ONLY B's RPC (not-found there), and A's RPC is
    never contacted — no cross-chain search that would legitimize a wrong-chain send.
    """
    monkeypatch.setitem(CHAINS, CHAIN_B.caip2, CHAIN_B)
    sid = make_store(slug="cc-wrongchain", pay_to=PAY_TO)
    tx = "0x" + secrets.token_hex(32)
    exp = 9_000_123

    # The tx resolves on the CANONICAL chain (A) only; chain B has never seen it.
    route_a = respx.post(config.RPC_URL).mock(
        side_effect=_rpc_handler(
            {tx.lower(): _receipt([_log(PAY_TO, exp, tx, CANONICAL_CHAIN.asset)])}
        )
    )
    route_b = respx.post(CHAIN_B.rpc_url).mock(side_effect=_rpc_handler({}))

    with SessionLocal() as s:
        order = _order_on(s, sid, CHAIN_B.caip2, exp)
        s.commit()
        try:
            checkout.verify_txhash(s, order, tx)
        except checkout.TxVerificationError as e:
            assert "not found" in str(e)
        else:
            raise AssertionError("a chain-A receipt must not confirm a chain-B order")
        s.refresh(order)
        assert order.status == "pending"

    assert route_b.called, "the order's own chain RPC must be queried"
    assert not route_a.called, "the canonical RPC must NEVER be searched cross-chain"


@respx.mock
def test_unregistered_network_order_skipped_not_reorged(make_store):
    """A detected order pinned to a network that has vanished from the registry
    must be SKIPPED by the promote stage (stays detected — operator problem),
    never marked reorged/canceled, and must not abort promotion of other orders."""
    sid = make_store(slug="cc-orphan", pay_to=PAY_TO)
    tx_good = "0x" + secrets.token_hex(32)
    exp = 9_000_123
    respx.post(config.RPC_URL).mock(
        side_effect=_rpc_handler(
            {
                tx_good.lower(): _receipt(
                    [_log(PAY_TO, exp, tx_good, CANONICAL_CHAIN.asset)]
                )
            }
        )
    )
    with SessionLocal() as s:
        orphan = _order_on(s, sid, "eip155:999999", exp)
        orphan.status = "detected"
        orphan.tx_hash = "0x" + secrets.token_hex(32)
        orphan.block_number = 100
        good = _order_on(s, sid, CANONICAL_CHAIN.caip2, exp + 1)
        good.status = "detected"
        good.tx_hash = tx_good
        good.block_number = 100
        good.paid_micro = exp + 1
        s.commit()
        checkout._promote_matured(
            s, CANONICAL_CHAIN.caip2, head=100 + config.CONFIRMATIONS
        )
        s.commit()
        s.refresh(orphan)
        s.refresh(good)
        assert orphan.status == "detected"  # skipped, not reorged/canceled
        assert good.status in ("confirmed", "delivered")  # promotion not aborted


@respx.mock
def test_wrong_asset_ignored(make_store):
    """Same chain, different token contract: a Transfer of the wrong asset to the
    right recipient for the exact amount is skipped, so it never confirms."""
    sid = make_store(slug="cc-wrongasset", pay_to=PAY_TO)
    tx = "0x" + secrets.token_hex(32)
    exp = 9_000_123
    other_token = "0x" + "1f" * 20

    respx.post(config.RPC_URL).mock(
        side_effect=_rpc_handler(
            {tx.lower(): _receipt([_log(PAY_TO, exp, tx, other_token)])}
        )
    )
    with SessionLocal() as s:
        order = _order_on(s, sid, CANONICAL_CHAIN.caip2, exp)
        s.commit()
        try:
            checkout.verify_txhash(s, order, tx)
        except checkout.TxVerificationError as e:
            assert "no USDT0 transfer" in str(e)
        else:
            raise AssertionError("a wrong-asset transfer must not confirm the order")
        s.refresh(order)
        assert order.status == "pending"


@respx.mock
def test_right_asset_on_pinned_chain_confirms(make_store):
    """Control: the correct asset to the recipient for the exact amount, on the
    order's own chain, DOES confirm — proving the guards reject only the bad cases.
    """
    sid = make_store(slug="cc-good", pay_to=PAY_TO)
    tx = "0x" + secrets.token_hex(32)
    exp = 9_000_123

    respx.post(config.RPC_URL).mock(
        side_effect=_rpc_handler(
            {tx.lower(): _receipt([_log(PAY_TO, exp, tx, CANONICAL_CHAIN.asset)])}
        )
    )
    with SessionLocal() as s:
        order = _order_on(s, sid, CANONICAL_CHAIN.caip2, exp)
        s.commit()
        checkout.verify_txhash(s, order, tx)
        s.refresh(order)
        assert order.status in ("confirmed", "overpaid", "delivered", "paid")


# ==================================================================== 18.2
# /supported probe gate + per-chain accepts abstraction. CHAIN_B is the synthetic
# second chain; it becomes a real accepts entry ONLY when its flag is on AND the
# probe snapshot lists (exact, its caip2) — never a real settlement claim.


@pytest.fixture
def clean_snapshot():
    """Reset the process-global probe snapshot around a test (it is cached for the
    process, so a test that sets it must not leak into the next)."""
    saved = supported_snapshot() if payment._SUPPORTED_SNAPSHOT is not None else None
    payment._SUPPORTED_SNAPSHOT = None
    yield
    payment._SUPPORTED_SNAPSHOT = frozenset(saved) if saved is not None else None


class _Kind:
    def __init__(self, scheme, network):
        self.scheme = scheme
        self.network = network


class _SupportedResp:
    def __init__(self, kinds):
        self.kinds = kinds


class _FakeFacilitator:
    def __init__(self, kinds=None, boom=False):
        self._kinds = kinds or []
        self._boom = boom

    def get_supported(self):
        if self._boom:
            raise httpx.ConnectError("probe down")
        return _SupportedResp(self._kinds)


def test_probe_caches_scheme_network_pairs(clean_snapshot):
    snap = probe_supported(
        _FakeFacilitator([_Kind("exact", "eip155:196"), _Kind("period", "eip155:196")])
    )
    assert snap == frozenset({("exact", "eip155:196"), ("period", "eip155:196")})
    assert supported_snapshot() == snap


def test_probe_failure_falls_back_196(clean_snapshot):
    """A probe that raises must NEVER crash — it caches an empty snapshot, so the
    canonical rail stays live (grandfathered) and no other chain is advertised."""
    snap = probe_supported(_FakeFacilitator(boom=True))
    assert snap == frozenset()
    # 196 is enabled regardless of the (empty) snapshot; a second chain is not.
    assert chain_enabled(CANONICAL_CHAIN) is True
    assert enabled_chains() == [CANONICAL_CHAIN]


def test_402_never_lists_unprobed_chain(clean_snapshot, monkeypatch):
    """Flag ON but the probe snapshot does NOT list the chain ⇒ the accepts list is
    unchanged (196-only), the chain is not advertised."""
    monkeypatch.setitem(CHAINS, CHAIN_B.caip2, CHAIN_B)
    monkeypatch.setenv(f"TILLA_CHAIN_{CHAIN_B.chain_id}_ENABLED", "1")
    # Snapshot lists only the canonical scheme/network — NOT (exact, chain B).
    set_supported_snapshot([("exact", "eip155:196")])

    assert chain_enabled(CHAIN_B) is False
    assert enabled_chains() == [CANONICAL_CHAIN]

    rail = load_payment_rail({"PAY_TO_ADDRESS": PAY_TO})
    opts = build_store_payment_options(rail)
    assert [o.network for o in opts] == ["eip155:196"]


def test_402_lists_chain_only_when_flag_and_probe_agree(clean_snapshot, monkeypatch):
    """Flag ON AND the probe lists (exact, chain B) ⇒ exactly one extra exact
    accepts entry on chain B, sharing the dynamic per-store resolvers, carrying
    chain B's asset + EIP-712 domain. This is the ONLY path a second chain appears."""
    monkeypatch.setitem(CHAINS, CHAIN_B.caip2, CHAIN_B)
    monkeypatch.setenv(f"TILLA_CHAIN_{CHAIN_B.chain_id}_ENABLED", "1")
    set_supported_snapshot([("exact", "eip155:196"), ("exact", CHAIN_B.caip2)])

    assert chain_enabled(CHAIN_B) is True
    rail = load_payment_rail({"PAY_TO_ADDRESS": PAY_TO})
    opts = build_store_payment_options(rail)
    assert [o.network for o in opts] == ["eip155:196", CHAIN_B.caip2]
    assert opts[1].scheme == PAYMENT_SCHEME
    # dynamic (callable) resolvers, exactly like the canonical store entry
    assert callable(opts[1].pay_to)
    assert callable(opts[1].price)


def test_402_chain_needs_the_flag_too(clean_snapshot, monkeypatch):
    """Probe lists the chain but the flag is OFF ⇒ still not advertised (both gates
    required, INV-1)."""
    monkeypatch.setitem(CHAINS, CHAIN_B.caip2, CHAIN_B)
    monkeypatch.delenv(f"TILLA_CHAIN_{CHAIN_B.chain_id}_ENABLED", raising=False)
    set_supported_snapshot([("exact", "eip155:196"), ("exact", CHAIN_B.caip2)])

    assert chain_enabled(CHAIN_B) is False
    rail = load_payment_rail({"PAY_TO_ADDRESS": PAY_TO})
    assert [o.network for o in build_store_payment_options(rail)] == ["eip155:196"]


@respx.mock
def test_sentinel_skips_settle_per_chain(clean_snapshot, monkeypatch):
    """The dead-store sentinel rule holds on EVERY configured chain's store entry:
    an unknown/non-live store path resolves to Tilla's own sentinel payTo + price
    '1' (never a raise, never a merchant settle), so a dead store can never settle
    on any advertised chain."""
    monkeypatch.setitem(CHAINS, CHAIN_B.caip2, CHAIN_B)
    monkeypatch.setenv(f"TILLA_CHAIN_{CHAIN_B.chain_id}_ENABLED", "1")
    set_supported_snapshot([("exact", "eip155:196"), ("exact", CHAIN_B.caip2)])
    rail = load_payment_rail({"PAY_TO_ADDRESS": PAY_TO})

    class _Ctx:
        path = "/s/does-not-exist/buy"

    for opt, cfg in zip(build_store_payment_options(rail), enabled_chains()):
        pay_to = asyncio.run(opt.pay_to(_Ctx()))
        price = asyncio.run(opt.price(_Ctx()))
        assert pay_to == PAY_TO  # sentinel = Tilla's own rail payTo, not a merchant
        assert price.amount == "1"  # sentinel price
        assert price.asset == cfg.asset  # each chain names its own settle asset


# ==================================================================== 18.4
# Wrong-chain refusal hardening: a valid-but-nonexistent hash returns the "not
# found" path with a buyer-facing hint naming the pinned chain, and NEVER triggers
# a lookup on any other RPC (a cross-chain search would legitimize wrong-chain sends).


@respx.mock
def test_not_found_hint_names_pinned_chain(make_store):
    """A syntactically valid hash that does not resolve on the order's own chain
    returns 'not found' with a hint naming the pinned chainId — so the buyer learns
    they may have paid on the wrong network, without any cross-chain lookup."""
    sid = make_store(slug="cc-hint", pay_to=PAY_TO)
    tx = "0x" + secrets.token_hex(32)
    respx.post(config.RPC_URL).mock(side_effect=_rpc_handler({}))  # tx unknown here
    with SessionLocal() as s:
        order = _order_on(s, sid, CANONICAL_CHAIN.caip2, 9_000_123)
        s.commit()
        try:
            checkout.verify_txhash(s, order, tx)
        except checkout.TxVerificationError as e:
            assert "not found" in str(e)
            assert str(CANONICAL_CHAIN.chain_id) in str(e)  # names the pinned chain
        else:
            raise AssertionError("a nonexistent tx must not confirm the order")


@respx.mock
def test_no_cross_chain_rpc_search(make_store, monkeypatch):
    """Only the order's OWN chain RPC host is ever contacted for verification. A
    second chain's RPC is wired up but must receive ZERO requests — verification
    never fans out across chains looking for the hash."""
    monkeypatch.setitem(CHAINS, CHAIN_B.caip2, CHAIN_B)
    sid = make_store(slug="cc-nofanout", pay_to=PAY_TO)
    tx = "0x" + secrets.token_hex(32)
    route_own = respx.post(config.RPC_URL).mock(side_effect=_rpc_handler({}))
    route_other = respx.post(CHAIN_B.rpc_url).mock(side_effect=_rpc_handler({}))
    with SessionLocal() as s:
        order = _order_on(s, sid, CANONICAL_CHAIN.caip2, 9_000_123)
        s.commit()
        try:
            checkout.verify_txhash(s, order, tx)
        except checkout.TxVerificationError:
            pass
    assert route_own.call_count == 1  # exactly one RPC host, one lookup
    assert not route_other.called


@respx.mock
def test_wrong_recipient_same_chain_ignored(make_store):
    """A USDT0 transfer of the exact amount on the right chain but to a DIFFERENT
    recipient never confirms the order (the topic-2 recipient pin)."""
    sid = make_store(slug="cc-wrongto", pay_to=PAY_TO)
    tx = "0x" + secrets.token_hex(32)
    exp = 9_000_123
    other_to = "0x" + "a" * 40
    respx.post(config.RPC_URL).mock(
        side_effect=_rpc_handler(
            {tx.lower(): _receipt([_log(other_to, exp, tx, CANONICAL_CHAIN.asset)])}
        )
    )
    with SessionLocal() as s:
        order = _order_on(s, sid, CANONICAL_CHAIN.caip2, exp)
        s.commit()
        try:
            checkout.verify_txhash(s, order, tx)
        except checkout.TxVerificationError as e:
            assert "no USDT0 transfer" in str(e)
        else:
            raise AssertionError("a transfer to another recipient must not confirm")
        s.refresh(order)
        assert order.status == "pending"
