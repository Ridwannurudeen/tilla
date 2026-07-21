"""Phase 3 growth-kit tests (no network, no funds, no on-chain).

Covers the merchant-gated POST/GET /api/stores/{slug}/growth-kit: the auth matrix
(manage key OR owning merchant, IDOR-blocked otherwise), the live-store 409, the
fail-closed LLM + screening contract (outage/malformed/schema-violation -> 503,
BLOCK -> 422, screening unavailable -> 503, none of which persist a kit), the happy
path (validated kit JSON + nosniff + one event_log row), the GET read-back, and the
6/hour rate limit. The LLM seam is monkeypatched and Warden is respx-mocked, so no
network is ever touched; TILLA_WARDEN_PAID is off so no paid path is constructed.
"""

import hashlib
import json

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main
from app import engine
from app.config import WARDEN_SCREEN_URL
from app.db import SessionLocal
from app.delivery import mint_manage_key
from app.engine import GenerationUnavailable
from app.models import EventLog, Merchant, Store

client = TestClient(main.app)

GOOD_KIT = {
    "social_posts": [
        "Launch day is here — meet the tool that saves solo founders hours.",
        "Stop wrestling spreadsheets. Start shipping. Link in bio.",
        "Three clicks to a live crypto storefront. Your move.",
    ],
    "launch_tweet": "We just went live. One product, zero friction, paid in USDT on X Layer.",
    "email_subject": "Your storefront is live — here's how to launch it today",
}

CONTENT = {
    "store_name": "Flowstate",
    "tagline": "ship faster",
    "hero_headline": "Your storefront, live in seconds",
    "hero_subcopy": "A crisp one-product store with crypto checkout.",
    "product_name": "Flowstate Pro",
    "product_blurb": "Everything a solo founder needs to sell online.",
    "price_usdt": 9,
}


def _auth(key: str) -> dict:
    return {"Authorization": "Bearer " + key}


def _store_with_key(make_store, slug="grow1", status="live", content=None):
    """A store with a known manage key and a persisted content blob."""
    sid = make_store(slug=slug, status=status)
    key, key_hash = mint_manage_key()
    with SessionLocal() as s:
        st = s.get(Store, sid)
        st.manage_key_hash = key_hash
        st.content = dict(content or CONTENT)
        s.commit()
    return sid, key


def _fake_llm(monkeypatch, kit_dict, usage=None):
    """Patch the shared generation seam to return `kit_dict` as Anthropic would."""

    def _inner(prompt):
        body = {"content": [{"text": json.dumps(kit_dict)}]}
        if usage is not None:
            body["usage"] = usage
        return body

    monkeypatch.setattr(engine, "_post_generation", _inner)


def _mock_allow():
    return respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "ALLOW", "risk_level": "none"}
        )
    )


def _kit_event_count(sid: int) -> int:
    with SessionLocal() as s:
        return s.scalar(
            select(func.count())
            .select_from(EventLog)
            .where(
                EventLog.store_id == sid,
                EventLog.event == "growth.kit_generated",
            )
        )


