import collections
import json
import re

import httpx
import respx

from app import config
from app.config import WARDEN_SCREEN_URL
from app.engine import (
    _PERSONAS,
    _resolve_theme,
    _screening_text,
    generate,
    resolve_design,
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


def test_unique_slug_collision_prefers_the_brands_own_words(tmp_path, monkeypatch):
    """A numeric suffix announces that something generated the store and that a
    near-identical one already exists. The brand's own vocabulary does not."""
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    (tmp_path / "highland-roast").mkdir()
    content = {
        "store_name": "Highland Roast",
        "tagline": "Small batch beans",
        "products": [{"name": "Ethiopia Single Origin"}],
    }
    assert unique_slug("highland-roast", content) == "highland-roast-small"


def test_unique_slug_falls_back_to_trade_words_then_numbers(tmp_path, monkeypatch):
    """With no brand vocabulary to draw on, a trade word still reads as a decision.
    The numeric tail is kept only as the fallback that is guaranteed to terminate."""
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    (tmp_path / "acme").mkdir()
    assert unique_slug("acme") == "acme-co"

    # exhaust every trade word and the numeric tail takes over
    for suffix in engine._SLUG_TRADE_SUFFIXES:
        (tmp_path / f"acme-{suffix}").mkdir()
    assert unique_slug("acme") == "acme-2"


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
    # taken in the DB but not on disk -> still treated as taken, so it gets a suffix
    assert engine.unique_slug("x") == "x-co"


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


@respx.mock
def test_create_store_honours_a_stated_sub_one_usdt_price(tmp_path, monkeypatch):
    # Regression: a merchant who states "0.5 USDT" was silently repriced to 1.0 by
    # the persist-time floor, while the create receipt still quoted 0.5 — so the
    # receipt promised half what checkout would charge. The receipt, the persisted
    # Product row and store.json must all agree on the stated price.
    import app.engine as engine
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Product, Store

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm_response(
        monkeypatch,
        {
            "store_name": "Dossier",
            "products": [
                {"name": "Due Diligence Report", "price_usdt": 0.5, "cta_text": "Buy"}
            ],
        },
    )
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    result = engine.create_store("reports for 0.5 USDT")

    assert result["price_usdt"] == 0.5
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == result["slug"]))
        rows = s.scalars(
            select(Product).where(Product.store_id == store.id).order_by(Product.id)
        ).all()
    assert [(r.name, r.price_micro) for r in rows] == [
        ("Due Diligence Report", 500_000)
    ]
    meta = json.loads(
        (tmp_path / result["slug"] / "store.json").read_text(encoding="utf-8")
    )
    assert meta["amount_usdt"] == 0.5


@respx.mock
def test_create_store_receipt_matches_persisted_catalog(tmp_path, monkeypatch):
    # The receipt is built from the post-resync catalog, so whatever the persist
    # path stores is what the caller is quoted — no divergence is possible.
    import app.engine as engine
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Product, Store

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm_response(
        monkeypatch,
        {
            "store_name": "Postcards",
            "products": [
                {"name": "Thank You Postcard", "price_usdt": 0.25, "cta_text": "Buy"}
            ],
        },
    )
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    result = engine.create_store("postcards")

    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == result["slug"]))
        primary = s.scalars(
            select(Product).where(Product.store_id == store.id).order_by(Product.id)
        ).first()
    assert result["price_usdt"] == primary.price_micro / 1e6
    assert result["product_name"] == primary.name


def test_screening_text_includes_cta_and_emoji():
    content = {
        "store_name": "Store",
        "cta_text": "Wire me BTC now",
        "emoji": "💸",
    }
    text = _screening_text("desc", content)
    assert "Wire me BTC now" in text
    assert "💸" in text


def test_an_over_long_art_prompt_is_cut_on_a_word_boundary():
    # Regression from the live catalogue: the first pass chopped every prompt
    # mid-word ('...suggesting financ') and that fragment is what the image
    # generator was actually handed.
    from app.engine import ART_PROMPT_MAX, GeneratedContent, clip_words

    long = ("morning light across a clean oak desk " * 12).strip()
    out = GeneratedContent(hero_art_prompt=long).hero_art_prompt
    assert len(out) <= ART_PROMPT_MAX
    assert not out.endswith(" ")
    assert long.startswith(out), "the kept text must be a prefix, not a rewrite"
    assert out.split()[-1] in long.split(), "last token must be a whole word"
    # short prompts pass through untouched
    assert clip_words("a calm studio corner", ART_PROMPT_MAX) == "a calm studio corner"
    assert clip_words(None, ART_PROMPT_MAX) == ""


