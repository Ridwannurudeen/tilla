"""Self-serve paid create-store — screen-before-pay, on-chain payment verification,
tx-reuse + owner-scoping guards, and the post-payment generation retry.

All chain access is mocked (no network, no funds); screening and generation are
stubbed so the flow itself is what's under test, not the LLM.
"""

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

import app.main as main
from app import chain, checkout, config, engine, payment, screening, self_serve
from app.db import SessionLocal
from app.models import StoreCreation
from app.screening import ScreeningBlocked

client = TestClient(main.app)

TILLA = "0x" + "f" * 40  # Tilla's create-store fee address (PAY_TO_ADDRESS)
FEE = 50_000  # 0.05 USDT in micro


@pytest.fixture(autouse=True)
def _rail_and_llm(monkeypatch):
    """Every test needs Tilla's fee address configured + generation 'available'."""
    monkeypatch.setenv("PAY_TO_ADDRESS", TILLA)
    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")


def _auth(token):
    return {"Authorization": "Bearer " + token}


def _merchant_token(acct) -> str:
    msg = client.post(
        "/api/merchant/auth/nonce", json={"address": acct.address}
    ).json()["message"]
    sig = acct.sign_message(encode_defunct(text=msg)).signature.hex()
    return client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    ).json()["session_token"]


def _allow_screen(monkeypatch):
    # Return the REAL ScanOutcome the screener produces ('allow' | 'pending'). A
    # SimpleNamespace with an invented status lets a wrong comparison in create_intent
    # pass here while 422-ing every real description.
    monkeypatch.setattr(
        screening, "screen", lambda *_a, **_k: screening.ScanOutcome("allow", None)
    )


def _stub_create_store(monkeypatch, slug="cool-beans", pending=False):
    # Mirror engine.create_store's REAL return shape and signature: the success dict
    # carries NO 'status' key; only the screening-queued path sets 'pending_screening'.
    # A stub inventing status='live' hid _generate marking every success as 'failed'.
    def _fake(desc, addr=None, delivery=None, theme=None, brand_color=None):
        if pending:
            return {
                "slug": slug,
                "status": "pending_screening",
                "store_name": "Cool Beans",
                "manage_key": "k",
                "message": "queued",
            }
        return {
            "slug": slug,
            "store_name": "Cool Beans",
            "url": f"https://tilla.gudman.xyz/s/{slug}/",
            "product_name": "Beans",
            "price_usdt": 5,
            "manage_key": "k",
        }

    monkeypatch.setattr(engine, "create_store", _fake)


def _pad(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower()[2:]


def _receipt(from_addr, to_addr, value, status="0x1", block=1):
    return {
        "status": status,
        "blockNumber": hex(block),
        "logs": [
            {
                "address": config.USDT0,
                "topics": [config.TRANSFER_TOPIC, _pad(from_addr), _pad(to_addr)],
                "data": hex(value),
                "transactionHash": "0x" + "e" * 64,
                "logIndex": "0x0",
                "blockNumber": hex(block),
            }
        ],
    }


def _mock_tx(monkeypatch, receipt):
    # Bind the REAL signature (cfg, tx_hash, timeout=None). A permissive *args stub
    # accepts any call, which is how a receipt lookup missing its ChainConfig shipped
    # and 500'd every payment; this way a wrong call raises TypeError here instead.
    def _fake(cfg, tx_hash, timeout=None):
        assert cfg is payment.CANONICAL_CHAIN, (
            "verification must pin the canonical chain"
        )
        return receipt

    monkeypatch.setattr(chain, "get_transaction_receipt", _fake)


def _intent(token, description="single-origin coffee beans", theme=None):
    body = {"description": description}
    if theme:
        body["theme"] = theme
    return client.post("/api/merchant/create-store", json=body, headers=_auth(token))


# ------------------------------------------------------------------------- tests
def test_intent_screens_before_payment(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    monkeypatch.setattr(
        screening,
        "screen",
        lambda *_a, **_k: (_ for _ in ()).throw(
            ScreeningBlocked({"risk_level": "high"})
        ),
    )
    r = _intent(token, description="something clearly prohibited")
    assert r.status_code == 422, r.text
    # No intent row was created — nothing to pay for.
    assert r.json()["detail"] == "content did not pass safety screening"


def test_llm_unconfigured_is_503(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    monkeypatch.delenv("TILLA_LLM_KEY", raising=False)
    r = _intent(token)
    assert r.status_code == 503


def test_pay_happy_path_creates_store(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch, slug="cool-beans")

    intent = _intent(token, theme="bold")
    assert intent.status_code == 200, intent.text
    body = intent.json()
    assert body["status"] == "pending"
    assert body["amount_micro"] == FEE
    assert body["pay_to"].lower() == TILLA

    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE))
    pay = client.post(
        f"/api/merchant/create-store/{body['id']}/pay",
        json={"tx_hash": "0x" + "a" * 64},
        headers=_auth(token),
    )
    assert pay.status_code == 200, pay.text
    assert pay.json()["status"] == "live"
    assert pay.json()["slug"] == "cool-beans"
    assert pay.json()["url"] == "/s/cool-beans/"


def test_pay_wrong_amount_rejected(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    cid = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE - 1))  # underpaid
    r = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "b" * 64},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.text


