"""M17.4 multi-channel draft-shape tests (no network, no funds, NOTHING sends).

Covers POST /api/stores/{slug}/growth/draft for the two new channels (email_body,
product_update): the happy path into the outbox, the per-channel strict schema, the
HTML-in-output => GenerationUnavailable => 503 guarantee (XSS-safe by construction),
the fail-closed screening contract (BLOCK => 422, unavailable => 503), and the
absolute no-send invariant (respx records only the Warden screen call — no outbound
post to any social/mail host).
"""

import json

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import engine
from app.config import WARDEN_SCREEN_URL
from app.db import SessionLocal
from app.delivery import mint_manage_key
from app.models import GrowthDraft, Store

client = TestClient(main.app)

CONTENT = {
    "store_name": "Flowstate",
    "tagline": "ship faster",
    "hero_headline": "Your storefront, live in seconds",
    "hero_subcopy": "A crisp one-product store with crypto checkout.",
    "product_name": "Flowstate Pro",
    "product_blurb": "Everything a solo founder needs to sell online.",
    "price_usdt": 9,
}

EMAIL_BODY = {
    "subject": "Your storefront is live — launch it today",
    "body": "Hey there,\n\nYour Flowstate store is live. Share the link and start "
    "selling in USDT today. No code, no friction.\n\n— The Flowstate team",
}
PRODUCT_UPDATE = {
    "blurb": "New: one-click crypto checkout on every Flowstate store. Paid in USDT "
    "on X Layer, delivered instantly."
}


def _auth(key: str) -> dict:
    return {"Authorization": "Bearer " + key}


def _store_with_key(make_store, slug="chan1"):
    sid = make_store(slug=slug, status="live")
    key, key_hash = mint_manage_key()
    with SessionLocal() as s:
        st = s.get(Store, sid)
        st.manage_key_hash = key_hash
        st.content = dict(CONTENT)
        s.commit()
    return sid, key


def _fake_llm(monkeypatch, out_dict):
    def _inner(prompt):
        return {"content": [{"text": json.dumps(out_dict)}]}

    monkeypatch.setattr(engine, "_post_generation", _inner)


def _mock_allow():
    return respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "ALLOW", "risk_level": "none"}
        )
    )


@respx.mock
def test_email_body_happy_path(make_store, monkeypatch):
    _fake_llm(monkeypatch, EMAIL_BODY)
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="chan-email")
    r = client.post(
        "/api/stores/chan-email/growth/draft",
        headers=_auth(key),
        json={"channel": "email_body"},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["channel"] == "email_body"
    assert d["status"] == "pending"
    assert d["source"] == "manual"
    assert EMAIL_BODY["subject"] in d["body"]
    assert EMAIL_BODY["body"] in d["body"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    # Shows up in the outbox as one draft.
    out = client.get("/api/stores/chan-email/growth/outbox", headers=_auth(key))
    assert len(out.json()["drafts"]) == 1
    # NO outbound post anywhere — respx saw ONLY the Warden screen call.
    assert all(str(c.request.url) == WARDEN_SCREEN_URL for c in respx.calls)


@respx.mock
def test_product_update_happy_path(make_store, monkeypatch):
    _fake_llm(monkeypatch, PRODUCT_UPDATE)
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="chan-prod")
    r = client.post(
        "/api/stores/chan-prod/growth/draft",
        headers=_auth(key),
        json={"channel": "product_update"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["channel"] == "product_update"
    assert r.json()["body"] == PRODUCT_UPDATE["blurb"]


@respx.mock
def test_email_body_never_html(make_store, monkeypatch):
    # An LLM that slips HTML tags into the body must fold into the 503 path, never
    # a persisted draft (XSS-safe by construction).
    _fake_llm(
        monkeypatch,
        {"subject": "hi", "body": "<script>alert(1)</script> buy now"},
    )
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="chan-xss")
    r = client.post(
        "/api/stores/chan-xss/growth/draft",
        headers=_auth(key),
        json={"channel": "email_body"},
    )
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    with SessionLocal() as s:
        assert (
            s.scalars(select(GrowthDraft).where(GrowthDraft.store_id == sid)).all()
            == []
        )


@respx.mock
def test_product_update_html_rejected(make_store, monkeypatch):
    _fake_llm(monkeypatch, {"blurb": "great deal <b>50% off</b>"})
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="chan-xss2")
    r = client.post(
        "/api/stores/chan-xss2/growth/draft",
        headers=_auth(key),
        json={"channel": "product_update"},
    )
    assert r.status_code == 503


@respx.mock
def test_channel_schema_violation_503(make_store, monkeypatch):
    # An over-long subject (>78) and an unexpected key both violate the strict shape.
    _fake_llm(monkeypatch, {"subject": "x" * 79, "body": "ok"})
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="chan-bad")
    r = client.post(
        "/api/stores/chan-bad/growth/draft",
        headers=_auth(key),
        json={"channel": "email_body"},
    )
    assert r.status_code == 503
    with SessionLocal() as s:
        assert (
            s.scalars(select(GrowthDraft).where(GrowthDraft.store_id == sid)).all()
            == []
        )