def test_screening_text_includes_the_generated_art_prompt():
    # hero_art_prompt never reaches a stock provider, but it IS the literal
    # instruction an image generator is handed — if anything the more important of
    # the two to screen.
    text = _screening_text(
        "desc", {"hero_art_prompt": "morning light across a clean oak desk"}
    )
    assert "morning light across a clean oak desk" in text


def test_screening_text_includes_every_product_not_just_the_first():
    # The scalar product_name/product_blurb only carry product 1. A multi-product
    # store's later products are model-generated copy that gets rendered, so they
    # go to Warden on the same single call — otherwise products 2..N are screened
    # by nobody.
    content = {
        "store_name": "Store",
        "product_name": "First",
        "product_blurb": "first blurb",
        "products": [
            {"name": "First", "blurb": "first blurb", "cta_text": "Buy now"},
            {
                "name": "Second",
                "blurb": "wire me BTC for this one",
                "cta_text": "Send crypto direct",
            },
        ],
    }
    text = _screening_text("desc", content)
    assert "Second" in text
    assert "wire me BTC for this one" in text
    assert "Send crypto direct" in text


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


def test_rerender_stores_covers_sandbox_stores_and_marks_them(tmp_path, monkeypatch):
    # A sandbox store has a served index.html exactly like a live one, so excluding
    # it from the startup re-render meant the sample stores — the pages that most
    # need to say they cannot take payments — could never receive a theme fix.
    import app.engine as engine
    from app.db import SessionLocal
    from app.models import Store, get_or_create_merchant

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    content = {"store_name": "S", "price_usdt": 9, "product_name": "Thing"}
    with SessionLocal() as s:
        merchant = get_or_create_merchant(s, "0x" + "c" * 40)
        for slug, status in (
            ("sb", "sandbox"),
            ("lv", "live"),
            ("pd", "pending_screening"),
        ):
            s.add(
                Store(
                    slug=slug,
                    merchant_id=merchant.id,
                    status=status,
                    pay_to="0x" + "c" * 40,
                    content=content,
                    theme="original.html",
                )
            )
        s.commit()

    assert engine.rerender_stores() == {"rendered": 2, "skipped": 0}

    sandbox_html = (tmp_path / "sb" / "index.html").read_text(encoding="utf-8")
    assert "Sample store — not payable" in sandbox_html
    assert '<button class="buy" disabled' in sandbox_html
    # the live store next to it is untouched by the sandbox branch
    live_html = (tmp_path / "lv" / "index.html").read_text(encoding="utf-8")
    assert "Sample store" not in live_html
    assert '<button class="buy" disabled' not in live_html
    # a pending_screening store still gets NO page: writing one would open a
    # checkout that screening has not cleared
    assert not (tmp_path / "pd" / "index.html").exists()


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
        d,
        {"store_name": "A", "price_usdt": 9, "emoji": "x"},
        "0xabc",
        "aperture",
        "original.html",
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
                {
                    "name": "Guji",
                    "blurb": "floral",
                    "price_usdt": 18,
                    "cta_text": "Buy",
                },
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
                {
                    "name": "Guji",
                    "blurb": "floral",
                    "price_usdt": 18,
                    "cta_text": "Buy",
                },
                {
                    "name": "Yirg",
                    "blurb": "citrus",
                    "price_usdt": 16,
                    "cta_text": "Buy",
                },
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


@respx.mock
def test_create_store_populates_content_product_ids(tmp_path, monkeypatch):
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
                {"name": "Guji", "price_usdt": 18, "cta_text": "Buy"},
                {"name": "Yirg", "price_usdt": 16, "cta_text": "Buy"},
            ],
        },
    )
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )
    result = engine.create_store("coffee")
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == result["slug"]))
        rows = s.scalars(
            select(Product).where(Product.store_id == store.id).order_by(Product.id)
        ).all()
        # content carries the stable product ids -> id-based buy buttons
        assert [it["id"] for it in store.content["products"]] == [r.id for r in rows]
    html = (tmp_path / result["slug"] / "index.html").read_text(encoding="utf-8")
    assert f'data-pid="{rows[0].id}"' in html and f'data-pid="{rows[1].id}"' in html


# ------------------------------------------------------- seeded visual identity
def test_resolve_design_is_deterministic_in_the_slug():
    """A store's look must never drift. resync_catalog re-renders on any pricing or
    product edit, so a non-deterministic pick would silently restyle a live shop."""
    first = resolve_design("highland-roast", {})
    for _ in range(5):
        assert resolve_design("highland-roast", {}) == first


