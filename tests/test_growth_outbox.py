"""M17.1 draft-outbox tests — no network, no funds, no on-chain, NOTHING sends.

Covers the growth_drafts outbox fanned from a screened kit: the fan itself, the
merchant/IDOR gate on every read+action path, the blocked-body withholding, draft
immutability across approve, and the INV-1 guarantee that mark-published records a
human post without ANY outbound call (respx asserts zero) plus the static grep that
this module imports no sender at all.
"""

import hashlib
import json
import pathlib

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

GOOD_KIT = {
    "social_posts": [
        "Launch day is here — meet the tool that saves solo founders hours.",
        "Stop wrestling spreadsheets. Start shipping. Link in bio.",
        "Three clicks to a live crypto storefront. Your move.",
    ],
    "launch_tweet": "We just went live. One product, zero friction, paid in USDT.",
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
    sid = make_store(slug=slug, status=status)
    key, key_hash = mint_manage_key()
    with SessionLocal() as s:
        st = s.get(Store, sid)
        st.manage_key_hash = key_hash
        st.content = dict(content or CONTENT)
        s.commit()
    return sid, key


def _fake_llm(monkeypatch, kit_dict):
    def _inner(prompt):
        return {"content": [{"text": json.dumps(kit_dict)}]}

    monkeypatch.setattr(engine, "_post_generation", _inner)


def _mock_allow():
    return respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "ALLOW", "risk_level": "none"}
        )
    )


def _generate_kit(slug, key):
    return client.post(f"/api/stores/{slug}/growth-kit", headers=_auth(key))


# ===================================================================== fan
@respx.mock
def test_kit_fans_into_outbox(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="fan1")
    assert _generate_kit("fan1", key).status_code == 200

    r = client.get("/api/stores/fan1/growth/outbox", headers=_auth(key))
    assert r.status_code == 200, r.text
    drafts = r.json()["drafts"]
    # 3 social + launch_tweet + email_subject = 5 pending rows.
    assert len(drafts) == 5
    channels = sorted(d["channel"] for d in drafts)
    assert channels == ["email_subject", "launch_tweet", "social", "social", "social"]
    for d in drafts:
        assert d["status"] == "pending"
        assert d["source"] == "manual"
        assert d["screening_status"] == "allow"
        assert d["content_sha256"] == hashlib.sha256(d["body"].encode()).hexdigest()
    assert r.headers["X-Content-Type-Options"] == "nosniff"


# ===================================================================== IDOR
@respx.mock
def test_outbox_idor_blocked(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    _, key_a = _store_with_key(make_store, slug="idor-a")
    _, key_b = _store_with_key(make_store, slug="idor-b")
    assert _generate_kit("idor-a", key_a).status_code == 200

    with SessionLocal() as s:
        store_a = s.scalar(select(Store).where(Store.slug == "idor-a"))
        did = s.scalar(select(GrowthDraft.id).where(GrowthDraft.store_id == store_a.id))

    # Wrong key -> 401 on the list.
    assert (
        client.get(
            "/api/stores/idor-a/growth/outbox", headers=_auth("nope")
        ).status_code
        == 401
    )
    # Store B's owner cannot read or act on store A's draft (scoped by store_id).
    assert (
        client.get("/api/stores/idor-b/growth/outbox", headers=_auth(key_b)).json()[
            "drafts"
        ]
        == []
    )
    for verb in ("approve", "discard", "mark-published"):
        r = client.post(
            f"/api/stores/idor-b/growth/outbox/{did}/{verb}", headers=_auth(key_b)
        )
        assert r.status_code == 404, verb


# ===================================================================== blocked
def test_draft_blocked_never_readable(make_store):
    sid, key = _store_with_key(make_store, slug="blocked1")
    with SessionLocal() as s:
        s.add(
            GrowthDraft(
                store_id=sid,
                channel="social",
                body="unscreened dangerous copy that must never leave",
                source="manual",
                status="blocked",
                content_sha256=hashlib.sha256(b"x").hexdigest(),
                screening_status="blocked",
            )
        )
        s.commit()

    r = client.get("/api/stores/blocked1/growth/outbox", headers=_auth(key))
    assert r.status_code == 200
    drafts = r.json()["drafts"]
    assert len(drafts) == 1
    assert drafts[0]["status"] == "blocked"
    assert drafts[0]["body"] is None
    # The blocked body appears nowhere in the serialized response.
    assert "dangerous copy" not in r.text


# ===================================================================== immutable
@respx.mock
def test_draft_immutable_after_approval(make_store, monkeypatch):
    _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="immut1")
    _generate_kit("immut1", key)
    with SessionLocal() as s:
        draft = s.scalars(
            select(GrowthDraft).where(GrowthDraft.store_id == sid)
        ).first()
        did, body_before, sha_before = draft.id, draft.body, draft.content_sha256

    r = client.post(
        f"/api/stores/immut1/growth/outbox/{did}/approve", headers=_auth(key)
    )
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    assert r.json()["approved_at"] is not None

    with SessionLocal() as s:
        after = s.get(GrowthDraft, did)
        assert after.body == body_before
        assert after.content_sha256 == sha_before
        assert after.content_sha256 == hashlib.sha256(body_before.encode()).hexdigest()

    # Approving an already-approved draft is rejected (status-only flip, no mutation).
    r2 = client.post(
        f"/api/stores/immut1/growth/outbox/{did}/approve", headers=_auth(key)
    )
    assert r2.status_code == 409


# ===================================================================== sends nothing
def test_mark_published_sends_nothing(make_store, monkeypatch):
    with respx.mock:
        _fake_llm(monkeypatch, GOOD_KIT)
        _mock_allow()
        sid, key = _store_with_key(make_store, slug="pub1")
        _generate_kit("pub1", key)
        with SessionLocal() as s:
            did = (
                s.scalars(select(GrowthDraft).where(GrowthDraft.store_id == sid))
                .first()
                .id
            )
        client.post(f"/api/stores/pub1/growth/outbox/{did}/approve", headers=_auth(key))

    # A fresh respx context with NO routes: any outbound httpx call raises.
    with respx.mock:
        r = client.post(
            f"/api/stores/pub1/growth/outbox/{did}/mark-published",
            headers=_auth(key),
            json={"url": "https://twitter.com/me/status/123"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "published"
        assert r.json()["published_at"] is not None
        assert r.json()["performance_note"] == "https://twitter.com/me/status/123"
        # respx recorded zero outbound calls — nothing was sent anywhere.
        assert len(respx.calls) == 0


# ===================================================================== grep
def test_no_outbound_posting_grep():
    src = pathlib.Path("app/growth.py").read_text(encoding="utf-8")
    assert "smtplib" not in src
    forbidden_hosts = (
        "twitter.com",
        "x.com",
        "api.twitter",
        "facebook.com",
        "graph.facebook",
        "linkedin.com",
        "instagram.com",
        "mastodon",
        "discord.com/api",
        "telegram.org",
        "hooks.slack",
    )
    for host in forbidden_hosts:
        assert host not in src, host
    # This module opens no HTTP client at all: no httpx (so no httpx.post to any
    # host), no requests, no urllib — every outbound-capable seam is absent.
    for sender in ("httpx", "requests", "urllib", "aiohttp", "websocket"):
        assert sender not in src, sender