def test_pay_wrong_recipient_rejected(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    cid = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, "0x" + "1" * 40, FEE))  # not Tilla
    r = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "c" * 64},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.text


def _intent_at_block(monkeypatch, token, head):
    """An intent opened with the chain at ``head`` — the floor a fee must clear."""
    monkeypatch.setattr(checkout, "_current_head", lambda: head)
    body = _intent(token).json()
    with SessionLocal() as s:
        assert s.get(StoreCreation, body["id"]).created_block == head
    return body["id"]


def test_pay_rejects_a_fee_transfer_mined_before_the_intent(monkeypatch):
    """The dashboard fee and the agent x402 /create-store fee are the same amount to
    the same address, and the x402 path writes no store_creations row — so without a
    block floor a wallet could replay its own earlier fee transfer for a free second
    store. orders have carried this floor since M3."""
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    cid = _intent_at_block(monkeypatch, token, 900)
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE, block=899))
    r = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "d" * 64},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.text
    assert "predates" in r.json()["detail"]
    with SessionLocal() as s:
        creation = s.get(StoreCreation, cid)
        assert creation.status == "pending"  # nothing was minted
        assert creation.tx_hash is None


def test_pay_accepts_a_fee_transfer_in_the_intent_block(monkeypatch):
    """The boundary: the fee may land in the very block the intent was opened in."""
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch, slug="on-the-floor")
    cid = _intent_at_block(monkeypatch, token, 900)
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE, block=900))
    r = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "d" * 64},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "on-the-floor"


def test_pay_reused_tx_is_409(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    first = _intent(token).json()["id"]
    second = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE))
    tx = "0x" + "d" * 64
    r1 = client.post(
        f"/api/merchant/create-store/{first}/pay",
        json={"tx_hash": tx},
        headers=_auth(token),
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        f"/api/merchant/create-store/{second}/pay",
        json={"tx_hash": tx},
        headers=_auth(token),
    )
    assert r2.status_code == 409, r2.text


def test_creation_is_owner_scoped(monkeypatch):
    owner = Account.create()
    other = Account.create()
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    cid = _intent(_merchant_token(owner)).json()["id"]
    _mock_tx(monkeypatch, _receipt(owner.address, TILLA, FEE))
    # A different signed-in merchant cannot pay (or even see) someone else's intent.
    r = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "9" * 64},
        headers=_auth(_merchant_token(other)),
    )
    assert r.status_code == 404, r.text


def test_generation_outage_then_retry(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)

    def _outage(*_a, **_k):
        raise engine.GenerationUnavailable("model down")

    monkeypatch.setattr(engine, "create_store", _outage)
    cid = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE))
    pay = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "7" * 64},
        headers=_auth(token),
    )
    # Payment verified but generation failed: 'paid', no slug, no re-charge.
    assert pay.status_code == 200, pay.text
    assert pay.json()["status"] == "paid"
    assert pay.json()["slug"] is None

    # Model recovers; retry generates without touching payment.
    _stub_create_store(monkeypatch, slug="recovered-store")
    retry = client.post(f"/api/merchant/create-store/{cid}/retry", headers=_auth(token))
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "live"
    assert retry.json()["slug"] == "recovered-store"