def test_resolve_design_spreads_across_the_space():
    """The bug this replaces: asked to choose the five axes freely, the LLM returned
    ~3 answers for the whole catalogue. A seeded pick must not have a modal answer."""
    slugs = [f"store-{n}" for n in range(300)]
    combos, themes, heroes = set(), set(), set()
    for slug in slugs:
        dna, _persona, theme = resolve_design(slug, {})
        combos.add(tuple(sorted(dna.items())))
        themes.add(theme)
        heroes.add(dna["hero"])
    assert len(combos) >= 40, f"only {len(combos)} distinct looks across 300 slugs"
    assert themes == {"original", "bold", "editorial"}, themes
    assert heroes == {"stacked", "split", "offset"}, heroes


def test_published_design_spread_figures_still_hold():
    """README.md and docs/DESIGN-DNA.md publish these exact numbers. They were once
    quoted from a run the code had moved past — 81 looks and 2.5% when the truth was
    90 and 1.68% — and nothing caught it, because prose cannot fail. This test is the
    guard: if it fails the code changed legitimately, and BOTH documents must be
    updated to whatever it now reports rather than the assertion relaxed."""
    n = 4000
    combos = collections.Counter()
    personas = collections.Counter()
    for i in range(n):
        dna, persona, _theme = resolve_design(f"store-{i}", {})
        combos[tuple(sorted(dna.items()))] += 1
        personas[persona] += 1

    top_share = combos.most_common(1)[0][1] / n * 100
    shares = sorted(c / n * 100 for c in personas.values())

    assert len(combos) == 90, f"README says 90 distinct looks, got {len(combos)}"
    assert round(top_share, 2) == 1.68, (
        f"README says 1.68% modal share, got {top_share:.2f}%"
    )
    assert len(personas) == len(_PERSONAS) == 10
    assert round(shares[0], 1) == 9.3, (
        f"README says personas start at 9.3%, got {shares[0]:.2f}%"
    )
    assert round(shares[-1], 1) == 10.6, (
        f"README says personas top out at 10.6%, got {shares[-1]:.2f}%"
    )


def test_resolve_design_honours_an_llm_named_persona():
    dna, persona, theme = resolve_design("some-slug", {"design_persona": "poster"})
    assert persona == "poster"
    assert theme == _PERSONAS["poster"]["theme"]
    # scale/rhythm/hero come from the persona; weight+texture are seed-jittered
    assert dna["scale"] == _PERSONAS["poster"]["scale"]
    assert dna["hero"] == _PERSONAS["poster"]["hero"]


def test_resolve_design_ignores_a_bogus_persona_name():
    """An unrecognised name must fall back to the seed, never raise or blank out —
    same fail-closed spirit as the theme and palette validators."""
    seeded = resolve_design("some-slug", {})
    for bogus in ["", "nope", 42, None, [], {"a": 1}]:
        assert resolve_design("some-slug", {"design_persona": bogus}) == seeded


def test_resolve_design_emits_only_valid_axis_values():
    """Every value must be one the DesignDNA enum accepts, or render falls back to
    defaults and the whole exercise silently does nothing."""
    valid = {
        "scale": {"compact", "balanced", "dramatic", "monumental"},
        "weight": {"light", "regular", "heavy"},
        "rhythm": {"tight", "roomy", "airy"},
        "hero": {"stacked", "split", "offset"},
        "texture": {"sparse", "medium", "dense"},
        "type_pair": {"grotesk", "serif-display", "serif", "mono-display"},
    }
    for n in range(200):
        dna, _persona, theme = resolve_design(f"s-{n}", {})
        assert set(dna) == set(valid)
        for axis, allowed in valid.items():
            assert dna[axis] in allowed, (axis, dna[axis])
        assert theme in config.ALLOWED_THEMES


def test_every_persona_is_coherent_and_complete():
    """A persona missing an axis would silently render that axis as a default,
    quietly collapsing the variety this table exists to create."""
    for name, persona in _PERSONAS.items():
        assert set(persona) == {
            "theme",
            "scale",
            "weight",
            "rhythm",
            "hero",
            "texture",
            "type_pair",
        }, name
        assert persona["theme"] in config.ALLOWED_THEMES, name


