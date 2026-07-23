"""M8 MPP tests (no network, no funds, SA client fully mocked): the pure channel
state machine over mpp_channels (open/voucher/topup/close, monotonic vouchers,
overspend, sqlite persistence, idempotent replays), the fail-closed 503 when
unconfigured, the non-metered 409 / unknown-store 404 gates, and the 502-with-no-
state-change on an SA error. No test ever marks a channel closed with a synthetic
settle — a real open→voucher→close is USER-gated.
"""

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import config, mpp
from app.db import SessionLocal
from app.models import MppChannel, Store

client = TestClient(main.app)

# Deterministic throwaway buyer whose signature backs every seeded channel's
# vouchers; a second key stands in for an attacker who only learned the channel_id.
_BUYER = Account.from_key("0x" + "11" * 32)
_ATTACKER = Account.from_key("0x" + "22" * 32)


def _voucher(channel_id, cumulative, signer=_BUYER):
    """A buyer-signed voucher dict (as the endpoint receives it) authorizing spend
    up to ``cumulative`` on ``channel_id``."""
    msg = mpp._voucher_message(channel_id, cumulative)
    sig = signer.sign_message(encode_defunct(text=msg)).signature.hex()
    return {"signature": sig}


# --------------------------------------------------------------- helpers
def _metered_store(slug: str, unit_price=1000, min_deposit=1_000_000):
    with SessionLocal() as s:
        from app.models import Product, get_or_create_merchant

        pay_to = "0x" + "a" * 40
        merchant = get_or_create_merchant(s, pay_to)
        store = Store(
            slug=slug,
            merchant_id=merchant.id,
            status="live",
            pay_to=pay_to,
            delivery="metered unit content",
            theme="original.html",
        )
        s.add(store)
        s.flush()
        p = Product(
            store_id=store.id,
            name="Meter",
            price_micro=unit_price,
            active=True,
            pricing_model="metered",
            pricing_params={
                "unit": "token",
                "price_per_unit_micro": unit_price,
                "min_deposit_micro": min_deposit,
            },
        )
        s.add(p)
        s.commit()
        return store.id, p.id, pay_to


def _seed_channel(store_id, product_id, pay_to, channel_id="ch_1", deposit=1_000_000):
    return mpp.open_channel(
        store_id=store_id,
        product_id=product_id,
        pay_to=pay_to,
        channel_id=channel_id,
        buyer_addr=_BUYER.address,
        deposit_micro=deposit,
        unit_price_micro=1000,
    )


class MockGateway:
    def __init__(self, fail=False, open_receipt=None):
        self.fail = fail
        self.open_receipt = open_receipt or {
            "channelId": "ch_sa",
            "deposit": "1000000",
            "status": "open",
        }

    async def open(self, payload):
        if self.fail:
            raise RuntimeError("SA down")
        return self.open_receipt

    async def top_up(self, payload):
        if self.fail:
            raise RuntimeError("SA down")
        return {"channelId": payload.get("channelId", "ch_1"), "status": "open"}

    async def close(self, payload):
        if self.fail:
            raise RuntimeError("SA down")
        return {"channelId": payload.get("channelId", "ch_1"), "status": "closed"}

    async def status(self, cid):
        return {"channelId": cid}


def _enable(monkeypatch, gateway):
    monkeypatch.setattr(config, "MPP_ENABLED", True)
    monkeypatch.setattr(mpp, "_sa_creds", lambda: ("k", "s", "p"))
    monkeypatch.setattr(mpp, "_gateway_factory", lambda: gateway)


# ===================================================== state machine (pure)
def test_open_channel_inserts_row(make_store):
    sid, pid, pay_to = _metered_store("m1")
    view = _seed_channel(sid, pid, pay_to)
    assert view["status"] == "open"
    assert view["deposit_micro"] == 1_000_000
    assert view["spent_micro"] == 0
    with SessionLocal() as s:
        row = s.scalar(select(MppChannel).where(MppChannel.channel_id == "ch_1"))
        assert row is not None and row.status == "open"


def test_open_channel_idempotent_on_duplicate_id(make_store):
    sid, pid, pay_to = _metered_store("m2")
    _seed_channel(sid, pid, pay_to, channel_id="dup")
    again = _seed_channel(sid, pid, pay_to, channel_id="dup", deposit=999)
    assert again["deposit_micro"] == 1_000_000  # original row, not the replay
    with SessionLocal() as s:
        n = s.scalars(select(MppChannel).where(MppChannel.channel_id == "dup")).all()
        assert len(n) == 1


