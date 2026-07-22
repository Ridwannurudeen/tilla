"""Roadmap Phase 3 escrow (commissioned/custom-build job machine) tests.

NON-CUSTODIAL discipline is the headline invariant: the two fund-relevant steps
(funded, completed) are each a VERIFIED on-chain USDT0 transfer the parties signed
themselves — Tilla records the hash and NEVER sends. The deposit/release receipts are
respx-mocked httpx (no real network, no funds); Warden screening is exercised through
the real ``screen`` path with the endpoint respx-mocked (the test_reviews pattern).

Covers: the full open->budget_set->funded->submitted->completed lifecycle; the optional
evaluator gating release; fail-closed party checks on every action; the ESCROW_ENABLED
dormant gate (503 when off); brief/deliverable screening; exact-amount + pinned-wallet
verification rejections; tx-reuse 409; cancel/dispute; and a source-level assertion that
no fund-moving code path exists.
"""

import json
import pathlib
import secrets

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.escrow
import app.main as main
from app import chain, config, delivery
from app.config import WARDEN_SCREEN_URL
from app.db import SessionLocal
from app.models import CommissionJob, Product, Store, get_or_create_merchant

client = TestClient(main.app)

SLUG = "escrowshop"
BUYER = "0x" + "b" * 40
PROVIDER = "0x" + "a" * 40  # the store's pay_to == provider identity
EVALUATOR = "0x" + "e" * 40
ESCROW = "0x" + "c" * 40  # the holding wallet (Tilla holds no keys to it)
BUDGET = 5_000_000  # 5 USDT


@pytest.fixture
def escrow_on(monkeypatch):
    """Flip the phase-gate on for a test (default is OFF, exercised by the gate test)."""
    monkeypatch.setattr(config, "ESCROW_ENABLED", True)


# ------------------------------------------------------------ on-chain mocking
def _log(from_addr, to_addr, value, tx_hash, log_index=0, block=100):
    return {
        "address": config.USDT0,
        "topics": [
            config.TRANSFER_TOPIC,
            chain.pad_address(from_addr),
            chain.pad_address(to_addr),
        ],
        "data": hex(value),
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
        "blockNumber": hex(block),
    }


def _receipt(logs, status="0x1", block=100):
    return {"status": status, "blockNumber": hex(block), "logs": logs}


class Rpc:
    def __init__(self, receipts=None):
        self.receipts = receipts or {}

    def handler(self, request):
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        result = (
            self.receipts.get(params[0].lower())
            if method == "eth_getTransactionReceipt"
            else None
        )
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    def install(self):
        respx.post(config.RPC_URL).mock(side_effect=self.handler)


def _mock_allow():
    respx.post(WARDEN_SCREEN_URL).mock(
        side_effect=lambda request: httpx.Response(200, json={"verdict": "ALLOW"})
    )


def _auth(addr):
    return {"Authorization": f"Bearer {delivery.mint_session_token(addr)}"}


def _seed_store(slug=SLUG, pay_to=PROVIDER):
    with SessionLocal() as s:
        me = get_or_create_merchant(s, pay_to)
        store = Store(
            slug=slug,
            merchant_id=me.id,
            status="live",
            pay_to=pay_to,
            theme="original.html",
            content={"store_name": "Shoppe"},
        )
        s.add(store)
        s.flush()
        s.add(
            Product(store_id=store.id, name="Custom build", price_micro=1, active=True)
        )
        s.commit()
        return store.id


def _tx():
    return "0x" + secrets.token_hex(32)


def _create(evaluator=None, buyer=BUYER, title="Build me a logo", brief="vector, blue"):
    body = {"store": SLUG, "title": title, "brief": brief}
    if evaluator:
        body["evaluator_addr"] = evaluator
    return client.post("/api/escrow/jobs", headers=_auth(buyer), json=body)