def test_slug_words_skips_stopwords_and_short_tokens():
    from app.engine import slug_words

    words = slug_words(
        {
            "store_name": "The Ember Co",
            "tagline": "For the love of it",
            "products": [{"name": "Oak Candle"}],
        }
    )
    assert "the" not in words and "of" not in words and "it" not in words
    assert "co" not in words  # under three characters
    assert "ember" in words and "candle" in words and "oak" in words


def test_slug_words_tolerates_junk_content():
    from app.engine import slug_words

    for junk in [None, "not-a-mapping", 7, {"products": "nope"}, {"tagline": 5}]:
        assert isinstance(slug_words(junk), tuple)


def test_unique_slug_never_reuses_a_word_already_in_the_base(tmp_path, monkeypatch):
    """ "ember-ember" would be worse than a number."""
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    (tmp_path / "ember").mkdir()
    slug = engine.unique_slug("ember", {"store_name": "Ember", "tagline": "Ember only"})
    assert slug != "ember-ember"
    assert slug.startswith("ember-")


def test_unique_slug_stays_in_pattern_with_a_long_brand_word(tmp_path, monkeypatch):
    """A long suffix must not push the slug past SLUG_MAX_LEN — a slug over the
    bound 422s at checkout, so the store would be unbuyable."""
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    base = "b" * (config.SLUG_MAX_LEN - 2)
    (tmp_path / base).mkdir()
    slug = engine.unique_slug(base, {"tagline": "extraordinarily-long-descriptor"})
    assert len(slug) <= config.SLUG_MAX_LEN
    assert re.fullmatch(config.SLUG_PATTERN, slug), slug


# ---------- generation request shape (docs/ISSUES.md #1) ----------
# A customer bought twice with identical input and got different product names,
# different prices (5000 vs 2500) and a different product count. These pin the two
# halves of the fix: greedy sampling, and prices that belong to the merchant.
class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload or {
            "content": [{"text": "{}"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")


def test_generation_requests_greedy_sampling(monkeypatch):
    import app.engine as engine

    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(json)
        return _Resp()

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "KEY", "test-key")
    monkeypatch.setattr(engine.requests, "post", fake_post)
    engine._post_generation("hello")
    assert sent["temperature"] == 0


def test_generation_drops_temperature_when_the_model_rejects_it(monkeypatch):
    """Opus 4.7+, Sonnet 5 and Fable 5 all 400 on `temperature`, and
    TILLA_LLM_MODEL is env-settable — a model swap must not take every paid
    create-store down with it."""
    import app.engine as engine

    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(dict(json))
        if "temperature" in json:
            return _Resp(
                status=400,
                text="temperature: Extra inputs are not permitted",
            )
        return _Resp()

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "KEY", "test-key")
    monkeypatch.setattr(engine.requests, "post", fake_post)
    out = engine._post_generation("hello")
    assert out["content"][0]["text"] == "{}"
    assert len(calls) == 2, "should retry exactly once"
    assert "temperature" in calls[0] and "temperature" not in calls[1]


def test_generation_still_fails_on_an_unrelated_400(monkeypatch):
    import app.engine as engine
    import pytest

    def fake_post(url, headers=None, json=None, timeout=None):
        return _Resp(status=400, text="messages: field required")

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "KEY", "test-key")
    monkeypatch.setattr(engine.requests, "post", fake_post)
    with pytest.raises(engine.GenerationUnavailable):
        engine._post_generation("hello")


def test_prompt_makes_prices_belong_to_the_merchant(monkeypatch):
    import app.engine as engine

    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(json)
        return _Resp()

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "KEY", "test-key")
    monkeypatch.setattr(engine.requests, "post", fake_post)
    try:
        engine.generate("I sell candles for 25 USDT")
    except Exception:
        pass  # the empty {} payload fails validation downstream; the prompt is the assertion
    prompt = sent["messages"][0]["content"]
    assert "use that EXACT number" in prompt
    assert "never invent a large headline figure" in prompt


def test_prompt_makes_names_belong_to_the_merchant(monkeypatch):
    # Same rule as prices, worse when broken: store_name mints the slug and a
    # rename keeps the original slug forever, so a substituted brand is permanent.
    import app.engine as engine

    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": "{}"}]}

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "KEY", "test-key")
    monkeypatch.setattr(
        engine.requests,
        "post",
        lambda url, headers=None, json=None, timeout=None: sent.update(json) or _Resp(),
    )
    try:
        engine.generate("my shop is called Ember & Oak, I sell soy candles")
    except Exception:
        pass  # the empty {} payload fails downstream; the prompt is the assertion
    prompt = sent["messages"][0]["content"]
    assert "use that EXACT name" in prompt
    assert "never translate, shorten, prettify or substitute it" in prompt
    # An invented name is the one invented string a merchant cannot correct (it mints
    # the slug), so the licence to invent one must not mint a credential with it.
    assert "must NOT contain a credential, certification or authority word" in prompt
    assert "Certified, Licensed, Official, Accredited, Institutional" in prompt