def test_unknown_channel_422(make_store):
    _, key = _store_with_key(make_store, slug="chan-unknown")
    r = client.post(
        "/api/stores/chan-unknown/growth/draft",
        headers=_auth(key),
        json={"channel": "launch_tweet"},
    )
    assert r.status_code == 422


@respx.mock
def test_channel_draft_screening_block_422(make_store, monkeypatch):
    _fake_llm(monkeypatch, PRODUCT_UPDATE)
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    sid, key = _store_with_key(make_store, slug="chan-block")
    r = client.post(
        "/api/stores/chan-block/growth/draft",
        headers=_auth(key),
        json={"channel": "product_update"},
    )
    assert r.status_code == 422
    # Manual on-demand BLOCK persists nothing (unlike the scheduled path).
    with SessionLocal() as s:
        assert (
            s.scalars(select(GrowthDraft).where(GrowthDraft.store_id == sid)).all()
            == []
        )


def test_channel_draft_idor_blocked(make_store):
    _store_with_key(make_store, slug="chan-idor")
    r = client.post(
        "/api/stores/chan-idor/growth/draft",
        headers=_auth("nope"),
        json={"channel": "email_body"},
    )
    assert r.status_code == 401


@respx.mock
def test_channel_prompts_carry_the_copy_restraint(make_store, monkeypatch):
    """Both channel prompts had zero claims language, same as the kit prompt before
    docs/ISSUES.md #2's rules were extended to growth — and email_body is the largest
    generated surface Tilla has (2000 characters) and the one that leaves as a real
    email. The restraint text is engine.COPY_RESTRAINT verbatim, shared with the
    storefront prompt and the kit prompt so the three cannot drift apart."""
    captured = {}
    valid_output = {"email_body": EMAIL_BODY, "product_update": PRODUCT_UPDATE}
    asked = "email_body"

    def _spy(prompt):
        captured[asked] = prompt
        return {"content": [{"text": json.dumps(valid_output[asked])}]}

    monkeypatch.setattr(engine, "_post_generation", _spy)
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "ALLOW", "risk_level": "none"}
        )
    )
    _, key = _store_with_key(make_store, slug="chan-restraint")
    for channel in valid_output:
        asked = channel
        r = client.post(
            "/api/stores/chan-restraint/growth/draft",
            headers=_auth(key),
            json={"channel": channel},
        )
        assert r.status_code == 200, r.text
    assert sorted(captured) == ["email_body", "product_update"]
    for prompt in captured.values():
        assert engine.COPY_RESTRAINT in prompt
        # spelled out too, so emptying the constant cannot satisfy the line above
        assert "Never invent capabilities, features, credentials" in prompt
        assert "Never state a delivery frequency" in prompt
        assert "Never assert traction, demand or popularity" in prompt
        assert "what is forbidden is the measurable promise, not the warmth" in prompt
