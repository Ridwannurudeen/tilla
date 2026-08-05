import json
import os
import pathlib

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import app.main
from app.config import MAX_BODY_BYTES, WARDEN_SCREEN_URL

client = TestClient(app.main.app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "service": "tilla", "chain": "X Layer (196)"}


def test_create_store_503_without_llm_key():
    r = client.post("/create-store", json={"description": "I sell handmade socks"})
    assert r.status_code == 503


def test_checkout_404_unknown_store():
    r = client.post("/api/checkout/does-not-exist")
    assert r.status_code == 404


# ---------- validation ----------
# ABSENT input is defaulted to the sample store; PRESENT-but-invalid input still
# refuses. OKX's review automation pays and then replays an empty body — a 422
# there became "endpoint requires parameters" in the agent task flow, no human
# answered, and the review timed out. These pin the split.
def test_create_store_empty_description_proceeds_to_sample_store():
    # Without TILLA_LLM_KEY the handler 503s AFTER validation — so a 503 here
    # proves the empty body was accepted, same as the valid-body test above.
    r = client.post("/create-store", json={"description": ""})
    assert r.status_code == 503


def test_create_store_empty_json_body_proceeds():
    r = client.post("/create-store", json={})
    assert r.status_code == 503


def test_create_store_no_body_at_all_proceeds():
    r = client.post("/create-store")
    assert r.status_code == 503


def test_create_store_422_on_oversized_description():
    r = client.post("/create-store", json={"description": "a" * 3000})
    assert r.status_code == 422


def test_create_store_422_on_bad_receive_address():
    r = client.post(
        "/create-store",
        json={"description": "socks", "receive_address": "0x1234"},
    )
    assert r.status_code == 422


def test_create_store_422_on_zero_receive_address():
    r = client.post(
        "/create-store",
        json={"description": "socks", "receive_address": "0x" + "0" * 40},
    )
    assert r.status_code == 422


def test_checkout_422_on_dotted_slug():
    # a single path segment of ".." never matches SLUG_PATTERN, so it can't
    # be used to build an out-of-bounds filesystem path. %2e%2e avoids the
    # HTTP client's own client-side dot-segment normalization of a literal "..".
    r = client.post("/api/checkout/%2e%2e")
    assert r.status_code == 422