def test_prompt_forbids_inventing_capabilities_the_merchant_never_claimed(monkeypatch):
    # Third instance of the prices/names rule, and the one that reaches a buyer's
    # wallet: a "benefit-led" blurb from a thin description invented "team
    # credibility and tokenomics analysis" for a due-diligence store whose product
    # produces neither. A false capability claim on a merchant's own storefront.
    import app.engine as engine

    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": "{}"}]}

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "KEY", "test-key")
    monkeypatch.setattr(
        engine.requests,
        "post",
        lambda url, headers=None, json=None, timeout=None: sent.update(json) or _Resp(),
    )
    try:
        engine.generate("I sell signed token due-diligence reports for EVM tokens")
    except Exception:
        pass  # the empty {} payload fails downstream; the prompt is the assertion
    prompt = sent["messages"][0]["content"]
    assert "describe only what the merchant's description supports" in prompt
    assert "Never invent " in prompt
    assert "Vague and true beats detailed and false" in prompt
    # The first attempt at this fix FAILED in production while a test like the three
    # lines above passed: the rule was present, but the field spec still said the
    # blurb should be "benefit-led", and the model followed the instruction attached
    # to the field. A constraint that contradicts its own field spec does nothing.
    assert "benefit-led" not in prompt
    assert "claiming ONLY what the merchant stated" in prompt
    # Concrete negative example — the model followed the abstract rule far less
    # reliably than it follows a worked example of the exact failure.
    assert (
        "do NOT add 'covering contract security, tokenomics, team credibility'"
        in prompt
    )
    assert "A SHORT description must yield SHORT copy" in prompt


def test_prompt_defaults_to_one_product_and_forbids_invented_tiers(monkeypatch):
    # Issue #3, the compound form of #1 and #2: "I sell consulting" generated a
    # three-tier ladder including "Project Consulting" at 2500 USDT — a product the
    # merchant does not sell at a price they never chose. The old rule made
    # multi-product the DEFAULT ("otherwise 2 to 4 distinct items") whenever the
    # model couldn't judge a description "clearly" singular, which a one-line
    # description never is. Prompt-string assertions cannot prove the behaviour
    # (the first #2 fix passed such a test while inert) — the live check is
    # generate() against thin inputs; this pins the contradictions out instead.
    import app.engine as engine

    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": "{}"}]}

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "KEY", "test-key")
    monkeypatch.setattr(
        engine.requests,
        "post",
        lambda url, headers=None, json=None, timeout=None: sent.update(json) or _Resp(),
    )
    try:
        engine.generate("I sell consulting")
    except Exception:
        pass  # the empty {} payload fails downstream; the prompt is the assertion
    prompt = sent["messages"][0]["content"]
    # the new default and its worked example
    assert (
        "ONE product unless the merchant's own description names more than one"
        in prompt
    )
    assert "Never invent a tiered ladder" in prompt
    assert "One product from a short description is the correct output" in prompt
    # the contradictions that would silently undo it, pinned OUT — the #2 lesson:
    # an early instruction beats a later rule, so neither may reappear anywhere
    assert "otherwise 2 to 4 distinct items" not in prompt
    assert "focused product catalog" not in prompt


def test_prompt_forbids_unstated_quantifiable_commitments(monkeypatch):
    # Issue #4, the residual edge after #2 and #3: "I sell trading signals" produced
    # the blurb "Daily trading signals to guide your market moves". One adjective, and
    # the storefront promises a delivery cadence the merchant never offered — the
    # claims rule read as satisfied because a measurable promise is not a capability
    # or a tier. Same prompt-string caveat as the test above: this pins the rule and
    # its worked example IN, and the tone carve-out with it. The carve-out is half the
    # fix — evocative texture ("rich, full-bodied") asserts nothing checkable, the
    # merchant who reported #2 praised it, and over-tightening would delete it.
    import app.engine as engine

    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": "{}"}]}

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "KEY", "test-key")
    monkeypatch.setattr(
        engine.requests,
        "post",
        lambda url, headers=None, json=None, timeout=None: sent.update(json) or _Resp(),
    )
    try:
        engine.generate("I sell trading signals")
    except Exception:
        pass  # the empty {} payload fails downstream; the prompt is the assertion
    prompt = sent["messages"][0]["content"]
    # the rule: frequency, turnaround, counts, guarantees — none unless stated
    assert "never state a delivery frequency" in prompt
    assert "a turnaround or response time" in prompt
    assert "a quantity or coverage count" in prompt
    assert (
        "or a guarantee, refund or SLA — unless the merchant's description states it"
        in prompt
    )
    # a commitment the merchant DID state is theirs and must survive
    assert "use it exactly as given and keep it" in prompt
    # the worked negative example of the exact failure
    assert "do NOT write 'Daily trading signals to guide your market moves'" in prompt
    # the tone carve-out, pinned IN so a later tightening cannot quietly drop it
    assert "Tone is not a commitment" in prompt
    assert "evocative and textural language is welcome" in prompt
    assert "what is forbidden is the measurable promise, not the warmth" in prompt