def _paid_but_failed(monkeypatch, token, acct, tx="0x" + "6" * 64) -> int:
    """A creation whose payment verified but whose GENERATED content was BLOCKED —
    status 'failed', tx consumed, no store. The dead end that burned the fee before
    'failed' became retryable.

    A real block raises ScreeningBlocked out of create_store; this used to be faked
    with the pending_screening return instead, which is a DIFFERENT outcome (a store
    row exists, queued for the resweep) and must not be retryable at all.
    """

    def _blocked(*_a, **_k):
        raise ScreeningBlocked({"risk_level": "high"})

    monkeypatch.setattr(engine, "create_store", _blocked)
    cid = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE))
    pay = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": tx},
        headers=_auth(token),
    )
    assert pay.status_code == 200, pay.text
    assert pay.json()["status"] == "failed"
    return cid


def test_failed_generation_regenerates_without_recharge(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    cid = _paid_but_failed(monkeypatch, token, acct)

    # The tx is already consumed, so regenerating must never look at the chain again.
    def _no_receipt(cfg, tx_hash, timeout=None):
        raise AssertionError("a failed creation must not re-verify its payment")

    monkeypatch.setattr(chain, "get_transaction_receipt", _no_receipt)
    _stub_create_store(monkeypatch, slug="second-try")
    r = client.post(f"/api/merchant/create-store/{cid}/retry", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "live"
    assert r.json()["slug"] == "second-try"


def test_pending_resumes_the_open_creation_owner_scoped(monkeypatch):
    """A reload has nothing in memory; the open creation must come back from the
    server — and only ever the caller's own."""
    owner = Account.create()
    other = Account.create()
    token = _merchant_token(owner)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    cid = _intent(token, description="hand-thrown mugs", theme="bold").json()["id"]
    _intent(_merchant_token(other), description="someone else's shop")

    r = client.get("/api/merchant/create-store/pending", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fee_micro"] == FEE
    assert body["fee_usdt"] == "0.05"
    assert [c["id"] for c in body["creations"]] == [cid]  # never the other merchant's
    open_row = body["creations"][0]
    assert open_row["status"] == "pending"
    assert open_row["description"] == "hand-thrown mugs"
    assert open_row["theme"] == "bold"
    assert open_row["pay_to"].lower() == TILLA
    assert open_row["amount_usdt"] == "0.05"
    assert open_row["created_at"]


def test_pending_keeps_failed_and_drops_live(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    failed = _paid_but_failed(monkeypatch, token, acct)
    _stub_create_store(monkeypatch, slug="all-good")
    live = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE))
    client.post(
        f"/api/merchant/create-store/{live}/pay",
        json={"tx_hash": "0x" + "3" * 64},
        headers=_auth(token),
    )
    body = client.get("/api/merchant/create-store/pending", headers=_auth(token)).json()
    ids = [c["id"] for c in body["creations"]]
    assert failed in ids  # still actionable — regenerate at no charge
    assert live not in ids  # nothing left to do


def test_pending_requires_merchant_auth():
    assert client.get("/api/merchant/create-store/pending").status_code == 401


def test_pay_polls_a_not_yet_mined_receipt(monkeypatch):
    """The dashboard posts the hash as soon as the wallet broadcasts it, so the first
    receipt lookups legitimately return None — that must not read as 'not found'."""
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch, slug="mined-late")
    cid = _intent(token).json()["id"]
    mined = _receipt(acct.address, TILLA, FEE)
    seen = []

    def _fake(cfg, tx_hash, timeout=None):
        assert cfg is payment.CANONICAL_CHAIN, (
            "verification must pin the canonical chain"
        )
        seen.append(tx_hash)
        return mined if len(seen) > 2 else None

    monkeypatch.setattr(chain, "get_transaction_receipt", _fake)
    monkeypatch.setattr(self_serve, "_RECEIPT_RETRY_SECONDS", 0)
    r = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "5" * 64},
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "live"
    assert len(seen) == 3