def test_checkout_404_on_encoded_slash_traversal():
    # a %2F-encoded traversal attempt doesn't even match the single-segment
    # {slug} route (Starlette decodes it to a literal "/" first) — it 404s
    # before reaching our handler at all, which is the safe outcome.
    r = client.post("/api/checkout/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 404


# ---------- body size cap ----------
def test_body_too_large_rejected_by_content_length():
    big = "a" * (MAX_BODY_BYTES + 5000)
    r = client.post("/create-store", json={"description": big})
    assert r.status_code == 413


def test_body_at_cap_is_not_rejected_by_size():
    # comfortably under the cap; should reach validation (422 for length), not 413.
    payload = json.dumps({"description": "safe " * 50})
    r = client.post(
        "/create-store",
        content=payload,
        headers={"content-type": "application/json"},
    )
    assert r.status_code != 413


# ---------- M4 upload: middleware exemption is scoped to multipart only ----------
def test_deliverable_json_body_still_capped_at_64kb():
    # The upload-path exemption is ONLY for multipart; a JSON body to the same
    # path is still buffered and capped at 64KB (regression for the body-cap hole).
    big = json.dumps({"kind": "text", "payload": "a" * (MAX_BODY_BYTES + 5000)})
    r = client.post(
        "/api/stores/whatever/deliverable",
        content=big,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413


def test_deliverable_upload_requires_manage_key(make_store):
    # A store with no manage key (NULL hash) rejects any bearer -> 401, never 500.
    make_store(slug="upauth", pay_to="0x" + "a" * 40)
    r = client.post(
        "/api/stores/upauth/deliverable",
        headers={"Authorization": "Bearer anything"},
        files={"file": ("g.pdf", b"data", "application/pdf")},
    )
    assert r.status_code == 401


def test_malformed_content_length_returns_400():
    # the test client recomputes Content-Length, so exercise the guard directly:
    # a non-numeric header must yield 400, not a 500 from int() blowing up.
    import asyncio

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/create-store",
        "headers": [(b"content-length", b"not-a-number")],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def call_next(request):
        raise AssertionError("call_next should not run for a malformed header")

    resp = asyncio.run(app.main.limit_body_size(Request(scope, receive), call_next))
    assert resp.status_code == 400


def test_test_mark_route_absent_without_env():
    # the /api/_test/mark backdoor is only registered when TILLA_TEST=1; without
    # it the path does not exist at all (404), not merely a gated 403.
    r = client.post("/api/_test/mark/whatever")
    assert r.status_code == 404


# ---------- rate limiting ----------
def test_create_store_rate_limited():
    codes = [
        client.post("/create-store", json={"description": "socks"}).status_code
        for _ in range(10)
    ]
    assert 429 in codes


def test_checkout_status_rate_limited():
    codes = [
        client.get("/api/checkout/doesnotexist123456").status_code for _ in range(45)
    ]
    assert 429 in codes


# ---------- screening integration ----------
def _mock_llm(monkeypatch, content):
    def fake_generate(desc):
        from app.engine import GeneratedContent

        return GeneratedContent.model_validate(content).model_dump()

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr("app.engine.generate", fake_generate)


@respx.mock
def test_create_store_blocked_returns_422(tmp_path, monkeypatch):
    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    _mock_llm(monkeypatch, {"store_name": "Bad Store", "price_usdt": 9})
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    r = client.post("/create-store", json={"description": "something unsafe"})
    assert r.status_code == 422
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_create_store_pending_when_screening_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    _mock_llm(monkeypatch, {"store_name": "Pending Store", "price_usdt": 9})
    respx.post(WARDEN_SCREEN_URL).mock(side_effect=httpx.TimeoutException("timeout"))
    r = client.post(
        "/create-store",
        json={"description": "something", "receive_address": "0x" + "a" * 40},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending_screening"
    slug_dir = tmp_path / body["slug"]
    assert (slug_dir / "store.json").exists()
    assert not (slug_dir / "index.html").exists()


@respx.mock
def test_create_store_live_when_screening_allows(tmp_path, monkeypatch):
    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    _mock_llm(
        monkeypatch,
        {
            "store_name": "Good Store",
            "price_usdt": 9,
            "palette": {
                "primary": "#111111",
                "accent": "#222222",
                "bg": "#333333",
                "text": "#444444",
            },
        },
    )
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    r = client.post(
        "/create-store",
        json={
            "description": "something safe",
            "receive_address": "0x" + "a" * 40,
        },
    )
    assert r.status_code == 200
    slug = r.json()["slug"]
    slug_dir = tmp_path / slug
    assert (slug_dir / "index.html").exists()
    meta = json.loads((slug_dir / "store.json").read_text(encoding="utf-8"))
    assert meta["status"] == "live"


@respx.mock
def test_create_store_empty_body_delivers_the_sample_store(tmp_path, monkeypatch):
    # The full unattended-reviewer path: paid POST with NO parameters ends in a
    # non-payable sample built from DEFAULT_STORE_DESCRIPTION, and the response
    # says a default was used so a machine caller can tell its input never arrived.
    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    seen = {}

    def fake_generate(desc):
        from app.engine import GeneratedContent

        seen["description"] = desc
        return GeneratedContent.model_validate(
            {"store_name": "Sample Store", "price_usdt": 9}
        ).model_dump()

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr("app.engine.generate", fake_generate)
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    r = client.post("/create-store", json={})
    assert r.status_code == 200
    body = r.json()
    assert seen["description"] == app.main.DEFAULT_STORE_DESCRIPTION
    assert "sample store" in body["note"]
    assert (tmp_path / body["slug"] / "index.html").exists()


@respx.mock
def test_create_store_with_description_carries_no_note(tmp_path, monkeypatch):
    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    _mock_llm(monkeypatch, {"store_name": "Real Store", "price_usdt": 9})
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    r = client.post(
        "/create-store",
        json={
            "description": "I sell honest socks",
            "receive_address": "0x" + "a" * 40,
        },
    )
    assert r.status_code == 200
    assert "note" not in r.json()


@respx.mock
def test_pending_store_resumes_live_and_checkout_works(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    _mock_llm(monkeypatch, {"store_name": "Resumable Store", "price_usdt": 9})

    screen_state = {"available": False}

    def _screen(request):
        if not screen_state["available"]:
            raise httpx.TimeoutException("down")
        return httpx.Response(200, json={"verdict": "ALLOW"})

    respx.post(WARDEN_SCREEN_URL).mock(side_effect=_screen)

    # 1) screening unavailable -> pending: no index.html, checkout 409'd
    r = client.post(
        "/create-store",
        json={"description": "something", "receive_address": "0x" + "a" * 40},
    )
    assert r.status_code == 200
    slug = r.json()["slug"]
    slug_dir = tmp_path / slug
    assert not (slug_dir / "index.html").exists()
    assert client.post(f"/api/checkout/{slug}").status_code == 409

    # 2) screening recovers -> resume_pending flips it live, checkout works
    screen_state["available"] = True
    engine.resume_pending()
    assert (slug_dir / "index.html").exists()
    meta = json.loads((slug_dir / "store.json").read_text(encoding="utf-8"))
    assert meta["status"] == "live"
    assert client.post(f"/api/checkout/{slug}").status_code == 200


def _store_theme(slug):
    from app.db import SessionLocal
    from app.models import Store
    from sqlalchemy import select

    with SessionLocal() as s:
        return s.scalar(select(Store.theme).where(Store.slug == slug))


@respx.mock
def test_create_store_explicit_theme_persists_and_renders(tmp_path, monkeypatch):
    # Caller picks the theme: it wins over the LLM's suggestion, is persisted, and
    # the store renders on it (bold's `.stamp` mark). og.svg is written too.
    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    _mock_llm(
        monkeypatch, {"store_name": "Chosen", "price_usdt": 9, "theme": "original"}
    )
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    r = client.post(
        "/create-store", json={"description": "loud brand", "theme": "bold"}
    )
    assert r.status_code == 200
    slug = r.json()["slug"]
    assert _store_theme(slug) == "bold.html"
    slug_dir = tmp_path / slug
    html = (slug_dir / "index.html").read_text(encoding="utf-8")
    assert 'class="stamp mono"' in html
    assert (slug_dir / "og.svg").exists()
    meta = json.loads((slug_dir / "store.json").read_text(encoding="utf-8"))
    assert meta["theme"] == "bold.html"


@respx.mock
def test_create_store_uses_llm_theme_when_unspecified(tmp_path, monkeypatch):
    # No caller theme -> the LLM's suggestion (editorial) is used and persisted.
    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    _mock_llm(
        monkeypatch, {"store_name": "Elegant", "price_usdt": 9, "theme": "editorial"}
    )
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    r = client.post("/create-store", json={"description": "a refined journal"})
    assert r.status_code == 200
    slug = r.json()["slug"]
    assert _store_theme(slug) == "editorial.html"
    html = (tmp_path / slug / "index.html").read_text(encoding="utf-8")
    assert 'class="folio mono"' in html


def test_create_store_invalid_theme_422():
    # Body validation rejects an unknown theme before the endpoint body runs, so
    # no LLM key / mock is needed.
    r = client.post("/create-store", json={"description": "socks", "theme": "neon"})
    assert r.status_code == 422


# ---------- idempotent create + pre-settle failure envelope ----------
# 0xqdee (2026-08-05) paid for a store, the store was created, the payment settled —
# and a deploy-window 502 lost the response. Holding no slug and no manage_key, their
# only move was a retry, and a retry carrying a NEW signed payment buys a SECOND store
# and pays a SECOND time. These pin the two halves of the fix: an Idempotency-Key that
# already funded a store is answered 409 (>= 400, so the x402 middleware skips
# settlement and the duplicate payment never settles), and every error this route
# returns says so in words with a correlation id to quote.
_MERCHANT = "0x" + "a" * 40
_PAYER = "0x" + "1" * 40


def _store_rows():
    from app.db import SessionLocal
    from app.models import Store
    from sqlalchemy import select

    with SessionLocal() as s:
        return list(s.scalars(select(Store.slug).order_by(Store.id)))


def _idempotency_columns():
    from app.db import SessionLocal
    from app.models import Store
    from sqlalchemy import select

    with SessionLocal() as s:
        return list(
            s.execute(
                select(
                    Store.create_idempotency_addr, Store.create_idempotency_key
                ).order_by(Store.id)
            ).all()
        )


def _paid_by(monkeypatch, payer):
    """Stand in for the x402 payer the middleware would have verified. The suite runs
    with OKX_API_KEY unset, so no middleware runs and request.state.payment_payload
    never exists; the extraction itself is pinned against a real PaymentPayload by
    test_idempotency_scope_reads_the_signed_payer below. Returns the dict so a test
    can switch payers mid-flight."""
    seen = {"addr": payer}
    monkeypatch.setattr("app.main._idempotency_scope", lambda request: seen["addr"])
    return seen


def _allow_create(tmp_path, monkeypatch, name="Idem Store"):
    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    _mock_llm(
        monkeypatch,
        {"store_name": name, "product_name": "Honest Socks", "price_usdt": 9},
    )
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )


def test_idempotency_scope_reads_the_signed_payer():
    """The scope is the EIP-3009 ``from`` of the payment the middleware verified,
    lowercased — the same field the buy path reads off request.state.payment_payload.
    Built from the real PaymentPayload so the stub the HTTP tests use cannot drift
    from the shape production actually receives."""
    from starlette.requests import Request
    from x402.schemas import PaymentPayload, PaymentRequirements

    from app.payment import PAYMENT_ASSET

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/create-store",
        "headers": [],
        "query_string": b"",
        "client": ("test", 1),
        "server": ("test", 80),
        "scheme": "http",
    }
    req = Request(scope)
    assert app.main._idempotency_scope(req) is None  # no paywall -> no payer
    req.state.payment_payload = PaymentPayload(
        x402_version=2,
        payload={
            "authorization": {"from": "0xAB" + "1" * 38, "nonce": "0x" + "2" * 64}
        },
        accepted=PaymentRequirements(
            scheme="exact",
            network="eip155:196",
            asset=PAYMENT_ASSET,
            amount="50000",
            pay_to="0x" + "b" * 40,
            max_timeout_seconds=300,
            extra={},
        ),
    )
    assert app.main._idempotency_scope(req) == "0xab" + "1" * 38


@respx.mock
def test_same_idempotency_key_returns_409_with_the_first_store(tmp_path, monkeypatch):
    _allow_create(tmp_path, monkeypatch)
    _paid_by(monkeypatch, _PAYER)
    body = {"description": "I sell honest socks", "receive_address": _MERCHANT}
    head = {"Idempotency-Key": "retry-1"}

    first = client.post("/create-store", json=body, headers=head)
    assert first.status_code == 200
    slug = first.json()["slug"]

    second = client.post("/create-store", json=body, headers=head)
    # 409, not 200: >= 400 is what makes the middleware skip settlement, so the fresh
    # payment on this retry never settles. Turning it into a 200 double-charges.
    assert second.status_code == 409
    replay = second.json()
    assert replay["slug"] == slug
    assert replay["url"].endswith(f"/s/{slug}/")
    assert replay["product_name"]
    assert replay["price_usdt"] == 9.0
    assert replay["status"] == "live"
    # THE money assertion: one payment, one store row. No second store, ever.
    assert _store_rows() == [slug]
    # ...and the key rode in on the store's OWN insert, not a patch afterwards: a
    # commit that lands without its key is what makes the retry buy a second store.
    assert _idempotency_columns() == [(_PAYER, "retry-1")]


@respx.mock
def test_idempotent_replay_409_says_not_charged_and_how_to_recover(
    tmp_path, monkeypatch
):
    _allow_create(tmp_path, monkeypatch)
    _paid_by(monkeypatch, _PAYER)
    body = {"description": "I sell honest socks", "receive_address": _MERCHANT}
    head = {"Idempotency-Key": "abc.123:xyz-9"}
    assert client.post("/create-store", json=body, headers=head).status_code == 200

    replay = client.post("/create-store", json=body, headers=head).json()
    assert "NOT charged" in replay["not_charged"]
    assert "already created a store" in replay["detail"]
    assert len(replay["ref"]) == 12
    # The manage_key CANNOT come back: only its sha256 is stored, and minting a fresh
    # one would hand store control to whoever replayed a key. The 409 says so and
    # names the wallet path instead of implying full recovery.
    assert "manage_key" not in replay
    assert "cannot be re-issued" in replay["recovery"]
    assert "receive_address" in replay["recovery"]
    assert "dashboard" in replay["recovery"]


@respx.mock
def test_no_idempotency_key_still_creates_two_stores(tmp_path, monkeypatch):
    # Proves the fix did not silently make every create idempotent: with no key, two
    # identical paid calls are two paid stores, exactly as before.
    _allow_create(tmp_path, monkeypatch)
    _paid_by(monkeypatch, _PAYER)
    body = {"description": "I sell honest socks", "receive_address": _MERCHANT}
    first = client.post("/create-store", json=body)
    second = client.post("/create-store", json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["slug"] != second.json()["slug"]
    assert len(_store_rows()) == 2


@respx.mock
def test_two_payers_may_use_the_same_idempotency_key(tmp_path, monkeypatch):
    # The key is scoped to the PAYER (uq_stores_create_idem), so one caller's "retry-1"
    # can never refuse another's paid create — nor leak them that store's slug, url,
    # product and price in a 409 they did not earn.
    _allow_create(tmp_path, monkeypatch)
    payer = _paid_by(monkeypatch, _PAYER)
    head = {"Idempotency-Key": "retry-1"}
    first = client.post(
        "/create-store",
        json={"description": "socks", "receive_address": _MERCHANT},
        headers=head,
    )
    assert first.status_code == 200

    payer["addr"] = "0x" + "2" * 40
    second = client.post(
        "/create-store",
        json={"description": "socks", "receive_address": "0x" + "c" * 40},
        headers=head,
    )
    assert second.status_code == 200
    assert second.json()["slug"] != first.json()["slug"]
    assert len(_store_rows()) == 2


@respx.mock
def test_malformed_idempotency_key_is_422_before_anything_is_created(
    tmp_path, monkeypatch
):
    # Charset and length are refused BEFORE settle (>= 400 -> no settlement), so the
    # caller pays nothing for a key we could neither store nor safely echo.
    _allow_create(tmp_path, monkeypatch)
    _paid_by(monkeypatch, _PAYER)
    body = {"description": "socks", "receive_address": _MERCHANT}
    for bad in ("has space", "semi;colon", "a" * 201):
        r = client.post("/create-store", json=body, headers={"Idempotency-Key": bad})
        assert r.status_code == 422, bad
        assert "Idempotency-Key must be" in r.json()["detail"]
        assert "NOT charged" in r.json()["not_charged"]
    assert _store_rows() == []


@respx.mock
def test_blank_idempotency_key_is_treated_as_absent(tmp_path, monkeypatch):
    # An empty or whitespace-only header is NOT a key: storing "" would give every
    # later caller who sends one a stranger's 409. It means today's behaviour instead.
    _allow_create(tmp_path, monkeypatch)
    _paid_by(monkeypatch, _PAYER)
    body = {"description": "socks", "receive_address": _MERCHANT}
    first = client.post("/create-store", json=body, headers={"Idempotency-Key": "   "})
    second = client.post("/create-store", json=body, headers={"Idempotency-Key": ""})
    assert first.status_code == 200 and second.status_code == 200
    assert len(_store_rows()) == 2


@respx.mock
def test_idempotency_key_without_a_verified_payer_is_ignored(tmp_path, monkeypatch):
    # No paywall installed (OKX_API_KEY unset) means no payer to scope the key to —
    # and no payment to duplicate. The key is dropped rather than stored half-scoped
    # under a NULL payer, where it would match no lookup and mean nothing. Production
    # cannot reach this: the middleware 402s an unpaid create-store before the handler.
    _allow_create(tmp_path, monkeypatch)
    body = {"description": "socks", "receive_address": _MERCHANT}
    head = {"Idempotency-Key": "retry-1"}
    first = client.post("/create-store", json=body, headers=head)
    second = client.post("/create-store", json=body, headers=head)
    assert first.status_code == 200 and second.status_code == 200
    assert len(_store_rows()) == 2
    assert _idempotency_columns() == [(None, None), (None, None)]


def test_create_store_503_without_llm_key_says_not_charged_with_a_ref():
    r = client.post("/create-store", json={"description": "socks"})
    assert r.status_code == 503
    body = r.json()
    assert body["detail"] == "generation unavailable"  # existing text, untouched
    assert "NOT charged" in body["not_charged"]
    assert len(body["ref"]) == 12


@respx.mock
def test_create_store_503_on_generation_outage_keeps_retry_after(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")

    def _down(desc):
        raise engine.GenerationUnavailable("anthropic unreachable")

    monkeypatch.setattr("app.engine.generate", _down)
    r = client.post("/create-store", json={"description": "socks"})
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "60"
    body = r.json()
    assert body["detail"] == "store generation temporarily unavailable — retry shortly"
    assert "NOT charged" in body["not_charged"]
    assert len(body["ref"]) == 12


def test_create_store_422_on_bad_brand_color_says_not_charged_with_a_ref():
    # The pydantic 422 — no raise site in the handler to edit, which is why the
    # envelope is applied by an exception handler scoped to this route.
    r = client.post("/create-store", json={"brand_color": "not-a-colour"})
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], list)  # FastAPI's error list, unchanged in shape
    assert "NOT charged" in body["not_charged"]
    assert len(body["ref"]) == 12


@respx.mock
def test_create_store_422_on_screening_block_says_not_charged(tmp_path, monkeypatch):
    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    _mock_llm(monkeypatch, {"store_name": "Bad Store", "price_usdt": 9})
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    r = client.post("/create-store", json={"description": "something unsafe"})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "content did not pass safety screening"
    assert "NOT charged" in body["not_charged"]
    assert len(body["ref"]) == 12


def test_free_routes_keep_the_plain_error_body():
    # The envelope is scoped to the paid create route: on a free route "you were not
    # charged" is noise, and every other route's body stays byte-identical.
    r = client.post("/api/checkout/does-not-exist")
    assert r.status_code == 404
    assert r.json() == {"detail": "store not found"}


@respx.mock
def test_correlation_ref_is_logged_with_the_failure(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    _mock_llm(monkeypatch, {"store_name": "Bad Store", "price_usdt": 9})
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(
            200, json={"verdict": "BLOCK", "risk_level": "high"}
        )
    )
    with caplog.at_level(logging.WARNING, logger="tilla"):
        r = client.post("/create-store", json={"description": "something unsafe"})
    ref = r.json()["ref"]
    # The id a buyer quotes must find BOTH lines: the cause and the refusal.
    assert f"ref={ref} risk_level=high" in caplog.text
    assert f"ref={ref} status=422" in caplog.text


@respx.mock
def test_idempotency_race_loser_never_deletes_the_winners_paid_store(
    tmp_path, monkeypatch
):
    """The race the pre-check cannot see: a live create runs ~15s, agent clients give
    up around 10s and retry, so both attempts are in the pipeline at once and the
    loser trips the unique index at INSERT. The old handler read every IntegrityError
    as a slug collision and rmtree'd the directory — which, in the window where the
    loser had resolved the SAME slug, is the winner's committed, PAID store: buyer
    charged, store URL 404. unique_slug is pinned to the winner's slug to put the
    loser exactly in that window."""
    import app.engine as engine

    _allow_create(tmp_path, monkeypatch, name="Race Store")
    winner = engine.create_store(
        "socks", _MERCHANT, idempotency_addr=_PAYER, idempotency_key="race-1"
    )
    slug = winner["slug"]
    assert (tmp_path / slug / "index.html").exists()

    monkeypatch.setattr("app.engine.unique_slug", lambda base, content=None: slug)
    with pytest.raises(engine.IdempotentReplay) as caught:
        engine.create_store(
            "socks", _MERCHANT, idempotency_addr=_PAYER, idempotency_key="race-1"
        )
    assert caught.value.slug == slug
    assert (tmp_path / slug / "index.html").exists()  # the paid store survives
    assert _store_rows() == [slug]


@respx.mock
def test_idempotency_race_loser_leaves_no_orphan_directory(tmp_path, monkeypatch):
    # The other half of the same branch: when the loser resolved its OWN slug, those
    # files back no store row and would serve a rendered storefront nobody owns.
    import app.engine as engine

    _allow_create(tmp_path, monkeypatch, name="Race Store")
    winner = engine.create_store(
        "socks", _MERCHANT, idempotency_addr=_PAYER, idempotency_key="race-2"
    )
    with pytest.raises(engine.IdempotentReplay):
        engine.create_store(
            "socks", _MERCHANT, idempotency_addr=_PAYER, idempotency_key="race-2"
        )
    assert [p.name for p in tmp_path.iterdir() if p.is_dir()] == [winner["slug"]]
    assert _store_rows() == [winner["slug"]]


# ---------- 0034: the idempotency columns + their unique index ----------
def test_migration_0034_additive_and_scoped_unique(tmp_path):
    import sqlite3
    import subprocess
    import sys

    def _alembic(db, *args):
        env = dict(os.environ)
        env["TILLA_DB_PATH"] = str(db)
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=pathlib.Path(__file__).resolve().parent.parent,
            env=env,
            capture_output=True,
            text=True,
        )

    db = tmp_path / "m34.db"
    r = _alembic(db, "upgrade", "0033_brand_color")
    assert r.returncode == 0, r.stderr

    # two stores against the pre-0034 schema, so the columns are proven additive
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO merchants (id, wallet_address, created_at) VALUES (1,'0xabc','2026')"
    )
    for i in (1, 2):
        con.execute(
            "INSERT INTO stores (id, slug, merchant_id, status, pay_to, theme,"
            " created_at, updated_at) VALUES (?,?,1,'live','0xabc','original.html',"
            "'2026','2026')",
            (i, f"m34-{i}"),
        )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    cols = {c[1] for c in con.execute("PRAGMA table_info(stores)")}
    assert {"create_idempotency_addr", "create_idempotency_key"} <= cols
    # existing rows backfill to NULL/NULL, which means "no key" — today's behaviour
    assert (
        con.execute(
            "SELECT count(*) FROM stores WHERE create_idempotency_key IS NULL"
        ).fetchone()[0]
        == 2
    )
    idx = {
        i[0]
        for i in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='stores'"
        )
    }
    assert "uq_stores_create_idem" in idx
    assert "uq_stores_custom_domain" in idx  # native ADD COLUMN, no batch rebuild
    con.execute(
        "UPDATE stores SET create_idempotency_addr='0xp', create_idempotency_key='k'"
        " WHERE id=1"
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "UPDATE stores SET create_idempotency_addr='0xp',"
            " create_idempotency_key='k' WHERE id=2"
        )
    # ...but the SAME key under a different payer is not a duplicate
    con.execute(
        "UPDATE stores SET create_idempotency_addr='0xq', create_idempotency_key='k'"
        " WHERE id=2"
    )
    con.commit()
    con.close()

    r = _alembic(db, "downgrade", "0033_brand_color")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    cols = {c[1] for c in con.execute("PRAGMA table_info(stores)")}
    assert not ({"create_idempotency_addr", "create_idempotency_key"} & cols)
    idx = {
        i[0]
        for i in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='stores'"
        )
    }
    assert "uq_stores_create_idem" not in idx
    assert "uq_stores_custom_domain" in idx  # untouched by the downgrade too
    assert con.execute("SELECT count(*) FROM stores").fetchone()[0] == 2
    con.close()