def test_prompt_anchors_the_hero_fields_not_just_the_product_blurb(monkeypatch):
    # The claims rule was written for #2, which was a blurb defect, so every example in
    # it was blurb-shaped and `blurb` was the only field carrying an on-field anchor —
    # leaving it readable as a products-only rule. hero_subcopy is the field that
    # travels furthest: agentic.py::_store_description returns
    # `hero_subcopy or tagline or store.description`, so this generated sentence, not
    # the merchant's own words, is the store description in feed.json, llms.txt, the
    # discovery surfaces, the RSS item and og:description — the machine-readable claim
    # a buying agent reads before it pays.
    import app.engine as engine

    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": "{}"}]}

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "KEY", "test-key")
    monkeypatch.setattr(
        engine.requests,
        "post",
        lambda url, headers=None, json=None, timeout=None: sent.update(json) or _Resp(),
    )
    try:
        engine.generate("I sell yoga classes")
    except Exception:
        pass  # the empty {} payload fails downstream; the prompt is the assertion
    prompt = sent["messages"][0]["content"]
    # (a) the anchor lives ON each hero field, the #2 lesson — a rule in a later
    # paragraph loses to the spec attached to the field
    assert "tagline (<=6 words, claiming ONLY what the merchant stated)" in prompt
    assert (
        "hero_headline (punchy, <=8 words, claiming ONLY what the merchant stated)"
        in prompt
    )
    assert "hero_subcopy (1 sentence, claiming ONLY what the merchant stated" in prompt
    # the length/tone specs the anchors were added to must survive alongside them
    assert "punchy" in prompt
    # (b) the rule's own scope names every copy field, so it can no longer be read as
    # products-only
    assert (
        "and for claims, in EVERY copy field (tagline, hero_headline, hero_subcopy, "
        "blurb, cta_text)" in prompt
    )
    # (c) a worked example on a HERO field, because a blurb-shaped one is how the rule
    # came to be misread. Credentials are the class hero copy attracts.
    assert "The same applies to the hero fields: from 'I sell yoga classes'" in prompt
    assert (
        "hero_subcopy must NOT be 'Certified instructors bring a decade of studio "
        "experience to every session'" in prompt
    )


def test_copy_restraint_shares_the_storefront_prompts_vocabulary(monkeypatch):
    """COPY_RESTRAINT is the compact form of this prompt's claims/commitments rules,
    imported by app/growth.py's two prompt builders. It is a SECOND copy of a policy,
    which is exactly how the storefront and growth surfaces would drift apart — so the
    shared clauses are pinned to appear in both. If a rule is reworded here, this fails
    until the constant is reworded too (and vice versa)."""
    import app.engine as engine

    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": "{}"}]}

    monkeypatch.setenv("TILLA_LLM_KEY", "test-key")
    monkeypatch.setattr(engine, "KEY", "test-key")
    monkeypatch.setattr(
        engine.requests,
        "post",
        lambda url, headers=None, json=None, timeout=None: sent.update(json) or _Resp(),
    )
    try:
        engine.generate("I sell trading signals")
    except Exception:
        pass  # the empty {} payload fails downstream; the prompt is the assertion
    prompt = sent["messages"][0]["content"].lower()
    restraint = engine.COPY_RESTRAINT.lower()
    for clause in (
        "never invent",
        "never state a delivery frequency",
        "a turnaround or response time",
        "a guarantee, refund or sla",
        "what is forbidden is the measurable promise, not the warmth",
    ):
        assert clause in restraint, clause
        assert clause in prompt, clause
    # The traction clause is growth-only by design: the storefront prompt has no
    # performance numbers in it, and only growth's scheduler feeds real ones in.
    assert "never assert traction, demand or popularity" in restraint
    assert "flying off the shelves" in restraint


