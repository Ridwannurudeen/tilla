import json
import re

import httpx
import respx

from app import config
from app.config import WARDEN_SCREEN_URL
from app.engine import (
    _resolve_theme,
    _screening_text,
    generate,
    slugify,
    unique_slug,
)


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


def _fake_llm_response(monkeypatch, raw: dict, usage: dict | None = None):
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            body = {"content": [{"text": json.dumps(raw)}]}
            if usage is not None:
                body["usage"] = usage
            return body

    monkeypatch.setattr("app.engine.requests.post", lambda *a, **k: FakeResp())


def test_generate_clamps_missing_price(monkeypatch):
    _fake_llm_response(monkeypatch, {"store_name": "No Price Store"})
    data = generate("i sell a thing")
    assert data["price_usdt"] >= 0.01
    assert data["price_usdt"] != 0


def test_generate_returns_llm_theme_suggestion(monkeypatch):
    _fake_llm_response(monkeypatch, {"store_name": "Loud Co", "theme": "bold"})
    assert generate("i sell a thing")["theme"] == "bold"


def test_generate_coerces_unknown_theme_to_default(monkeypatch):
    # A stray theme the LLM invents must never fail generation — it's coerced to
    # the default so create_store keeps working.
    _fake_llm_response(monkeypatch, {"store_name": "X", "theme": "chartreuse"})
    assert generate("i sell a thing")["theme"] == "original"


def test_generate_defaults_theme_when_absent(monkeypatch):
    _fake_llm_response(monkeypatch, {"store_name": "X"})
    assert generate("i sell a thing")["theme"] == "original"


def test_resolve_theme_maps_short_name_to_template():
    assert _resolve_theme("bold") == "bold.html"
    assert _resolve_theme("editorial") == "editorial.html"
    assert _resolve_theme(None) == config.DEFAULT_THEME
    assert _resolve_theme("nope") == config.DEFAULT_THEME


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


def test_write_og_png_skips_when_rsvg_absent(tmp_path, monkeypatch):
    # No rasterizer on this host (dev/Windows): og.png generation is a silent
    # no-op — never a raise that could break the paid create-store path.
    import app.engine as engine

    monkeypatch.setattr(engine, "_RSVG_BIN", None)
    svg = tmp_path / "og.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    engine._write_og_png(svg, tmp_path / "og.png")
    assert not (tmp_path / "og.png").exists()


def test_write_og_png_invokes_rsvg_with_1200x630(tmp_path, monkeypatch):
    import app.engine as engine

    calls = []
    monkeypatch.setattr(engine, "_RSVG_BIN", "/usr/bin/rsvg-convert")
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    svg, png = tmp_path / "og.svg", tmp_path / "og.png"
    engine._write_og_png(svg, png)
    (argv,), kwargs = calls[0]
    assert argv[0] == "/usr/bin/rsvg-convert"
    assert "1200" in argv and "630" in argv
    assert str(svg) in argv and str(png) in argv
    assert kwargs["check"] is True and kwargs["timeout"] == 15


def test_write_og_png_failopen_on_rsvg_error(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "_RSVG_BIN", "/usr/bin/rsvg-convert")

    def boom(*a, **k):
        raise engine.subprocess.CalledProcessError(1, "rsvg-convert")

    monkeypatch.setattr(engine.subprocess, "run", boom)
    # Must NOT raise — a rasterization failure is swallowed and logged.
    engine._write_og_png(tmp_path / "og.svg", tmp_path / "og.png")


def test_write_store_pages_writes_svg_and_index_without_rsvg(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "_RSVG_BIN", None)
    d = tmp_path / "aperture"
    d.mkdir()
    engine._write_store_pages(
        d, {"store_name": "A", "price_usdt": 9, "emoji": "x"}, "0xabc", "aperture", "original.html"
    )
    assert (d / "index.html").exists()
    assert (d / "og.svg").exists()
    assert not (d / "og.png").exists()


def test_generated_content_multiproduct_mirrors_primary():
    from app.engine import GeneratedContent

    g = GeneratedContent.model_validate(
        {
            "store_name": "Cafe",
            "products": [
                {"name": "Guji", "blurb": "floral", "price_usdt": 18, "cta_text": "Buy"},
                {"name": "Yirg", "blurb": "citrus", "price_usdt": 16},
            ],
        }
    ).model_dump()
    assert [p["name"] for p in g["products"]] == ["Guji", "Yirg"]
    # the legacy scalar fields mirror the primary (first) product
    assert g["product_name"] == "Guji" and g["price_usdt"] == 18


def test_generated_content_oldstyle_synthesizes_one_product():
    from app.engine import GeneratedContent

    g = GeneratedContent.model_validate(
        {
            "store_name": "S",
            "product_name": "Widget",
            "product_blurb": "b",
            "price_usdt": 9,
            "cta_text": "Get",
        }
    ).model_dump()
    assert len(g["products"]) == 1
    assert g["products"][0]["name"] == "Widget" and g["products"][0]["price_usdt"] == 9


@respx.mock
def test_create_store_creates_a_product_row_per_catalog_item(tmp_path, monkeypatch):
    import app.engine as engine
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Product, Store

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm_response(
        monkeypatch,
        {
            "store_name": "Cafe",
            "products": [
                {"name": "Guji", "blurb": "floral", "price_usdt": 18, "cta_text": "Buy"},
                {"name": "Yirg", "blurb": "citrus", "price_usdt": 16, "cta_text": "Buy"},
            ],
        },
    )
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    result = engine.create_store("i sell coffee")
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == result["slug"]))
        rows = s.scalars(
            select(Product).where(Product.store_id == store.id).order_by(Product.id)
        ).all()
    assert [(r.name, r.price_micro) for r in rows] == [
        ("Guji", 18_000_000),
        ("Yirg", 16_000_000),
    ]
