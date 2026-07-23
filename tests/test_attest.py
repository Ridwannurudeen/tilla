"""M11 EAS attester tests — NO network, NO signing, NO funds, NO on-chain.

The pure encoding (schema UID + payload ABI + Attested topic/UID) is asserted against
the Spike-4 precompute. The worker's web3 boundary is fully mocked (the module's
chain_id / getSchema / send / receipt seams are patched; an ephemeral in-memory
MagicMock stands in for the Web3 client), so a signed transaction is NEVER built or
broadcast. Dormancy is asserted three ways: importing app.attest loads no web3, the
default factory is None, and a flag-off deliver leaves attest_status 'none' while the
order still delivers.
"""

import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak
from fastapi.testclient import TestClient

import app.main as main
from app import agentic, attest, checkout, config
from app.db import SessionLocal
from app.models import Delivery, Order, Store

client = TestClient(main.app)

# Spike-4 precomputes (see scratch attest_receipt.py) — pinned so any drift in the
# schema string / field order / event signature fails loudly. Built by concat so the
# literal 0x+64hex never appears verbatim (the config.py TRANSFER_TOPIC convention).
SCHEMA_UID_HEX = (
    "0x" + "80bddc2e0248d8729a0925d8ddfea352196d546b22e1f0dcbfc9ddea6e79fd98"
)
ATTESTED_TOPIC0_HEX = (
    "0x" + "8bf46bf4cfd674fa735a3d63ec1c9ad4153f033c290341f3a588b75685141b35"
)
ATTEST_SELECTOR = "0xf17325e7"

PAYER = "0x" + "33" * 20
PAY_TX = "0x" + "11" * 32
# The delivered payload a seeded order carries + its pinned sha256 (0x + 64 hex) — the
# contentHash the attester binds into the receipt.
DELIV_PAYLOAD = "delivered-secret-xyz"
CONTENT_HASH = "0x" + "c68a06c58ac560c4c5f35f6c7cea9d66897832f46caeea878770e7186e43249a"
ATTEST_TX = "0x" + "ee" * 32
ATTEST_TX2 = "0x" + "dd" * 32
UID = "0x" + "ab" * 32
NONCE = "0x" + "1" * 64
ATTEST_NONCE = 7


def _prep(tx=ATTEST_TX, nonce=ATTEST_NONCE):
    """A _prepare_attestation stand-in: returns (signed, tx_hash, nonce) and honours a
    pinned nonce kwarg (the reconcile re-broadcast path), so nothing is ever built."""

    def _fn(*a, **k):
        pinned = k.get("nonce")
        return object(), tx, pinned if pinned is not None else nonce

    return _fn


@pytest.fixture(autouse=True)
def _reset_attest_state():
    attest._reset_state()
    yield
    attest._reset_state()


# ============================================================================
# Spies + fakes (web3 boundary)
# ============================================================================
class _Spy:
    """Records calls; returns a value / calls a callable / raises an exception."""

    def __init__(self, side=None):
        self.calls = []
        self._side = side

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        if isinstance(self._side, BaseException):
            raise self._side
        return self._side(*a, **k) if callable(self._side) else self._side

    @property
    def count(self):
        return len(self.calls)


def _receipt(status=1, uid=UID):
    logs = []
    if uid is not None:
        # data = uid (32) followed by schemaUID padding — _uid_from_receipt reads
        # only the first 32 bytes.
        data = bytes.fromhex(uid[2:]) + b"\x00" * 32
        logs.append(
            {
                "address": config.EAS_ADDR,
                "topics": [attest.ATTESTED_TOPIC0],
                "data": data,
            }
        )
    return {"status": status, "logs": logs}


def _wire(monkeypatch, *, chain_id=196, schema_registered=True):
    """Patch the tick so a fake attester is used and the chain/schema seams answer."""
    monkeypatch.setattr(attest, "_attester_factory", lambda: object())
    monkeypatch.setattr(attest, "_chain_id", lambda att: chain_id)
    monkeypatch.setattr(attest, "_schema_is_registered", lambda att: schema_registered)


