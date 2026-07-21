import json
import re

import httpx
import respx

from app import config
from app.config import WARDEN_SCREEN_URL
from app.engine import _screening_text, generate, slugify, unique_slug


def test_slugify_unicode():
    assert slugify("Café Münchën") == "cafe-munchen"


def test_slugify_symbols():
    assert slugify("Hello, World! @#$ 2024") == "hello-world-2024"


def test_slugify_length_cap():
    result = slugify("a" * 100)
    assert len(result) == 40
    assert result == "a" * 40


def test_slugify_empty_fallback():
    assert slugify("!!!") == "store"
    assert slugify("") == "store"


def test_unique_slug_reserved_name_gets_suffix(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    assert engine.unique_slug("api") == "api-store"
    assert engine.unique_slug("health") == "health-store"


def test_unique_slug_collision_gets_numeric_suffix(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme-2").mkdir()
    assert unique_slug("acme") == "acme-3"


def test_unique_slug_no_collision_passthrough(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    assert engine.unique_slug("brand-new") == "brand-new"


def test_unique_slug_db_row_without_disk_dir_gets_suffix(tmp_path, monkeypatch):
    # A blocked store leaves its stores.slug row behind after its disk dir is
    # removed; unique_slug must still treat that slug as taken (disk OR db).
    import app.engine as engine
    from app.db import SessionLocal
    from app.models import Store, get_or_create_merchant

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)  # empty dir -> no disk hit
    with SessionLocal() as s:
        merchant = get_or_create_merchant(s, "0x" + "a" * 40)
        s.add(
            Store(
                slug="x",
                merchant_id=merchant.id,
                status="blocked",
                pay_to="0x" + "a" * 40,
                theme="original.html",
            )
        )
        s.commit()
    assert engine.unique_slug("x") == "x-2"


def test_unique_slug_maxlen_collision_stays_within_pattern(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    base = "a" * config.SLUG_MAX_LEN  # already at the length ceiling
    (tmp_path / base).mkdir()
    slug = engine.unique_slug(base)
    assert slug != base
    assert len(slug) <= config.SLUG_MAX_LEN
    # the returned slug must satisfy the exact pattern the checkout route uses,
    # otherwise POST /api/checkout/{slug} 422s forever.
    assert re.fullmatch(config.SLUG_PATTERN, slug)


def _fake_llm_response(monkeypatch, raw: dict):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": json.dumps(raw)}]}

    monkeypatch.setattr("app.engine.requests.post", lambda *a, **k: FakeResp())


def test_generate_clamps_missing_price(monkeypatch):
    _fake_llm_response(monkeypatch, {"store_name": "No Price Store"})
    data = generate("i sell a thing")
    assert data["price_usdt"] >= 0.01
    assert data["price_usdt"] != 0


@respx.mock
def test_create_store_missing_price_never_goes_live_at_zero(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm_response(monkeypatch, {"store_name": "No Price Store"})
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    result = engine.create_store("i sell a thing")
    meta = json.loads(
        (tmp_path / result["slug"] / "store.json").read_text(encoding="utf-8")
    )
    assert meta["status"] == "live"
    assert meta["amount_usdt"] >= 0.01
    assert meta["amount_usdt"] != 0


def test_screening_text_includes_cta_and_emoji():
    content = {
        "store_name": "Store",
        "cta_text": "Wire me BTC now",
        "emoji": "💸",
    }
    text = _screening_text("desc", content)
    assert "Wire me BTC now" in text
    assert "💸" in text


def test_rerender_stores_rewrites_live_index_from_content(tmp_path, monkeypatch):
    # A theme fix reaches already-deployed static pages: rerender_stores rebuilds
    # index.html for every live store from its persisted content.
    import app.engine as engine
    from app.db import SessionLocal
    from app.models import Store, get_or_create_merchant

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    with SessionLocal() as s:
        merchant = get_or_create_merchant(s, "0x" + "a" * 40)
        s.add(
            Store(
                slug="rr",
                merchant_id=merchant.id,
                status="live",
                pay_to="0x" + "a" * 40,
                content={"store_name": "RR", "price_usdt": 9, "product_name": "Thing"},
                theme="original.html",
            )
        )
        s.commit()

    out = engine.rerender_stores()
    assert out == {"rendered": 1, "skipped": 0}

    html = (tmp_path / "rr" / "index.html").read_text(encoding="utf-8")
    # the redeployed page carries the current M5 checkout partial (wallet-connect +
    # QR + sign-to-claim), proving a theme change reaches already-deployed pages.
    assert 'id="coAmount"' in html
    assert 'id="coAddr"' in html
    assert 'id="payWalletBtn"' in html
    assert "qrMatrix" in html


def test_rerender_stores_skips_contentless_store(tmp_path, monkeypatch):
    # A pre-persistence live store (content NULL) can't be re-rendered; it must be
    # skipped, never crash the startup re-render.
    import app.engine as engine
    from app.db import SessionLocal
    from app.models import Store, get_or_create_merchant

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    with SessionLocal() as s:
        merchant = get_or_create_merchant(s, "0x" + "b" * 40)
        s.add(
            Store(
                slug="old",
                merchant_id=merchant.id,
                status="live",
                pay_to="0x" + "b" * 40,
                content=None,
                theme="original.html",
            )
        )
        s.commit()

    assert engine.rerender_stores() == {"rendered": 0, "skipped": 1}
    assert not (tmp_path / "old" / "index.html").exists()
