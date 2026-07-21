"""M17.3 content-calendar + scheduler tests (no network, no funds, NOTHING sends).

Covers the owner-gated calendar plan endpoint (strict validation, latest-row-wins
readback) and the flag-gated background scheduler tick: flag-off = zero LLM spend,
per-store daily cap halts the tick, scheduled drafts are re-screened fail-closed
(BLOCK persists a withheld 'blocked' row; screening outage persists nothing), and
the prompt is built only from store content + first-party numbers (never buyer
data). The LLM seam is monkeypatched and Warden is respx-mocked, so no network is
touched; the scheduler flag is forced on/off per test.
"""

import json

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import config, engine, growth, growth_scheduler
from app.config import WARDEN_SCREEN_URL
from app.db import SessionLocal
from app.delivery import mint_manage_key
from app.models import EventLog, GrowthDraft, Store, log_event

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


def _store_with_key(make_store, slug="cal1", status="live", content=None):
    sid = make_store(slug=slug, status=status)
    key, key_hash = mint_manage_key()
    with SessionLocal() as s:
        st = s.get(Store, sid)
        st.manage_key_hash = key_hash
        st.content = dict(content or CONTENT)
        s.commit()
    return sid, key


def _set_calendar(sid, plan):
    with SessionLocal() as s:
        log_event(s, "growth", "growth.calendar_set", store_id=sid, data=plan)
        s.commit()


def _fake_llm(monkeypatch, kit_dict, usage=None):
    calls = {"n": 0}

    def _inner(prompt):
        calls["n"] += 1
        body = {"content": [{"text": json.dumps(kit_dict)}]}
        if usage is not None:
            body["usage"] = usage
        return body

    monkeypatch.setattr(engine, "_post_generation", _inner)
    return calls


def _mock_allow():
    return respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "ALLOW", "risk_level": "none"}
        )
    )


def _enable(monkeypatch):
    monkeypatch.setattr(config, "GROWTH_SCHED_ENABLED", True)


# ===================================================================== calendar API
def test_calendar_validation(make_store):
    _, key = _store_with_key(make_store, slug="cal-valid")
    # Good plan round-trips and reads back (latest row wins).
    r = client.post(
        "/api/stores/cal-valid/growth/calendar",
        headers=_auth(key),
        json={"cadence": "weekly", "channels": ["social"], "active": True},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"cadence": "weekly", "channels": ["social"], "active": True}
    # Bad cadence, unknown channel, empty channels, duplicate channels, extra key.
    for bad in (
        {"cadence": "hourly", "channels": ["social"]},
        {"cadence": "daily", "channels": ["telegram"]},
        {"cadence": "daily", "channels": []},
        {"cadence": "daily", "channels": ["social", "social"]},
        {"cadence": "daily", "channels": ["social"], "surprise": 1},
    ):
        rb = client.post(
            "/api/stores/cal-valid/growth/calendar", headers=_auth(key), json=bad
        )
        assert rb.status_code == 422, bad
    # Latest row wins on readback.
    client.post(
        "/api/stores/cal-valid/growth/calendar",
        headers=_auth(key),
        json={"cadence": "daily", "channels": ["social", "email_subject"]},
    )
    g = client.get("/api/stores/cal-valid/growth/calendar", headers=_auth(key))
    assert g.json()["cadence"] == "daily"
    assert g.json()["channels"] == ["social", "email_subject"]


def test_calendar_idor_and_auth(make_store):
    _store_with_key(make_store, slug="cal-idor")
    assert (
        client.post(
            "/api/stores/cal-idor/growth/calendar",
            headers=_auth("nope"),
            json={"cadence": "daily", "channels": ["social"]},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/stores/cal-idor/growth/calendar", headers=_auth("nope")
        ).status_code
        == 401
    )


# ===================================================================== scheduler
@respx.mock
def test_scheduler_flag_off_zero_llm(make_store, monkeypatch):
    calls = _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    sid, _ = _store_with_key(make_store, slug="sched-off")
    _set_calendar(sid, {"cadence": "daily", "channels": ["social"], "active": True})
    # Flag OFF (default): the tick is a no-op and never calls the LLM.
    assert config.GROWTH_SCHED_ENABLED is False
    assert growth_scheduler.run_scheduler_tick() == 0
    assert calls["n"] == 0
    with SessionLocal() as s:
        assert s.scalars(select(GrowthDraft)).all() == []


@respx.mock
def test_scheduled_draft_rescreened(make_store, monkeypatch):
    _enable(monkeypatch)
    calls = _fake_llm(
        monkeypatch, GOOD_KIT, usage={"input_tokens": 50, "output_tokens": 40}
    )
    _mock_allow()
    sid, key = _store_with_key(make_store, slug="sched-ok")
    _set_calendar(
        sid,
        {"cadence": "daily", "channels": ["social", "email_subject"], "active": True},
    )
    assert growth_scheduler.run_scheduler_tick() == 1
    assert calls["n"] == 1
    # 3 social + 1 email_subject = 4 scheduled, pending, screened drafts.
    r = client.get("/api/stores/sched-ok/growth/outbox", headers=_auth(key))
    drafts = r.json()["drafts"]
    assert len(drafts) == 4
    assert all(d["source"] == "scheduled" for d in drafts)
    assert all(d["status"] == "pending" for d in drafts)
    assert all(d["screening_status"] == "allow" for d in drafts)
    # A scheduled_run event logged the token spend.
    with SessionLocal() as s:
        run = s.scalar(select(EventLog).where(EventLog.event == "growth.scheduled_run"))
        assert run.data["llm_input_tokens"] == 50
        assert run.data["dispositions"]["allow"] == 4