def test_sitemap_lists_live_stores_excludes_pending_and_blocked(make_store):
    make_store(slug="live-a", pay_to="0x" + "a" * 40)
    make_store(slug="live-b", pay_to="0x" + "b" * 40)
    make_store(slug="pending-x", pay_to="0x" + "c" * 40, status="pending_screening")
    make_store(slug="blocked-y", pay_to="0x" + "d" * 40, status="blocked")
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    body = r.text
    assert "https://tilla.gudman.xyz/s/live-a/" in body
    assert "https://tilla.gudman.xyz/s/live-b/" in body
    assert "pending-x" not in body
    assert "blocked-y" not in body
    # Hub pages precede the store URLs.
    assert "<loc>https://tilla.gudman.xyz/</loc>" in body
    assert "https://tilla.gudman.xyz/marketplace.html" in body
    assert "https://tilla.gudman.xyz/receipt-demo.html" in body
    assert "https://tilla.gudman.xyz/library.html" in body


def test_checkout_409_on_pending_screening_store(make_store):
    make_store(slug="pending-store", status="pending_screening")
    r = client.post("/api/checkout/pending-store")
    assert r.status_code == 409


def test_checkout_returns_pay_to_unique_amount_and_expiry(make_store):
    # pay_to is present in BOTH the POST and GET responses; the amount is the
    # exact unique expected amount (price + offset), identical in both; and the
    # POST response carries the expiry the buyer's page needs.
    make_store(slug="contract-store", pay_to="0x" + "b" * 40, price_micro=9_000_000)
    post = client.post("/api/checkout/contract-store")
    assert post.status_code == 200
    pb = post.json()
    assert pb["pay_to"] == "0x" + "b" * 40
    assert "expires_at" in pb
    assert isinstance(pb["amount"], float)
    # price 9.0 + offset in [0.000001, 0.004999]
    assert 9.0 < pb["amount"] < 9.005
    get = client.get(f"/api/checkout/{pb['id']}")
    assert get.status_code == 200
    gb = get.json()
    assert gb["pay_to"] == "0x" + "b" * 40
    assert gb["amount"] == pb["amount"]
    assert gb["status"] == "pending"