def test_voucher_advances_spent_and_persists(make_store):
    sid, pid, pay_to = _metered_store("m3")
    _seed_channel(sid, pid, pay_to)
    v = mpp.apply_voucher(
        channel_id="ch_1", cumulative_micro=1000, voucher_json=_voucher("ch_1", 1000)
    )
    assert v["spent_micro"] == 1000
    v2 = mpp.apply_voucher(
        channel_id="ch_1", cumulative_micro=2500, voucher_json=_voucher("ch_1", 2500)
    )
    assert v2["spent_micro"] == 2500
    with SessionLocal() as s:
        row = s.scalar(select(MppChannel).where(MppChannel.channel_id == "ch_1"))
        assert row.spent_micro == 2500
        assert row.last_voucher_json == _voucher("ch_1", 2500)


def test_voucher_regression_rejected_no_change(make_store):
    sid, pid, pay_to = _metered_store("m4")
    _seed_channel(sid, pid, pay_to)
    mpp.apply_voucher(
        channel_id="ch_1", cumulative_micro=5000, voucher_json=_voucher("ch_1", 5000)
    )
    with pytest.raises(mpp.MppStateError):
        mpp.apply_voucher(
            channel_id="ch_1",
            cumulative_micro=4000,
            voucher_json=_voucher("ch_1", 4000),
        )
    with SessionLocal() as s:
        assert (
            s.scalar(
                select(MppChannel).where(MppChannel.channel_id == "ch_1")
            ).spent_micro
            == 5000
        )


def test_voucher_replay_same_cumulative_rejected(make_store):
    sid, pid, pay_to = _metered_store("m5")
    _seed_channel(sid, pid, pay_to)
    mpp.apply_voucher(
        channel_id="ch_1", cumulative_micro=3000, voucher_json=_voucher("ch_1", 3000)
    )
    with pytest.raises(mpp.MppStateError):
        mpp.apply_voucher(
            channel_id="ch_1",
            cumulative_micro=3000,
            voucher_json=_voucher("ch_1", 3000),
        )


def test_voucher_overspend_rejected(make_store):
    sid, pid, pay_to = _metered_store("m6")
    _seed_channel(sid, pid, pay_to, deposit=1000)
    with pytest.raises(mpp.MppStateError):
        mpp.apply_voucher(
            channel_id="ch_1",
            cumulative_micro=2000,
            voucher_json=_voucher("ch_1", 2000),
        )


def test_voucher_unknown_channel_raises(make_store):
    _metered_store("m7")
    with pytest.raises(mpp.ChannelNotFound):
        mpp.apply_voucher(channel_id="nope", cumulative_micro=1, voucher_json={})


def test_voucher_missing_signature_rejected(make_store):
    sid, pid, pay_to = _metered_store("m11")
    _seed_channel(sid, pid, pay_to)
    with pytest.raises(mpp.MppStateError):
        mpp.apply_voucher(
            channel_id="ch_1", cumulative_micro=1000, voucher_json={"note": "no sig"}
        )
    with SessionLocal() as s:
        assert (
            s.scalar(
                select(MppChannel).where(MppChannel.channel_id == "ch_1")
            ).spent_micro
            == 0
        )


def test_voucher_wrong_signer_rejected(make_store):
    # A voucher signed by anyone other than the channel's depositing buyer is
    # rejected — learning a live channel_id is not enough to spend it.
    sid, pid, pay_to = _metered_store("m12")
    _seed_channel(sid, pid, pay_to)
    with pytest.raises(mpp.MppStateError):
        mpp.apply_voucher(
            channel_id="ch_1",
            cumulative_micro=1000,
            voucher_json=_voucher("ch_1", 1000, signer=_ATTACKER),
        )
    with SessionLocal() as s:
        assert (
            s.scalar(
                select(MppChannel).where(MppChannel.channel_id == "ch_1")
            ).spent_micro
            == 0
        )


def test_topup_raises_deposit(make_store):
    sid, pid, pay_to = _metered_store("m8")
    _seed_channel(sid, pid, pay_to, deposit=1_000_000)
    v = mpp.topup_channel(channel_id="ch_1", add_micro=500_000)
    assert v["deposit_micro"] == 1_500_000


def test_close_transitions_once_then_idempotent(make_store):
    sid, pid, pay_to = _metered_store("m9")
    _seed_channel(sid, pid, pay_to)
    v = mpp.close_channel(channel_id="ch_1", final_spent_micro=1234)
    assert v["status"] == "closed"
    assert v["spent_micro"] == 1234
    # replay: already closed -> unchanged, no error
    v2 = mpp.close_channel(channel_id="ch_1", final_spent_micro=9999)
    assert v2["status"] == "closed"
    assert v2["spent_micro"] == 1234  # not overwritten by the replay