# ======================================================= happy path
@respx.mock
def test_growth_kit_happy_path(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT, usage={"input_tokens": 120, "output_tokens": 90})
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="grow-happy")

    r = client.post("/api/stores/grow-happy/growth-kit", headers=_auth(key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == GOOD_KIT
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert _kit_event_count(sid) == 1


# ======================================================= auth matrix
@respx.mock
def test_growth_kit_missing_key_401(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    sid, _ = _store_with_key(make_store, slug="grow-nokey")
    r = client.post("/api/stores/grow-nokey/growth-kit")
    assert r.status_code == 401
    assert _kit_event_count(sid) == 0


@respx.mock
def test_growth_kit_wrong_key_401(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    sid, _ = _store_with_key(make_store, slug="grow-wrong")
    r = client.post("/api/stores/grow-wrong/growth-kit", headers=_auth("nope"))
    assert r.status_code == 401
    assert _kit_event_count(sid) == 0


@respx.mock
def test_growth_kit_owning_merchant_api_key_ok(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    sid, _ = _store_with_key(make_store, slug="grow-owner")
    api_key = "owner-api-key-" + "c" * 20
    with SessionLocal() as s:
        store = s.get(Store, sid)
        merchant = s.get(Merchant, store.merchant_id)
        merchant.api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        s.commit()
    r = client.post("/api/stores/grow-owner/growth-kit", headers=_auth(api_key))
    assert r.status_code == 200, r.text
    assert _kit_event_count(sid) == 1


@respx.mock
def test_growth_kit_non_owner_merchant_401(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    sid, _ = _store_with_key(make_store, slug="grow-idor")
    # A different merchant with its own API key must NOT reach this store's kit.
    other_key = "other-api-key-" + "d" * 20
    with SessionLocal() as s:
        other = Merchant(
            wallet_address="0x" + "e" * 40,
            api_key_hash=hashlib.sha256(other_key.encode()).hexdigest(),
        )
        s.add(other)
        s.commit()
    r = client.post("/api/stores/grow-idor/growth-kit", headers=_auth(other_key))
    assert r.status_code == 401
    assert _kit_event_count(sid) == 0


# ======================================================= live-store gate
@respx.mock
def test_growth_kit_non_live_store_409(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    sid, key = _store_with_key(
        make_store, slug="grow-pending", status="pending_screening"
    )
    r = client.post("/api/stores/grow-pending/growth-kit", headers=_auth(key))
    assert r.status_code == 409
    assert _kit_event_count(sid) == 0


# ======================================================= LLM fail-closed
@respx.mock
def test_growth_kit_llm_outage_503_no_event(make_store, monkeypatch):
    def _boom(prompt):
        raise GenerationUnavailable("anthropic down")

    monkeypatch.setattr(engine, "_post_generation", _boom)
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="grow-outage")
    r = client.post("/api/stores/grow-outage/growth-kit", headers=_auth(key))
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    assert _kit_event_count(sid) == 0


@respx.mock
def test_growth_kit_schema_violation_503_no_event(make_store, monkeypatch):
    # Four posts + an over-long post both violate the strict GrowthKit shape.
    bad = {
        "social_posts": ["a", "b", "c", "d"],
        "launch_tweet": "x" * 281,
        "email_subject": "hi",
    }
    _fake_llm(monkeypatch, bad)
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="grow-bad")
    r = client.post("/api/stores/grow-bad/growth-kit", headers=_auth(key))
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    assert _kit_event_count(sid) == 0


@respx.mock
def test_growth_kit_non_json_output_503(make_store, monkeypatch):
    def _inner(prompt):
        return {"content": [{"text": "sorry, I cannot do that"}]}

    monkeypatch.setattr(engine, "_post_generation", _inner)
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="grow-nojson")
    r = client.post("/api/stores/grow-nojson/growth-kit", headers=_auth(key))
    assert r.status_code == 503
    assert _kit_event_count(sid) == 0


# ======================================================= screening fail-closed
@respx.mock
def test_growth_kit_screening_block_422_no_event(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    sid, key = _store_with_key(make_store, slug="grow-block")
    r = client.post("/api/stores/grow-block/growth-kit", headers=_auth(key))
    assert r.status_code == 422
    assert _kit_event_count(sid) == 0
    # Nothing to read back.
    g = client.get("/api/stores/grow-block/growth-kit", headers=_auth(key))
    assert g.status_code == 404


@respx.mock
def test_growth_kit_screening_unavailable_503_no_event(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    respx.post(WARDEN_SCREEN_URL).mock(return_value=httpx.Response(500))
    sid, key = _store_with_key(make_store, slug="grow-screendown")
    r = client.post("/api/stores/grow-screendown/growth-kit", headers=_auth(key))
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    assert _kit_event_count(sid) == 0


# ======================================================= GET read-back
@respx.mock
def test_growth_kit_get_returns_latest(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="grow-read")
    client.post("/api/stores/grow-read/growth-kit", headers=_auth(key))
    r = client.get("/api/stores/grow-read/growth-kit", headers=_auth(key))
    assert r.status_code == 200, r.text
    assert r.json() == GOOD_KIT
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_growth_kit_get_404_when_none(make_store):
    sid, key = _store_with_key(make_store, slug="grow-empty")
    r = client.get("/api/stores/grow-empty/growth-kit", headers=_auth(key))
    assert r.status_code == 404


def test_growth_kit_get_requires_auth(make_store):
    _store_with_key(make_store, slug="grow-getauth")
    r = client.get("/api/stores/grow-getauth/growth-kit", headers=_auth("nope"))
    assert r.status_code == 401


# ======================================================= rate limit
@respx.mock
def test_growth_kit_rate_limited(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    # 6/hour: the 7th POST in the window is rejected before the handler runs.
    _, key = _store_with_key(make_store, slug="grow-rl")
    codes = [
        client.post("/api/stores/grow-rl/growth-kit", headers=_auth(key)).status_code
        for _ in range(7)
    ]
    assert 429 in codes