def test_generate_quantises_price_to_micro_precision(monkeypatch):
    # store.json is written from generated content before the Product row exists,
    # so a price finer than USDT0's 6dp would persist as a rounded price_micro the
    # on-disk record never sees. A sub-micro price is economically zero and takes
    # the same clamp a non-positive one does.
    _fake_llm_response(
        monkeypatch,
        {
            "store_name": "Dust",
            "products": [{"name": "Speck", "price_usdt": 0.0000004, "cta_text": "Buy"}],
        },
    )
    import app.engine as engine

    data = generate("dust-priced item")
    assert data["products"][0]["price_usdt"] == engine.MIN_PRICE_USDT
    assert data["price_usdt"] == engine.MIN_PRICE_USDT


def test_generate_preserves_a_micro_precise_price(monkeypatch):
    _fake_llm_response(
        monkeypatch,
        {
            "store_name": "Fine",
            "products": [{"name": "Bit", "price_usdt": 0.123456, "cta_text": "Buy"}],
        },
    )
    assert generate("fine-priced item")["price_usdt"] == 0.123456


# ---------- resync_catalog attaches extras by product (docs/ISSUES.md #9) ----------
def _catalog_rows(make_store, slug, *names):
    """A live store whose ACTIVE Product rows are ``names``, in id order."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Product

    sid = make_store(slug=slug)
    with SessionLocal() as s:
        rows = s.scalars(
            select(Product).where(Product.store_id == sid).order_by(Product.id)
        ).all()
        rows[0].name = names[0]
        for name in names[1:]:
            s.add(Product(store_id=sid, name=name, price_micro=5_000_000, active=True))
        s.commit()
    return sid


def _resync_with(sid, items):
    """Persist ``items`` as the store's catalog, resync, and return the new content."""
    import app.engine as engine
    from app.db import SessionLocal
    from app.models import Store

    with SessionLocal() as s:
        store = s.get(Store, sid)
        store.content = {"store_name": "S", "products": items}
        engine.resync_catalog(s, store)
        s.commit()
        return store.content


def test_resync_attaches_an_idless_blurb_by_name_not_by_position(make_store):
    """The #9 defect. `upgrade_store` replaces content with FRESHLY GENERATED items
    that carry no id and whose order is the model's, so backfilling ids by position
    printed a blurb describing one product on a different product's card — false text
    about a real product, and since #6 propagated verbatim to every feed."""
    sid = _catalog_rows(make_store, "byname", "Espresso Beans", "Ceramic Mug")
    products = _resync_with(
        sid,
        [
            {
                "name": "Ceramic Mug",
                "blurb": "stoneware, 300ml",
                "cta_text": "Add to cart",
                "image_query": "stoneware mug on a bench",
            },
            {"name": "Espresso Beans", "blurb": "washed, chocolate finish"},
        ],
    )["products"]
    by_name = {p["name"]: p for p in products}
    assert by_name["Espresso Beans"]["blurb"] == "washed, chocolate finish"
    assert by_name["Ceramic Mug"]["blurb"] == "stoneware, 300ml"
    # cta and the photography search text are display extras too, same rule
    assert by_name["Ceramic Mug"]["cta_text"] == "Add to cart"
    assert by_name["Espresso Beans"]["cta_text"] == "Buy now"
    assert by_name["Ceramic Mug"]["image_query"] == "stoneware mug on a bench"
    assert "image_query" not in by_name["Espresso Beans"]


def test_resync_drops_extras_for_a_generated_item_matching_no_product(make_store):
    """generate() is asked for 1-4 products and an upgrade leaves the Product rows
    alone, so the regenerated catalog routinely holds items the store does not sell.
    An invented item's copy must attach to nothing, not slide onto a real row."""
    sid = _catalog_rows(make_store, "invented", "Espresso Beans", "Ceramic Mug")
    products = _resync_with(
        sid,
        [
            {"name": "Espresso Beans", "blurb": "washed, chocolate finish"},
            {"name": "Tasting Flight", "blurb": "four cups, poured to order"},
            {"name": "Ceramic Mug", "blurb": "stoneware, 300ml"},
        ],
    )["products"]
    assert {p["name"]: p["blurb"] for p in products} == {
        "Espresso Beans": "washed, chocolate finish",
        "Ceramic Mug": "stoneware, 300ml",
    }
    assert "four cups" not in json.dumps(products)