def test_close_from_closing_state(make_store):
    sid, pid, pay_to = _metered_store("m10")
    _seed_channel(sid, pid, pay_to)
    with SessionLocal() as s:
        row = s.scalar(select(MppChannel).where(MppChannel.channel_id == "ch_1"))
        row.status = "closing"
        s.commit()
    v = mpp.close_channel(channel_id="ch_1")
    assert v["status"] == "closed"


# ===================================================== fail-closed (503)
@pytest.mark.parametrize("path", ["open", "voucher", "topup", "close"])
def test_endpoints_503_when_flag_off(make_store, path):
    _metered_store(f"off-{path}")
    # config.MPP_ENABLED defaults off in tests -> every endpoint fail-closes
    r = client.post(f"/s/off-{path}/mpp/{path}", json={})
    assert r.status_code == 503


@pytest.mark.parametrize("path", ["open", "voucher", "topup", "close"])
def test_endpoints_503_when_creds_missing(make_store, monkeypatch, path):
    _metered_store(f"nc-{path}")
    monkeypatch.setattr(config, "MPP_ENABLED", True)
    monkeypatch.setattr(mpp, "_sa_creds", lambda: None)  # flag on, no creds
    r = client.post(f"/s/nc-{path}/mpp/{path}", json={})
    assert r.status_code == 503


# ===================================================== endpoint gates
def test_open_non_metered_409(make_store, monkeypatch):
    make_store(slug="notmeter")  # default one_time
    _enable(monkeypatch, MockGateway())
    r = client.post("/s/notmeter/mpp/open", json={"payload": {"channelId": "c"}})
    assert r.status_code == 409


def test_open_unknown_store_404(monkeypatch):
    _enable(monkeypatch, MockGateway())
    r = client.post("/s/ghost/mpp/open", json={"payload": {"channelId": "c"}})
    assert r.status_code == 404


# ===================================================== endpoints (mocked SA)
def test_open_endpoint_creates_channel(make_store, monkeypatch):
    _metered_store("e1")
    _enable(monkeypatch, MockGateway())
    r = client.post(
        "/s/e1/mpp/open",
        json={
            "sa_request": {
                "payload": {
                    "channelId": "ch_sa",
                    "authorization": {"from": "0x" + "c" * 40},
                }
            }
        },
    )
    assert r.status_code == 200
    ch = r.json()["channel"]
    assert ch["channel_id"] == "ch_sa"
    assert ch["status"] == "open"
    assert ch["deposit_micro"] == 1_000_000
    with SessionLocal() as s:
        assert s.scalar(select(MppChannel).where(MppChannel.channel_id == "ch_sa"))


def test_open_endpoint_502_on_sa_error_no_row(make_store, monkeypatch):
    _metered_store("e2")
    _enable(monkeypatch, MockGateway(fail=True))
    r = client.post("/s/e2/mpp/open", json={"sa_request": {"payload": {"channelId": "ch_x"}}})
    assert r.status_code == 502
    with SessionLocal() as s:
        assert s.scalar(select(MppChannel)) is None  # no local state change