def _seed(
    make_store,
    *,
    oid="ordattest0000001",
    slug="attest-store",
    from_addr=PAYER,
    tx_hash=PAY_TX,
    attest_status="pending",
    status="delivered",
    delivery_payload=DELIV_PAYLOAD,
    with_delivery=True,
):
    sid = make_store(slug=slug, price_micro=1_000_000)
    with SessionLocal() as s:
        store = s.get(Store, sid)
        product = agentic._active_product(s, sid)
        # A delivered order carries a Delivery row (the payload the attester hashes) and
        # the product it bought. A row already past 'pending' has its content hash
        # recorded (the broadcast intent); a fresh 'pending' row leaves it NULL for the
        # worker to compute + persist at attest time.
        s.add(
            Order(
                id=oid,
                store_id=sid,
                product_id=product.id if product else None,
                pay_to=store.pay_to,
                amount_micro=1_000_000,
                expected_micro=1_000_123,
                status=status,
                from_addr=from_addr,
                tx_hash=tx_hash,
                attest_status=attest_status,
                content_hash=(
                    None
                    if attest_status == "pending"
                    else attest.content_hash_hex(delivery_payload)
                ),
                paid_at=checkout._now(),
            )
        )
        if with_delivery:
            s.flush()  # the order row must exist before the Delivery FK references it
            s.add(Delivery(order_id=oid, kind="text", payload=delivery_payload))
        s.commit()
    return sid, oid


def _attest_status(oid):
    with SessionLocal() as s:
        return s.get(Order, oid).attest_status


def _order(oid):
    with SessionLocal() as s:
        o = s.get(Order, oid)
        return (o.attest_status, o.attestation_uid, o.attest_tx)


# ============================================================================
# Pure encoding — the golden test (no RPC, no signing, no network)
# ============================================================================
def test_schema_uid_matches_spike_precompute():
    assert "0x" + attest.schema_uid().hex() == SCHEMA_UID_HEX


def test_attested_topic0_matches_precompute():
    assert "0x" + attest.ATTESTED_TOPIC0.hex() == ATTESTED_TOPIC0_HEX


def test_attest_selector_is_canonical():
    # The EAS attest((bytes32,(address,uint64,bool,bytes32,bytes,uint256))) selector.
    sig = "attest((bytes32,(address,uint64,bool,bytes32,bytes,uint256)))"
    assert "0x" + keccak(text=sig)[:4].hex() == ATTEST_SELECTOR


def test_payload_encodes_and_round_trips():
    data = attest.build_attestation_data(
        PAYER, "acme-store", 1_500_000, PAY_TX, 42, CONTENT_HASH
    )
    buyer, slug, micro, tx_b, product_id, ch_b = attest.decode_attestation_data(data)
    assert buyer.lower() == PAYER.lower()  # checksummed on the way in
    assert slug == "acme-store"
    assert micro == 1_500_000
    assert "0x" + tx_b.hex() == PAY_TX
    assert product_id == 42  # product identity bound into the receipt
    assert "0x" + ch_b.hex() == CONTENT_HASH  # sha256 of the delivered content


def test_payload_encodes_null_product_as_zero():
    # A store with no per-product order binds productId 0 (never a crash on None).
    data = attest.build_attestation_data(
        PAYER, "acme-store", 1, PAY_TX, None, CONTENT_HASH
    )
    _, _, _, _, product_id, _ = attest.decode_attestation_data(data)
    assert product_id == 0


def test_content_hash_hex_is_sha256_of_payload():
    assert attest.content_hash_hex(DELIV_PAYLOAD) == CONTENT_HASH


def test_attest_request_shape_and_recipient_is_buyer():
    req = attest._attest_request(
        PAYER, "acme-store", 1_000_123, PAY_TX, 7, CONTENT_HASH
    )
    assert req["schema"] == attest.schema_uid()
    assert req["data"]["recipient"].lower() == PAYER.lower()  # recipient = buyer
    assert req["data"]["revocable"] is True
    assert req["data"]["refUID"] == b"\x00" * 32
    assert req["data"]["value"] == 0
    buyer, slug, micro, tx_b, product_id, ch_b = attest.decode_attestation_data(
        req["data"]["data"]
    )
    assert slug == "acme-store" and micro == 1_000_123 and "0x" + tx_b.hex() == PAY_TX
    assert product_id == 7 and "0x" + ch_b.hex() == CONTENT_HASH


