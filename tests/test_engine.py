import json
import re

from app import config
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


def test_screening_text_includes_cta_and_emoji():
    content = {
        "store_name": "Store",
        "cta_text": "Wire me BTC now",
        "emoji": "💸",
    }
    text = _screening_text("desc", content)
    assert "Wire me BTC now" in text
    assert "💸" in text
