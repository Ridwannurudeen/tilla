from app.engine import render, slugify


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


def test_render_all_tokens_replaced():
    content = {
        "store_name": "Acme Supply",
        "tagline": "Tools that ship",
        "hero_headline": "Build faster today",
        "hero_subcopy": "The one product you actually need.",
        "product_name": "Widget Pro",
        "product_blurb": "It widgets, beautifully.",
        "cta_text": "Get it",
        "price_usdt": 9,
        "emoji": "🚀",
        "palette": {
            "primary": "#111111",
            "accent": "#222222",
            "bg": "#333333",
            "text": "#444444",
        },
    }
    html = render(content, "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51", "acme-supply")

    assert "{{" not in html
    assert "Acme Supply" in html
    assert "Tools that ship" in html
    assert "Build faster today" in html
    assert "Widget Pro" in html
    assert "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51" in html
    assert "acme-supply" in html
    assert "#111111" in html
    assert "#222222" in html
    assert "#333333" in html
    assert "#444444" in html
    assert "🚀" in html