def test_pay_receipt_polling_is_bounded(monkeypatch):
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch)
    cid = _intent(token).json()["id"]
    seen = []

    def _never(cfg, tx_hash, timeout=None):
        seen.append(tx_hash)
        return None

    monkeypatch.setattr(chain, "get_transaction_receipt", _never)
    monkeypatch.setattr(self_serve, "_RECEIPT_RETRY_SECONDS", 0)
    r = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "4" * 64},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "transaction not found"
    assert len(seen) == self_serve._RECEIPT_TRIES


def test_pay_runs_the_real_nested_session_write(monkeypatch, tmp_path):
    """The REAL engine.create_store, not the stub — the nested-session path every
    other test mocks away.

    pay_and_create flushed the pending->paid UPDATE and then called
    engine.create_store, which opens its OWN SessionLocal and INSERTs. SQLite has one
    writer, so the nested INSERT blocked on this session's open write txn and died on
    busy_timeout ("database is locked"); the route's rollback then reverted 'paid',
    leaving a merchant who HAD paid with a 'pending' creation. Every real paid
    creation 500'd in production while the suite stayed green, because
    _stub_create_store replaced the one call that deadlocks.

    Only the two external calls are mocked here (LLM + screening); every session and
    DB write is real.
    """
    from app import engine

    monkeypatch.setenv("TILLA_STORES_DIR", str(tmp_path))
    monkeypatch.setattr(
        engine,
        "generate",
        lambda desc: {
            "store_name": "Nested",
            "emoji": "x",
            "tagline": "t",
            "hero_subcopy": "h",
            "product_name": "Thing",
            "product_blurb": "b",
            "price_usdt": 5,
            "cta": "Buy",
            "palette": {
                "primary": "#111",
                "accent": "#222",
                "bg": "#fff",
                "text": "#000",
            },
        },
    )
    _allow_screen(monkeypatch)
    monkeypatch.setattr(self_serve, "_verify_payment", lambda *a, **k: None)

    with SessionLocal() as session:
        creation = StoreCreation(
            merchant_addr="0x" + "a" * 40,
            description="nested session store",
            theme=None,
            expected_micro=FEE,
            pay_to=TILLA,
            status="pending",
        )
        session.add(creation)
        session.commit()
        out = self_serve.pay_and_create(session, creation, "0x" + "b" * 64)
        session.commit()
        assert out.status == "live", "nested engine session must not deadlock"
        assert out.slug
        assert out.tx_hash == "0x" + "b" * 64  # payment durably consumed


def test_screening_queued_creation_cannot_mint_a_second_store(monkeypatch):
    """One fee must never produce two stores.

    When generated content is QUEUED (screening unavailable) create_store has already
    committed a Store row awaiting the resweep. Marking the creation merely 'failed'
    left it retryable, so a retry generated a SECOND store from the same payment while
    the queued one still went live. The creation now claims the queued slug, and
    retry() refuses any creation that has one.
    """
    acct = Account.create()
    token = _merchant_token(acct)
    _allow_screen(monkeypatch)
    _stub_create_store(monkeypatch, slug="queued-store", pending=True)
    cid = _intent(token).json()["id"]
    _mock_tx(monkeypatch, _receipt(acct.address, TILLA, FEE))
    pay = client.post(
        f"/api/merchant/create-store/{cid}/pay",
        json={"tx_hash": "0x" + "7" * 64},
        headers=_auth(token),
    )
    assert pay.status_code == 200, pay.text
    assert pay.json()["slug"] == "queued-store"  # owns the queued store
    assert pay.json()["status"] != "live"  # not published yet

    r = client.post(f"/api/merchant/create-store/{cid}/retry", headers=_auth(token))
    assert r.status_code == 409, "a queued creation must not regenerate"
    assert r.json()["detail"] == "nothing to retry"