def _to_funded(evaluator=None, dep_tx=None, block=100):
    """Drive a fresh job to 'funded' (create -> budget -> fund), returning its id."""
    dep_tx = dep_tx or _tx()
    job = _create(evaluator=evaluator).json()["id"]
    client.post(
        f"/api/escrow/jobs/{job}/budget",
        headers=_auth(BUYER),
        json={"budget_micro": BUDGET, "escrow_addr": ESCROW},
    )
    Rpc(
        {
            dep_tx: _receipt(
                [_log(BUYER, ESCROW, BUDGET, dep_tx, block=block)], block=block
            )
        }
    ).install()
    r = client.post(
        f"/api/escrow/jobs/{job}/fund", headers=_auth(BUYER), json={"tx_hash": dep_tx}
    )
    assert r.status_code == 200, r.text
    return job


# ------------------------------------------------------------------- lifecycle
@respx.mock
def test_full_lifecycle_no_evaluator(escrow_on):
    _seed_store()
    _mock_allow()
    r = _create()
    assert r.status_code == 201, r.text
    job = r.json()["id"]
    assert r.json()["status"] == "open"
    assert r.json()["provider"] == PROVIDER

    r = client.post(
        f"/api/escrow/jobs/{job}/budget",
        headers=_auth(BUYER),
        json={"budget_micro": BUDGET, "escrow_addr": ESCROW},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "budget_set"
    assert r.json()["budget_micro"] == BUDGET and r.json()["escrow_addr"] == ESCROW

    dep = _tx()
    Rpc({dep: _receipt([_log(BUYER, ESCROW, BUDGET, dep)])}).install()
    r = client.post(
        f"/api/escrow/jobs/{job}/fund", headers=_auth(BUYER), json={"tx_hash": dep}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "funded" and r.json()["funded_tx"] == dep

    r = client.post(
        f"/api/escrow/jobs/{job}/submit",
        headers=_auth(PROVIDER),
        json={"deliverable": "delivered: ipfs://logo"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"

    rel = _tx()
    Rpc(
        {rel: _receipt([_log(ESCROW, PROVIDER, BUDGET, rel, block=200)], block=200)}
    ).install()
    r = client.post(
        f"/api/escrow/jobs/{job}/release", headers=_auth(BUYER), json={"tx_hash": rel}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed" and r.json()["released_tx"] == rel

    with SessionLocal() as s:
        row = s.get(CommissionJob, job)
        assert row.status == "completed"
        assert row.funded_tx == dep and row.released_tx == rel
        assert row.funded_at and row.completed_at


@respx.mock
def test_evaluator_gates_release(escrow_on):
    _seed_store()
    _mock_allow()
    job = _to_funded(evaluator=EVALUATOR)
    client.post(
        f"/api/escrow/jobs/{job}/submit",
        headers=_auth(PROVIDER),
        json={"deliverable": "done"},
    )
    rel = _tx()
    Rpc(
        {rel: _receipt([_log(ESCROW, PROVIDER, BUDGET, rel, block=200)], block=200)}
    ).install()
    # the buyer may NOT release when an evaluator gates the job
    bad = client.post(
        f"/api/escrow/jobs/{job}/release", headers=_auth(BUYER), json={"tx_hash": rel}
    )
    assert bad.status_code == 403
    # the evaluator can
    ok = client.post(
        f"/api/escrow/jobs/{job}/release",
        headers=_auth(EVALUATOR),
        json={"tx_hash": rel},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "completed"


# ---------------------------------------------------------- fail-closed parties
@respx.mock
def test_wrong_party_actions_are_403(escrow_on):
    _seed_store()
    _mock_allow()
    job = _create().json()["id"]
    # a non-buyer cannot set the budget
    r = client.post(
        f"/api/escrow/jobs/{job}/budget",
        headers=_auth(PROVIDER),
        json={"budget_micro": BUDGET, "escrow_addr": ESCROW},
    )
    assert r.status_code == 403
    # the buyer sets it, then a non-buyer cannot fund
    client.post(
        f"/api/escrow/jobs/{job}/budget",
        headers=_auth(BUYER),
        json={"budget_micro": BUDGET, "escrow_addr": ESCROW},
    )
    r = client.post(
        f"/api/escrow/jobs/{job}/fund", headers=_auth(PROVIDER), json={"tx_hash": _tx()}
    )
    assert r.status_code == 403


@respx.mock
def test_only_provider_may_submit(escrow_on):
    _seed_store()
    _mock_allow()
    job = _to_funded()
    # the buyer is not the provider — cannot submit the deliverable
    r = client.post(
        f"/api/escrow/jobs/{job}/submit",
        headers=_auth(BUYER),
        json={"deliverable": "x"},
    )
    assert r.status_code == 403
    # a stranger cannot either
    r = client.post(
        f"/api/escrow/jobs/{job}/submit",
        headers=_auth("0x" + "d" * 40),
        json={"deliverable": "x"},
    )
    assert r.status_code == 403


@respx.mock
def test_get_is_party_scoped(escrow_on):
    _seed_store()
    _mock_allow()
    job = _create(evaluator=EVALUATOR).json()["id"]
    # each party can read it
    for who in (BUYER, PROVIDER, EVALUATOR):
        assert (
            client.get(f"/api/escrow/jobs/{job}", headers=_auth(who)).status_code == 200
        )
    # a non-party gets an opaque 404 (no job oracle)
    assert (
        client.get(
            f"/api/escrow/jobs/{job}", headers=_auth("0x" + "f" * 40)
        ).status_code
        == 404
    )


# ------------------------------------------------------------ verify-only rules
@respx.mock
def test_deposit_wrong_amount_rejected(escrow_on):
    _seed_store()
    _mock_allow()
    job = _create().json()["id"]
    client.post(
        f"/api/escrow/jobs/{job}/budget",
        headers=_auth(BUYER),
        json={"budget_micro": BUDGET, "escrow_addr": ESCROW},
    )
    dep = _tx()
    # a transfer of the wrong amount does not fund the job (exact-amount discipline)
    Rpc({dep: _receipt([_log(BUYER, ESCROW, BUDGET + 1, dep)])}).install()
    r = client.post(
        f"/api/escrow/jobs/{job}/fund", headers=_auth(BUYER), json={"tx_hash": dep}
    )
    assert r.status_code == 400
    with SessionLocal() as s:
        assert s.get(CommissionJob, job).status == "budget_set"  # unchanged


@respx.mock
def test_deposit_to_wrong_wallet_rejected(escrow_on):
    _seed_store()
    _mock_allow()
    job = _create().json()["id"]
    client.post(
        f"/api/escrow/jobs/{job}/budget",
        headers=_auth(BUYER),
        json={"budget_micro": BUDGET, "escrow_addr": ESCROW},
    )
    dep = _tx()
    # a transfer to some other wallet is not a deposit to escrow_addr — no match
    Rpc({dep: _receipt([_log(BUYER, "0x" + "9" * 40, BUDGET, dep)])}).install()
    r = client.post(
        f"/api/escrow/jobs/{job}/fund", headers=_auth(BUYER), json={"tx_hash": dep}
    )
    assert r.status_code == 400


@respx.mock
def test_release_predating_deposit_rejected(escrow_on):
    _seed_store()
    _mock_allow()
    job = _to_funded(block=100)
    client.post(
        f"/api/escrow/jobs/{job}/submit",
        headers=_auth(PROVIDER),
        json={"deliverable": "d"},
    )
    rel = _tx()
    # a release mined BEFORE the deposit block is rejected (the M9 block floor)
    Rpc(
        {rel: _receipt([_log(ESCROW, PROVIDER, BUDGET, rel, block=50)], block=50)}
    ).install()
    r = client.post(
        f"/api/escrow/jobs/{job}/release", headers=_auth(BUYER), json={"tx_hash": rel}
    )
    assert r.status_code == 400
    with SessionLocal() as s:
        assert s.get(CommissionJob, job).status == "submitted"  # unchanged


@respx.mock
def test_reused_deposit_tx_is_409(escrow_on):
    _seed_store()
    _mock_allow()
    dep = _tx()
    _to_funded(dep_tx=dep)  # first job consumes the deposit tx
    # a second job cannot fund off the same on-chain transfer
    job2 = _create().json()["id"]
    client.post(
        f"/api/escrow/jobs/{job2}/budget",
        headers=_auth(BUYER),
        json={"budget_micro": BUDGET, "escrow_addr": ESCROW},
    )
    Rpc({dep: _receipt([_log(BUYER, ESCROW, BUDGET, dep)])}).install()
    r = client.post(
        f"/api/escrow/jobs/{job2}/fund", headers=_auth(BUYER), json={"tx_hash": dep}
    )
    assert r.status_code == 409


# ------------------------------------------------------------ cancel / dispute
@respx.mock
def test_buyer_cancels_before_funding(escrow_on):
    _seed_store()
    _mock_allow()
    job = _create().json()["id"]
    r = client.post(f"/api/escrow/jobs/{job}/cancel", headers=_auth(BUYER))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # a cancelled job can no longer be funded
    r = client.post(
        f"/api/escrow/jobs/{job}/budget",
        headers=_auth(BUYER),
        json={"budget_micro": BUDGET, "escrow_addr": ESCROW},
    )
    assert r.status_code == 409


@respx.mock
def test_either_party_may_dispute_a_funded_job(escrow_on):
    _seed_store()
    _mock_allow()
    job = _to_funded()
    # the provider raises a dispute on the funded job
    r = client.post(f"/api/escrow/jobs/{job}/dispute", headers=_auth(PROVIDER))
    assert r.status_code == 200 and r.json()["status"] == "disputed"
    # a non-party cannot dispute
    job2 = _to_funded()
    r = client.post(f"/api/escrow/jobs/{job2}/dispute", headers=_auth("0x" + "7" * 40))
    assert r.status_code == 403


# --------------------------------------------------------------- screening gate
@respx.mock
def test_brief_is_screened_block_is_422(escrow_on):
    _seed_store()
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    r = _create(brief="something unsafe")
    assert r.status_code == 422
    assert r.json()["detail"] == "content did not pass safety screening"
    with SessionLocal() as s:
        assert s.scalar(select(CommissionJob)) is None  # nothing stored


# --------------------------------------------------------------- dormant gate
def test_dormant_by_default_503():
    # The phase-gate is OFF by default: every endpoint 503s (checked before auth).
    assert config.ESCROW_ENABLED is False
    _seed_store()
    probes = [
        ("post", "/api/escrow/jobs", {"store": SLUG, "title": "t", "brief": "b"}),
        ("get", "/api/escrow/jobs/whatever", None),
        ("post", "/api/escrow/jobs/whatever/fund", {"tx_hash": "0x" + "1" * 64}),
        ("post", "/api/escrow/jobs/whatever/release", {"tx_hash": "0x" + "1" * 64}),
    ]
    for method, path, body in probes:
        r = client.request(method, path, headers=_auth(BUYER), json=body)
        assert r.status_code == 503, (path, r.status_code)


# ------------------------------------------------ NON-CUSTODIAL: no fund-moving code
def test_no_fund_moving_code_path_exists():
    """The escrow module is verify-only: it reads receipts and records hashes, and
    contains no signing/sending primitive that could move funds."""
    src = pathlib.Path(app.escrow.__file__).read_text()
    assert "get_transaction_receipt" in src  # it VERIFIES on-chain
    for forbidden in (
        "send_transaction",
        "sendTransaction",
        "sign_transaction",
        "eth_sendRawTransaction",
        "eth_account",
        "private_key",
        "PrivateKey",
        "signTransaction",
    ):
        assert forbidden not in src, f"escrow must never {forbidden}"