def test_voucher_endpoint_serves_unit(make_store, monkeypatch):
    sid, pid, pay_to = _metered_store("e3")
    _seed_channel(sid, pid, pay_to, channel_id="ch_v")
    _enable(monkeypatch, MockGateway())
    r = client.post(
        "/s/e3/mpp/voucher",
        json={
            "channel_id": "ch_v",
            "cumulative_amount_micro": 1000,
            "voucher": _voucher("ch_v", 1000),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["channel"]["spent_micro"] == 1000
    assert body["unit"]["delivery"] == "metered unit content"


def test_voucher_endpoint_overspend_400(make_store, monkeypatch):
    sid, pid, pay_to = _metered_store("e4")
    _seed_channel(sid, pid, pay_to, channel_id="ch_o", deposit=500)
    _enable(monkeypatch, MockGateway())
    r = client.post(
        "/s/e4/mpp/voucher",
        json={
            "channel_id": "ch_o",
            "cumulative_amount_micro": 900,
            "voucher": _voucher("ch_o", 900),
        },
    )
    assert r.status_code == 400


def test_voucher_endpoint_forged_signature_400(make_store, monkeypatch):
    sid, pid, pay_to = _metered_store("e6")
    _seed_channel(sid, pid, pay_to, channel_id="ch_forge")
    _enable(monkeypatch, MockGateway())
    r = client.post(
        "/s/e6/mpp/voucher",
        json={
            "channel_id": "ch_forge",
            "cumulative_amount_micro": 1000,
            "voucher": _voucher("ch_forge", 1000, signer=_ATTACKER),
        },
    )
    assert r.status_code == 400
    with SessionLocal() as s:
        assert (
            s.scalar(
                select(MppChannel).where(MppChannel.channel_id == "ch_forge")
            ).spent_micro
            == 0
        )


def test_close_endpoint(make_store, monkeypatch):
    sid, pid, pay_to = _metered_store("e5")
    _seed_channel(sid, pid, pay_to, channel_id="ch_c")
    _enable(monkeypatch, MockGateway())
    r = client.post(
        "/s/e5/mpp/close",
        json={
            "channel_id": "ch_c",
            "sa_request": {"payload": {"channelId": "ch_c"}},
            "final_spent_micro": 42,
        },
    )
    assert r.status_code == 200
    assert r.json()["channel"]["status"] == "closed"


# ===================================================== validate-before-SA + accounting
class _TopupGateway(MockGateway):
    """A gateway whose top-up receipt carries an authoritative deposit figure."""

    async def top_up(self, payload):
        return {"channelId": payload.get("channelId", "ch_1"), "deposit": "700000"}


def test_topup_validates_body_before_sa(make_store, monkeypatch):
    # A missing additional_deposit_micro must 422 BEFORE the SA call. With a FAILING
    # gateway, validate-after would surface as 502; validate-before is 422.
    sid, pid, pay_to = _metered_store("tp1")
    _seed_channel(sid, pid, pay_to, channel_id="ch_t")
    _enable(monkeypatch, MockGateway(fail=True))
    r = client.post(
        "/s/tp1/mpp/topup",
        json={"channel_id": "ch_t", "payload": {"channelId": "ch_t"}},
    )
    assert r.status_code == 422


def test_topup_unknown_channel_404_before_sa(make_store, monkeypatch):
    _metered_store("tp3")
    _enable(monkeypatch, MockGateway(fail=True))  # SA would 502 if reached
    r = client.post(
        "/s/tp3/mpp/topup",
        json={
            "channel_id": "ghost",
            "additional_deposit_micro": 100,
            "sa_request": {"payload": {"channelId": "ghost"}},
        },
    )
    assert r.status_code == 404  # existence checked before the fund-moving SA call


def test_topup_amount_derived_from_receipt_not_client(make_store, monkeypatch):
    # The buyer claims +1 micro; the SA receipt says +700000. The row must reflect
    # the receipt, never the buyer's number.
    sid, pid, pay_to = _metered_store("tp2")
    _seed_channel(sid, pid, pay_to, channel_id="ch_t2", deposit=1_000_000)
    _enable(monkeypatch, _TopupGateway())
    r = client.post(
        "/s/tp2/mpp/topup",
        json={
            "channel_id": "ch_t2",
            "additional_deposit_micro": 1,
            "sa_request": {"payload": {"channelId": "ch_t2"}},
        },
    )
    assert r.status_code == 200
    assert r.json()["channel"]["deposit_micro"] == 1_700_000


def test_close_validates_body_before_sa(make_store, monkeypatch):
    sid, pid, pay_to = _metered_store("cl2")
    _seed_channel(sid, pid, pay_to, channel_id="ch_cl2")
    _enable(monkeypatch, MockGateway(fail=True))  # SA would 502 if reached
    r = client.post(
        "/s/cl2/mpp/close",
        json={
            "channel_id": "ch_cl2",
            "payload": {"channelId": "ch_cl2"},
            "final_spent_micro": -5,
        },
    )
    assert r.status_code == 422  # bad field rejected before the channel is closed


def test_close_does_not_lower_spent_below_vouchers(make_store, monkeypatch):
    # A buyer closing with final_spent_micro:0 after consuming vouchers must not
    # rewrite the authoritative spend down.
    sid, pid, pay_to = _metered_store("cl1")
    _seed_channel(sid, pid, pay_to, channel_id="ch_cl", deposit=1_000_000)
    mpp.apply_voucher(
        channel_id="ch_cl", cumulative_micro=5000, voucher_json=_voucher("ch_cl", 5000)
    )
    _enable(monkeypatch, MockGateway())
    r = client.post(
        "/s/cl1/mpp/close",
        json={
            "channel_id": "ch_cl",
            "sa_request": {"payload": {"channelId": "ch_cl"}},
            "final_spent_micro": 0,
        },
    )
    assert r.status_code == 200
    assert r.json()["channel"]["spent_micro"] == 5000  # clamped, not lowered to 0


def test_ready_false_by_default():
    assert mpp.mpp_ready() is False  # flags off in the test env -> dormant