def test_resync_carries_the_only_blurb_onto_the_only_product(make_store):
    """One old item and one active row can only mean each other, so the common
    single-product upgrade still carries its copy even when the model renamed the
    product. No misattachment is possible with one of each."""
    sid = _catalog_rows(make_store, "solo", "Signal Digest")
    products = _resync_with(
        sid,
        [{"name": "Daily Signals", "blurb": "market notes", "cta_text": "Subscribe"}],
    )["products"]
    assert products[0]["name"] == "Signal Digest"
    assert products[0]["blurb"] == "market notes"
    assert products[0]["cta_text"] == "Subscribe"


def test_resync_never_bleeds_a_blurb_when_no_name_matches(make_store):
    """Two of each and nothing matches: the honest outcome is an empty blurb the
    merchant can fix, never a guess. An empty blurb renders as no paragraph; wrong
    text renders as a claim about goods they do not sell."""
    sid = _catalog_rows(make_store, "nomatch", "Espresso Beans", "Ceramic Mug")
    content = _resync_with(
        sid,
        [
            {"name": "Tasting Flight", "blurb": "four cups, poured to order"},
            {"name": "Gift Card", "blurb": "any amount, any time"},
        ],
    )
    assert [p["blurb"] for p in content["products"]] == ["", ""]
    assert [p["cta_text"] for p in content["products"]] == ["Buy now", "Buy now"]
    # the mirrored scalar primary is rebuilt from the list, so it must be empty too —
    # render._catalog falls back to it and would print the dropped text.
    assert content["product_blurb"] == ""


def test_resync_matches_duplicate_names_in_id_order(make_store):
    """Two rows can legitimately share a name. Matches are consumed, so the first
    id-less item takes the lowest id and the second the next — deterministic, and
    never both blurbs on one row."""
    sid = _catalog_rows(make_store, "dupes", "Session Pass", "Session Pass")
    products = _resync_with(
        sid,
        [
            {"name": "Session Pass", "blurb": "morning"},
            {"name": "Session Pass", "blurb": "afternoon"},
        ],
    )["products"]
    assert [p["blurb"] for p in products] == ["morning", "afternoon"]
    assert products[0]["id"] < products[1]["id"]


def test_resync_keeps_id_carried_extras_and_lets_an_override_win(make_store):
    """The dashboard shape: content items carry ids, and the id is the strong link —
    it still decides even when the row has since been renamed past its content entry.
    extras_override (a product just added or edited) still wins over the carried copy.
    """
    from sqlalchemy import select

    import app.engine as engine
    from app.db import SessionLocal
    from app.models import Product, Store

    sid = _catalog_rows(make_store, "withids", "Espresso Beans", "Ceramic Mug")
    with SessionLocal() as s:
        ids = [
            p.id
            for p in s.scalars(
                select(Product).where(Product.store_id == sid).order_by(Product.id)
            ).all()
        ]
        store = s.get(Store, sid)
        store.content = {
            "store_name": "S",
            "products": [
                {"id": ids[0], "name": "an older name", "blurb": "washed"},
                {"id": ids[1], "name": "another older name", "blurb": "stoneware"},
            ],
        }
        engine.resync_catalog(
            s, store, extras_override={ids[1]: ("merchant's own words", "Get it")}
        )
        s.commit()
        products = store.content["products"]
    assert [p["id"] for p in products] == ids
    assert products[0]["blurb"] == "washed"
    assert (products[1]["blurb"], products[1]["cta_text"]) == (
        "merchant's own words",
        "Get it",
    )


def test_resync_still_backfills_legacy_precrud_content(make_store):
    """Regression guard for the shape the positional backfill existed to serve:
    content that predates per-item ids. The rows were created FROM that content, so
    its names ARE the row names and the name rule carries every extra exactly as
    before — including the photography a later upgrade re-resolves from."""
    sid = _catalog_rows(make_store, "legacy", "Guji", "Yirg")
    products = _resync_with(
        sid,
        [
            {
                "name": "Guji",
                "blurb": "floral",
                "cta_text": "Buy",
                "image_query": "guji beans",
            },
            {"name": "Yirg", "blurb": "citrus", "cta_text": "Get"},
        ],
    )["products"]
    assert [(p["name"], p["blurb"], p["cta_text"]) for p in products] == [
        ("Guji", "floral", "Buy"),
        ("Yirg", "citrus", "Get"),
    ]
    assert products[0]["image_query"] == "guji beans"
    assert all(p["id"] for p in products), "ids are backfilled onto id-less content"
