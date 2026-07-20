import json

import httpx
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


def test_checkout_404_unknown_store(tmp_path, monkeypatch):
    monkeypatch.setattr(app.main, "STORES", tmp_path)
    r = client.post("/api/checkout/does-not-exist")
    assert r.status_code == 404


# ---------- validation ----------
def test_create_store_422_on_empty_description():
    r = client.post("/create-store", json={"description": ""})
    assert r.status_code == 422


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


def test_checkout_404_on_encoded_slash_traversal(tmp_path, monkeypatch):
    # a %2F-encoded traversal attempt doesn't even match the single-segment
    # {slug} route (Starlette decodes it to a literal "/" first) — it 404s
    # before reaching our handler at all, which is the safe outcome.
    monkeypatch.setattr(app.main, "STORES", tmp_path)
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
    r = client.post("/create-store", json={"description": "something"})
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
    r = client.post("/create-store", json={"description": "something safe"})
    assert r.status_code == 200
    slug = r.json()["slug"]
    slug_dir = tmp_path / slug
    assert (slug_dir / "index.html").exists()
    meta = json.loads((slug_dir / "store.json").read_text())
    assert meta["status"] == "live"


@respx.mock
def test_pending_store_resumes_live_and_checkout_works(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr("app.engine.STORES_DIR", tmp_path)
    monkeypatch.setattr(app.main, "STORES", tmp_path)
    monkeypatch.setattr(app.main, "balance_of", lambda addr: 0.0)
    _mock_llm(monkeypatch, {"store_name": "Resumable Store", "price_usdt": 9})

    screen_state = {"available": False}

    def _screen(request):
        if not screen_state["available"]:
            raise httpx.TimeoutException("down")
        return httpx.Response(200, json={"verdict": "ALLOW"})

    respx.post(WARDEN_SCREEN_URL).mock(side_effect=_screen)

    # 1) screening unavailable -> pending: no index.html, checkout 409'd
    r = client.post("/create-store", json={"description": "something"})
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


@respx.mock
def test_checkout_409_on_pending_screening_store(tmp_path, monkeypatch):
    monkeypatch.setattr(app.main, "STORES", tmp_path)
    d = tmp_path / "pending-store"
    d.mkdir()
    (d / "store.json").write_text(
        json.dumps(
            {
                "slug": "pending-store",
                "status": "pending_screening",
                "pay_to": "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51",
                "amount_usdt": 9,
            }
        )
    )
    r = client.post("/api/checkout/pending-store")
    assert r.status_code == 409