def test_uid_extracted_from_synthetic_attested_log():
    assert attest._uid_from_receipt(_receipt(1, UID)) == UID
    assert attest._uid_from_receipt(_receipt(1, uid=None)) is None


# ============================================================================
# Dormancy — three independent, default-closed gates
# ============================================================================
def test_import_of_attest_loads_no_web3():
    # Subprocess so the assertion is immune to another test having imported web3.
    code = (
        "import sys; import app.attest; import app.main; "
        "assert 'web3' not in sys.modules, 'web3 imported on dormant boot'; "
        "print('ok')"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(main.__file__).rsplit("app", 1)[0],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_default_factory_is_none_when_flag_off():
    # Default test env: TILLA_ATTEST unset, no key => dormant, no attester built.
    assert config.ATTEST_ENABLED is False
    assert attest._default_attester_factory() is None


def test_default_factory_is_none_with_flag_but_no_key(monkeypatch):
    monkeypatch.setattr(config, "ATTEST_ENABLED", True)
    monkeypatch.setattr(config, "TILLA_ATTESTER_KEY", "")
    assert attest._default_attester_factory() is None


def test_tick_is_noop_when_dormant(monkeypatch):
    # Factory returns None => the tick never reaches chain_id / any web3 seam.
    chain_spy = _Spy(196)
    monkeypatch.setattr(attest, "_attester_factory", lambda: None)
    monkeypatch.setattr(attest, "_chain_id", chain_spy)
    assert attest.attest_tick() == 0
    assert chain_spy.count == 0


def test_dormant_deliver_leaves_status_none_and_still_delivers(make_store):
    # Flag OFF (default): a web order delivers normally and is NEVER queued.
    make_store(slug="dormant-store", pay_to="0x" + "a" * 40, price_micro=2_000_000)
    cid = client.post("/api/checkout/dormant-store").json()["id"]
    with SessionLocal() as s:
        o = s.get(Order, cid)
        checkout.apply_transfer(
            s,
            o,
            o.expected_micro,
            tx_hash=PAY_TX,
            log_index=0,
            block_number=1,
            from_addr=PAYER,
            head=10**9,
        )
        s.commit()
    assert client.get(f"/api/checkout/{cid}").json()["status"] == "paid"
    assert _attest_status(cid) == "none"


# ============================================================================
# Queueing — pending is written only when the flag is on
# ============================================================================
def test_web_deliver_queues_pending_when_enabled(monkeypatch, make_store):
    monkeypatch.setattr(config, "ATTEST_ENABLED", True)
    make_store(slug="q-store", pay_to="0x" + "b" * 40, price_micro=2_000_000)
    cid = client.post("/api/checkout/q-store").json()["id"]
    with SessionLocal() as s:
        o = s.get(Order, cid)
        checkout.apply_transfer(
            s,
            o,
            o.expected_micro,
            tx_hash=PAY_TX,
            log_index=0,
            block_number=1,
            from_addr=PAYER,
            head=10**9,
        )
        s.commit()
    assert _attest_status(cid) == "pending"
    # idempotent replay poll keeps it 'pending' (deliver is idempotent)
    client.get(f"/api/checkout/{cid}")
    assert _attest_status(cid) == "pending"


def _store_product(sid):
    with SessionLocal() as s:
        store = s.get(Store, sid)
        product = agentic._active_product(s, sid)
        s.expunge_all()
        return store, product


def test_agent_order_queued_only_on_settlement(monkeypatch, make_store):
    from x402.http.utils import encode_payment_response_header
    from x402.schemas import SettleResponse

    monkeypatch.setattr(config, "ATTEST_ENABLED", True)
    sid = make_store(slug="agent-q", price_micro=1_000_000, delivery="X")
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, _ = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        s.commit()
        oid = order.id
    # settling (delivered-but-unsettled): NOT queued yet
    assert _attest_status(oid) == "none"
    header = encode_payment_response_header(
        SettleResponse(success=True, transaction=PAY_TX, network="eip155:196")
    )
    agentic.record_settlement(oid, header)
    # settling -> delivered flip queues it
    assert _attest_status(oid) == "pending"


def test_voided_agent_order_is_never_queued(monkeypatch, make_store):
    monkeypatch.setattr(config, "ATTEST_ENABLED", True)
    sid = make_store(slug="agent-void", price_micro=1_000_000, delivery="X")
    store, product = _store_product(sid)
    with SessionLocal() as s:
        order, _ = agentic.fulfill_agent_order(
            s, s.merge(store), s.merge(product), PAYER, NONCE
        )
        s.commit()
        oid = order.id
    agentic.settle_failed_core(NONCE)  # settle failed -> canceled
    with SessionLocal() as s:
        assert s.get(Order, oid).status == "canceled"
    assert _attest_status(oid) == "none"


# ============================================================================
# Worker — idempotency, reconcile, guards (web3 boundary mocked)
# ============================================================================
def test_worker_attests_pending_exactly_once(monkeypatch, make_store):
    _wire(monkeypatch)
    prep = _Spy(_prep())
    bcast = _Spy(None)
    wait = _Spy(_receipt(1, UID))
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    monkeypatch.setattr(attest, "_wait_receipt", wait)
    _seed(make_store)

    assert attest.attest_tick() == 1
    assert _order("ordattest0000001") == ("attested", UID, ATTEST_TX)
    assert prep.count == 1 and bcast.count == 1

    # second tick: nothing pending -> no prepare, no broadcast, no-op
    assert attest.attest_tick() == 0
    assert prep.count == 1 and bcast.count == 1
    assert _attest_status("ordattest0000001") == "attested"


def test_worker_binds_product_and_content_hash(monkeypatch, make_store):
    # The worker hashes the delivered payload at attest time, threads the product id +
    # content hash into the attestation build, and persists the hash on the order (the
    # broadcast intent), so the receipt is provably bound to WHAT was delivered.
    _wire(monkeypatch)
    captured = {}

    def _cap_prepare(
        attester,
        from_addr,
        slug,
        expected_micro,
        tx_hash,
        product_id,
        content_hash,
        nonce=None,
    ):
        captured["product_id"] = product_id
        captured["content_hash"] = content_hash
        return object(), ATTEST_TX, ATTEST_NONCE if nonce is None else nonce

    monkeypatch.setattr(attest, "_prepare_attestation", _cap_prepare)
    monkeypatch.setattr(attest, "_broadcast", _Spy(None))
    monkeypatch.setattr(attest, "_wait_receipt", _Spy(_receipt(1, UID)))
    sid, oid = _seed(make_store)

    assert attest.attest_tick() == 1
    with SessionLocal() as s:
        expected_pid = agentic._active_product(s, sid).id
    assert captured["product_id"] == expected_pid  # the product the order bought
    assert captured["content_hash"] == CONTENT_HASH  # sha256 of the delivered payload
    # the hash is persisted on the order, recorded with the broadcast intent
    with SessionLocal() as s:
        assert s.get(Order, oid).content_hash == CONTENT_HASH


def test_missing_delivery_marks_failed(monkeypatch, make_store):
    # No delivered payload to hash => refuse (a receipt must bind to delivered content).
    prep = _Spy(_prep())
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    sid, oid = _seed(make_store, with_delivery=False)
    attest._attest_one(object(), oid, sid)
    assert _attest_status(oid) == "failed"
    assert (
        prep.count == 0 and bcast.count == 0
    )  # can't attest without delivered content


def test_sending_reconcile_missing_content_hash_fails(monkeypatch, make_store):
    # A 'sending' row without a recorded content hash can't rebuild its attestation —
    # fail-closed rather than sign a receipt not bound to the delivered content.
    prep = _Spy(_prep())
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    monkeypatch.setattr(attest, "_get_receipt", _Spy(None))
    _seed(make_store, attest_status="sending")
    with SessionLocal() as s:
        o = s.get(Order, "ordattest0000001")
        o.attest_tx = ATTEST_TX
        o.attest_nonce = ATTEST_NONCE
        o.content_hash = None  # broadcast intent incomplete
        s.commit()
    attest._reconcile_sending(object())
    assert _attest_status("ordattest0000001") == "failed"
    assert prep.count == 0 and bcast.count == 0


def test_pending_is_claimed_sending_with_nonce_before_broadcast(
    monkeypatch, make_store
):
    # The nonce + tx hash are recorded on the row BEFORE the broadcast fires — the
    # crash-window invariant. Assert it by inspecting the DB from inside the broadcast.
    _wire(monkeypatch)
    seen = {}

    def _capture(att, signed):
        with SessionLocal() as s:
            o = s.get(Order, "ordattest0000001")
            seen["status"] = o.attest_status
            seen["tx"] = o.attest_tx
            seen["nonce"] = o.attest_nonce

    monkeypatch.setattr(attest, "_prepare_attestation", _prep())
    monkeypatch.setattr(attest, "_broadcast", _capture)
    monkeypatch.setattr(attest, "_wait_receipt", _Spy(_receipt(1, UID)))
    _seed(make_store)
    attest.attest_tick()
    assert seen == {"status": "sending", "tx": ATTEST_TX, "nonce": ATTEST_NONCE}


def test_crash_after_broadcast_before_claim_does_not_double_broadcast(
    monkeypatch, make_store
):
    # Simulate a crash between broadcast and the sending->sent claim: the row is left
    # 'sending' with its tx + nonce recorded. On restart the reconcile resolves it from
    # chain and NEVER prepares/broadcasts a second attestation.
    prep = _Spy(_prep())
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    monkeypatch.setattr(attest, "_get_receipt", _Spy(_receipt(1, UID)))
    _seed(make_store, attest_status="sending")
    with SessionLocal() as s:
        o = s.get(Order, "ordattest0000001")
        o.attest_tx = ATTEST_TX
        o.attest_nonce = ATTEST_NONCE
        s.commit()
    attest._reconcile_sending(object())
    assert _order("ordattest0000001") == ("attested", UID, ATTEST_TX)
    assert prep.count == 0 and bcast.count == 0  # no re-broadcast — resolved from chain


def test_sending_reconcile_nonce_free_rebroadcasts_same_nonce(monkeypatch, make_store):
    # Crash BEFORE the broadcast landed: tx not on chain and the nonce is still free.
    # Reconcile re-broadcasts pinned to the SAME nonce (idempotent dedup) and advances
    # to 'sent'. The new hash replaces the recorded one.
    prep = _Spy(_prep(tx=ATTEST_TX2))
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    monkeypatch.setattr(attest, "_get_receipt", _Spy(None))  # not on chain
    monkeypatch.setattr(attest, "_confirmed_nonce", lambda att: ATTEST_NONCE)  # free
    _seed(make_store, attest_status="sending")
    with SessionLocal() as s:
        o = s.get(Order, "ordattest0000001")
        o.attest_tx = ATTEST_TX
        o.attest_nonce = ATTEST_NONCE
        s.commit()
    attest._reconcile_sending(object())
    assert _order("ordattest0000001") == ("sent", None, ATTEST_TX2)
    assert bcast.count == 1
    # re-broadcast pinned to the SAME nonce (dedup key), never a fresh one
    assert prep.calls[0][1].get("nonce") == ATTEST_NONCE


def test_sending_reconcile_nonce_consumed_reattests_fresh(monkeypatch, make_store):
    # Nonce-based dedup: the recorded nonce was consumed by a DIFFERENT mined tx (our
    # hash is not on chain) => our attestation is definitively not on chain => re-attest
    # fresh (back to pending, tx/nonce cleared) rather than re-broadcast at a dead nonce.
    prep = _Spy(_prep())
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    monkeypatch.setattr(attest, "_get_receipt", _Spy(None))  # our tx not on chain
    monkeypatch.setattr(attest, "_confirmed_nonce", lambda att: ATTEST_NONCE + 2)
    _seed(make_store, attest_status="sending")
    with SessionLocal() as s:
        o = s.get(Order, "ordattest0000001")
        o.attest_tx = ATTEST_TX
        o.attest_nonce = ATTEST_NONCE
        s.commit()
    attest._reconcile_sending(object())
    assert _order("ordattest0000001") == ("pending", None, None)
    assert prep.count == 0 and bcast.count == 0  # no re-broadcast at the dead nonce


def test_sending_reconcile_flaked_notfound_does_not_double_attest(
    monkeypatch, make_store
):
    # nonce consumed AND the first receipt read flakes to None (RPC error swallowed),
    # but our tx actually mined. The re-check receipt read finds it => resolve to
    # attested, NEVER re-attest fresh (which would double-attest = wasted OKB gas).
    seq = iter([None, _receipt(1, UID)])  # first read flakes None, re-check finds it
    prep = _Spy(_prep())
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    monkeypatch.setattr(attest, "_get_receipt", _Spy(lambda *a, **k: next(seq)))
    monkeypatch.setattr(attest, "_confirmed_nonce", lambda att: ATTEST_NONCE + 2)
    _seed(make_store, attest_status="sending")
    with SessionLocal() as s:
        o = s.get(Order, "ordattest0000001")
        o.attest_tx = ATTEST_TX
        o.attest_nonce = ATTEST_NONCE
        s.commit()
    attest._reconcile_sending(object())
    status, uid, _ = _order("ordattest0000001")
    assert status == "attested" and uid == UID  # resolved, not re-attested
    assert prep.count == 0 and bcast.count == 0  # no second broadcast


def test_sending_reconcile_mine_in_gap_uses_same_nonce(monkeypatch, make_store):
    # The nonce is read BEFORE the receipt, so a tx that mines in the read gap is seen
    # as still-pending (confirmed == attest_nonce) and takes the safe same-nonce
    # rebroadcast path — never the fresh-reattest path that would double-attest.
    prep = _Spy(_prep(tx=ATTEST_TX2))
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    monkeypatch.setattr(attest, "_get_receipt", _Spy(None))  # not seen yet by our read
    monkeypatch.setattr(attest, "_confirmed_nonce", lambda att: ATTEST_NONCE)  # pending
    _seed(make_store, attest_status="sending")
    with SessionLocal() as s:
        o = s.get(Order, "ordattest0000001")
        o.attest_tx = ATTEST_TX
        o.attest_nonce = ATTEST_NONCE
        s.commit()
    attest._reconcile_sending(object())
    # same-nonce rebroadcast (dedup), never a fresh re-attest
    assert prep.calls[0][1].get("nonce") == ATTEST_NONCE
    assert _order("ordattest0000001")[0] == "sent"


def test_sending_reconcile_reverted_marks_failed(monkeypatch, make_store):
    monkeypatch.setattr(attest, "_get_receipt", _Spy(_receipt(status=0)))
    _seed(make_store, attest_status="sending")
    with SessionLocal() as s:
        o = s.get(Order, "ordattest0000001")
        o.attest_tx = ATTEST_TX
        o.attest_nonce = ATTEST_NONCE
        s.commit()
    attest._reconcile_sending(object())
    assert _attest_status("ordattest0000001") == "failed"


def test_non_pending_order_sends_nothing(monkeypatch, make_store):
    # The pre-send status re-check means an order already out of 'pending' is never
    # signed for (the conditional-claim guard — no double-attest).
    prep = _Spy(_prep())
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    sid, oid = _seed(make_store, attest_status="attested")
    attest._attest_one(object(), oid, sid)
    assert prep.count == 0 and bcast.count == 0
    assert _attest_status(oid) == "attested"


def test_reverted_attestation_marks_failed(monkeypatch, make_store):
    _wire(monkeypatch)
    monkeypatch.setattr(attest, "_prepare_attestation", _Spy(_prep()))
    monkeypatch.setattr(attest, "_broadcast", _Spy(None))
    monkeypatch.setattr(attest, "_wait_receipt", _Spy(_receipt(status=0)))
    _seed(make_store)
    attest.attest_tick()
    assert _attest_status("ordattest0000001") == "failed"


def test_sent_reconcile_status1_attests(monkeypatch, make_store):
    prep = _Spy(_prep())  # must NOT be called
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    monkeypatch.setattr(attest, "_get_receipt", _Spy(_receipt(1, UID)))
    _seed(make_store, attest_status="sent", tx_hash=PAY_TX)
    with SessionLocal() as s:
        s.get(Order, "ordattest0000001").attest_tx = ATTEST_TX
        s.commit()
    attest._reconcile_sent(object())
    assert _order("ordattest0000001") == ("attested", UID, ATTEST_TX)
    assert prep.count == 0 and bcast.count == 0


def test_sent_reconcile_status0_fails(monkeypatch, make_store):
    monkeypatch.setattr(attest, "_get_receipt", _Spy(_receipt(status=0)))
    _seed(make_store, attest_status="sent")
    with SessionLocal() as s:
        s.get(Order, "ordattest0000001").attest_tx = ATTEST_TX
        s.commit()
    attest._reconcile_sent(object())
    assert _attest_status("ordattest0000001") == "failed"


def test_sent_reconcile_not_found_fails_after_bounded_ticks(monkeypatch, make_store):
    prep = _Spy(_prep())
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    monkeypatch.setattr(attest, "_get_receipt", _Spy(None))  # never found
    _seed(make_store, attest_status="sent")
    with SessionLocal() as s:
        s.get(Order, "ordattest0000001").attest_tx = ATTEST_TX
        s.commit()
    for _ in range(attest.RECONCILE_MAX_TICKS - 1):
        attest._reconcile_sent(object())
        assert _attest_status("ordattest0000001") == "sent"  # still retrying
    attest._reconcile_sent(object())
    assert _attest_status("ordattest0000001") == "failed"
    assert prep.count == 0 and bcast.count == 0  # reconcile never re-sends


def test_chain_guard_refuses_wrong_chain(monkeypatch, make_store):
    _wire(monkeypatch, chain_id=1952)  # != ATTEST_CHAIN_ID (196)
    prep = _Spy(_prep())
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    _seed(make_store)
    assert attest.attest_tick() == 0
    assert prep.count == 0 and bcast.count == 0  # nothing signed on a chain mismatch
    assert _attest_status("ordattest0000001") == "pending"  # untouched


def test_missing_buyer_or_tx_marks_failed(monkeypatch, make_store):
    prep = _Spy(_prep())
    bcast = _Spy(None)
    monkeypatch.setattr(attest, "_prepare_attestation", prep)
    monkeypatch.setattr(attest, "_broadcast", bcast)
    sid, oid = _seed(make_store, from_addr=None)
    attest._attest_one(object(), oid, sid)
    assert _attest_status(oid) == "failed"
    assert prep.count == 0 and bcast.count == 0  # can't attest without a buyer


def test_gas_over_cap_leaves_pending(monkeypatch, make_store):
    _wire(monkeypatch)
    # Gas is checked inside _prepare_attestation (before any broadcast) — a refusal
    # leaves the row 'pending', never 'sending', so nothing is ever broadcast.
    bcast = _Spy(None)
    monkeypatch.setattr(
        attest, "_prepare_attestation", _Spy(attest._GasTooHigh(10**18))
    )
    monkeypatch.setattr(attest, "_broadcast", bcast)
    _seed(make_store)
    attest.attest_tick()  # must not raise
    assert _attest_status("ordattest0000001") == "pending"  # retried later, not failed
    assert bcast.count == 0


def test_send_signed_refuses_over_gas_cap():
    # Exercises the real cap logic against a mocked Web3 (nothing broadcast).
    att = MagicMock()
    att.w3.eth.gas_price = 10**12  # 1000 gwei fee spike
    att.w3.to_wei.return_value = 10**9
    fn = MagicMock()
    fn.estimate_gas.return_value = 500_000  # 500k * 1e12 = 5e17 >> 1e16 cap
    with pytest.raises(attest._GasTooHigh):
        attest._send_signed(att, fn)
    att.w3.eth.send_raw_transaction.assert_not_called()  # never signed/broadcast


def test_send_signed_under_cap_broadcasts_once():
    att = MagicMock()
    att.w3.eth.gas_price = 10**9
    att.w3.to_wei.return_value = 10**9
    att.account.address = "0x" + "cc" * 20
    fn = MagicMock()
    fn.estimate_gas.return_value = 300_000  # 3e14 << 1e16 cap
    tx_obj = MagicMock()
    tx_obj.to_0x_hex.return_value = ATTEST_TX
    att.w3.eth.send_raw_transaction.return_value = tx_obj
    got = attest._send_signed(att, fn)
    assert got == ATTEST_TX
    att.w3.eth.send_raw_transaction.assert_called_once()


def test_schema_registered_once_then_cached(monkeypatch):
    reg = _Spy(ATTEST_TX)
    monkeypatch.setattr(attest, "_schema_is_registered", lambda att: False)
    monkeypatch.setattr(attest, "_register_schema", reg)
    monkeypatch.setattr(attest, "_wait_receipt", _Spy(_receipt(1, uid=None)))
    att = object()
    assert attest._ensure_schema(att) is True
    assert attest._ensure_schema(att) is True  # cached — no second register
    assert reg.count == 1


def test_schema_already_registered_skips_register(monkeypatch):
    reg = _Spy(ATTEST_TX)
    monkeypatch.setattr(attest, "_schema_is_registered", lambda att: True)
    monkeypatch.setattr(attest, "_register_schema", reg)
    assert attest._ensure_schema(object()) is True
    assert reg.count == 0


# ============================================================================
# Receipt surface — additive, NULL-safe, XSS-inert
# ============================================================================
def test_order_response_includes_attestation_when_set(make_store):
    _seed(make_store, oid="ordsurface000001", slug="surface-store")
    with SessionLocal() as s:
        o = s.get(Order, "ordsurface000001")
        o.attestation_uid = UID
        o.attest_tx = ATTEST_TX
        o.attest_status = "attested"
        s.commit()
    with SessionLocal() as s:
        out = main._order_response(s, s.get(Order, "ordsurface000001"))
    assert out["attestation_uid"] == UID
    assert out["attestation_tx_url"] == config.OKLINK_TX_BASE + ATTEST_TX


def test_order_response_omits_attestation_when_unset(make_store):
    _seed(make_store, oid="ordsurface000002", slug="surface-store2")
    with SessionLocal() as s:
        out = main._order_response(s, s.get(Order, "ordsurface000002"))
    assert "attestation_uid" not in out
    assert "attestation_tx_url" not in out


def test_merchant_order_detail_surfaces_attestation(make_store):
    acct = Account.create()
    make_store(slug="mdetail", pay_to=acct.address.lower(), price_micro=1_000_000)
    cid = client.post("/api/checkout/mdetail").json()["id"]
    with SessionLocal() as s:
        o = s.get(Order, cid)
        checkout.apply_transfer(
            s,
            o,
            o.expected_micro,
            tx_hash=PAY_TX,
            log_index=0,
            block_number=1,
            from_addr=PAYER,
            head=10**9,
        )
        o = s.get(Order, cid)
        o.attestation_uid = UID
        o.attest_tx = ATTEST_TX
        o.attest_status = "attested"
        s.commit()
    msg = client.post(
        "/api/merchant/auth/nonce", json={"address": acct.address}
    ).json()["message"]
    sig = acct.sign_message(encode_defunct(text=msg)).signature.hex()
    token = client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    ).json()["session_token"]
    d = client.get(
        "/api/merchant/orders/" + cid, headers={"Authorization": "Bearer " + token}
    ).json()
    assert d["attest_status"] == "attested"
    assert d["attestation_uid"] == UID
    assert d["attestation_tx_url"] == config.OKLINK_TX_BASE + ATTEST_TX


def test_checkout_theme_renders_attestation_xss_safe():
    # The receipt row is built through the textContent-only receiptRow helper and
    # hex-guarded by TX_RE — never innerHTML — so a hostile UID/URL can't inject.
    html = (config.THEMES_DIR / "_checkout.html").read_text(encoding="utf-8")
    assert 'receiptRow("On-chain receipt"' in html
    assert "TX_RE.test(auid)" in html
    assert "d.attestation_uid" in html


def test_dashboard_theme_renders_attestation_via_textcontent():
    html = (config.THEMES_DIR / "_dashboard.html").read_text(encoding="utf-8")
    assert "o.attestation_uid" in html
    assert "On-chain receipt" in html