def test_scheduled_block_persists_withheld(make_store, monkeypatch):
    _enable(monkeypatch)
    _fake_llm(monkeypatch, GOOD_KIT)
    sid, key = _store_with_key(make_store, slug="sched-block")
    _set_calendar(sid, {"cadence": "daily", "channels": ["social"], "active": True})
    with respx.mock:
        respx.post(WARDEN_SCREEN_URL).mock(
            return_value=httpx.Response(
                200, json={"verdict": "BLOCK", "risk_level": "high"}
            )
        )
        growth_scheduler.run_scheduler_tick()
    # BLOCKED scheduled drafts ARE persisted (the 17.1-review binding), body withheld.
    with SessionLocal() as s:
        rows = s.scalars(select(GrowthDraft).where(GrowthDraft.store_id == sid)).all()
        assert len(rows) == 3
        assert all(d.status == "blocked" for d in rows)
        assert all(d.screening_status == "blocked" for d in rows)
        # The body is stored in the row but must NEVER leave through a read path.
        assert all(d.body for d in rows)
    r = client.get("/api/stores/sched-block/growth/outbox", headers=_auth(key))
    for d in r.json()["drafts"]:
        assert d["status"] == "blocked"
        assert d["body"] is None
    assert "Launch day" not in r.text


def test_scheduled_screening_outage_persists_nothing(make_store, monkeypatch):
    _enable(monkeypatch)
    _fake_llm(monkeypatch, GOOD_KIT)
    sid, _ = _store_with_key(make_store, slug="sched-down")
    _set_calendar(sid, {"cadence": "daily", "channels": ["social"], "active": True})
    with respx.mock:
        respx.post(WARDEN_SCREEN_URL).mock(return_value=httpx.Response(500))
        growth_scheduler.run_scheduler_tick()
    # Screening unavailable => NO draft rows (skip, retry next tick — never serve).
    with SessionLocal() as s:
        assert (
            s.scalars(select(GrowthDraft).where(GrowthDraft.store_id == sid)).all()
            == []
        )


@respx.mock
def test_daily_cap_halts_tick(make_store, monkeypatch):
    _enable(monkeypatch)
    calls = _fake_llm(monkeypatch, GOOD_KIT)
    _mock_allow()
    sid, _ = _store_with_key(make_store, slug="sched-cap")
    _set_calendar(sid, {"cadence": "daily", "channels": ["social"], "active": True})
    # Seed the per-store daily cap worth of scheduled runs already today.
    with SessionLocal() as s:
        for _ in range(config.GROWTH_SCHED_STORE_DAILY_MAX):
            log_event(s, "growth", "growth.scheduled_run", store_id=sid, data={})
        s.commit()
    assert growth_scheduler.run_scheduler_tick() == 0
    assert calls["n"] == 0
    with SessionLocal() as s:
        assert (
            s.scalars(select(GrowthDraft).where(GrowthDraft.store_id == sid)).all()
            == []
        )


def test_failed_generation_counts_toward_daily_cap(make_store, monkeypatch):
    """A generation that raises GenerationUnavailable still logs a scheduled_run
    so it counts toward the caps — a persistently-failing LLM can't be retried
    every tick past the cap (the '6/day' bounds attempts, not just successes)."""
    from app.engine import GenerationUnavailable

    _enable(monkeypatch)

    def _boom(prompt):
        raise GenerationUnavailable("llm down")

    monkeypatch.setattr(growth, "_generate_kit", _boom)
    sid, _ = _store_with_key(make_store, slug="sched-fail")
    _set_calendar(sid, {"cadence": "daily", "channels": ["social"], "active": True})
    growth_scheduler.run_scheduler_tick()
    with SessionLocal() as s:
        runs = s.scalars(
            select(EventLog).where(EventLog.event == "growth.scheduled_run")
        ).all()
    assert len(runs) == 1  # the failed attempt was counted
    with SessionLocal() as s:
        assert (
            s.scalars(select(GrowthDraft).where(GrowthDraft.store_id == sid)).all()
            == []
        )  # but nothing was served


@respx.mock
def test_prompt_excludes_buyer_data(make_store, monkeypatch):
    _enable(monkeypatch)
    _mock_allow()
    captured = {}

    def _spy(prompt):
        captured["prompt"] = prompt
        return {"content": [{"text": json.dumps(GOOD_KIT)}]}

    monkeypatch.setattr(engine, "_post_generation", _spy)
    sid, _ = _store_with_key(make_store, slug="sched-noleak")
    # A buyer email that must NEVER enter the prompt (first-party numbers only).
    from app.models import EmailSubscriber

    with SessionLocal() as s:
        s.add(
            EmailSubscriber(store_id=sid, email="buyer-secret@x.com", source="waitlist")
        )
        s.commit()
    _set_calendar(sid, {"cadence": "daily", "channels": ["social"], "active": True})
    growth_scheduler.run_scheduler_tick()
    prompt = captured["prompt"]
    assert "buyer-secret@x.com" not in prompt
    # The prompt carries store content + a first-party performance line, nothing else.
    assert "Flowstate" in prompt
    assert "Recent performance" in prompt


def test_scheduler_inactive_calendar_skipped(make_store, monkeypatch):
    _enable(monkeypatch)
    calls = _fake_llm(monkeypatch, GOOD_KIT)
    sid, _ = _store_with_key(make_store, slug="sched-inactive")
    _set_calendar(sid, {"cadence": "daily", "channels": ["social"], "active": False})
    assert growth_scheduler.run_scheduler_tick() == 0
    assert calls["n"] == 0