def test_checkout_post_returns_amount_micro_and_z_expiry(make_store):
    # M5 additive keys the wallet JS needs: amount_micro (exact int base units, so
    # calldata is built with BigInt only, never float) and a 'Z'-suffixed expiry so
    # JS Date parses it as UTC, not local time.
    from app.db import SessionLocal
    from app.models import Order

    make_store(slug="micro-store", pay_to="0x" + "c" * 40, price_micro=9_000_000)
    post = client.post("/api/checkout/micro-store").json()
    assert isinstance(post["amount_micro"], int)
    assert post["amount_micro"] == round(post["amount"] * 1e6)
    assert 9_000_001 <= post["amount_micro"] <= 9_004_999
    assert post["expires_at"].endswith("Z")
    with SessionLocal() as s:
        assert post["amount_micro"] == s.get(Order, post["id"]).expected_micro


def test_erc20_transfer_calldata_parity_vector():
    # The exact ERC-20 transfer() calldata the browser's buildTransferData(payTo,
    # micro) MUST reproduce byte-for-byte: selector + pad32(to) + pad32(amount).
    # Pinned as a documented vector and cross-checked against chain.pad_address, so
    # a drift on either side is caught here; the JS is verified against it in the
    # browser smoke (docs/SMOKE-M5.md).
    from app import chain

    pay_to = "0x779ded0c9e1022225f8e0630b35a9b54be713736"
    micro = 9_004_999
    expected = (
        "0xa9059cbb"
        "000000000000000000000000779ded0c9e1022225f8e0630b35a9b54be713736"
        "00000000000000000000000000000000000000000000000000000000008967c7"
    )
    built = "0xa9059cbb" + chain.pad_address(pay_to)[2:] + format(micro, "064x")
    assert built == expected
    assert len(built) == 2 + 8 + 64 + 64  # 0x + selector + to word + amount word
    assert format(micro, "x") == "8967c7"


def test_www_fee_copy_tracks_payment_amount():
    """www/index.html hand-types the create-store fee (static page, no templating).
    Pin those strings to PAYMENT_AMOUNT so a fee change can never ship without the
    public copy — the exact 1-USDT drift that reached production once. The old
    price string must never reappear in any form ("0.01 USDT0" would substring-
    match "1 USDT", which is why service-fee copy stays out of this page)."""
    import pathlib as _pathlib

    from app.dashboard import fee_usdt
    from app.payment import PAYMENT_AMOUNT

    html = (
        _pathlib.Path(__file__).resolve().parent.parent / "www" / "index.html"
    ).read_text(encoding="utf-8")
    fee = fee_usdt(int(PAYMENT_AMOUNT))
    assert html.count(f"{fee} USDT0") >= 4
    assert "1 USDT" not in html
