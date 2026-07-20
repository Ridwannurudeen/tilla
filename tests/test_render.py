import pytest

from app.render import DEFAULT_PALETTE, render

ADDR = "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"
SLUG = "acme-supply"
THEMES = ["original.html", "bold.html", "editorial.html"]

XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "javascript:alert(1)",
    '"><script>alert(1)</script>',
    "</style><script>alert(1)</script>",
    "'; alert(1); //",
    "{{7*7}}",  # SSTI probe: must render as literal text, not evaluate to 49
]


def _content(payload):
    return {
        "store_name": payload,
        "tagline": payload,
        "hero_headline": payload,
        "hero_subcopy": payload,
        "product_name": payload,
        "product_blurb": payload,
        "cta_text": payload,
        "price_usdt": 9,
        "emoji": payload,
        "palette": {
            "primary": payload,
            "accent": payload,
            "bg": payload,
            "text": payload,
        },
    }


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_xss_corpus_renders_inert(theme, payload):
    html = render(_content(payload), ADDR, SLUG, theme)
    # the payload must never survive as a live tag/attribute — only as
    # HTML-escaped inert text (e.g. "<script>" -> "&lt;script&gt;"). Plain
    # text with no HTML metacharacters (e.g. "javascript:alert(1)") is
    # expected to survive verbatim since it's inert prose, never a URL/href.
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror=alert(1)>" not in html
    assert "</style><script>alert(1)</script>" not in html
    assert 'href="javascript:alert(1)"' not in html
    # SSTI probe: the expression must render as literal text, never evaluate
    if payload == "{{7*7}}":
        assert "49" not in html
    # a bogus palette value can never reach the CSS custom properties
    assert DEFAULT_PALETTE["primary"] in html


@pytest.mark.parametrize("theme", THEMES)
def test_valid_content_renders_all_fields(theme):
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
    html = render(content, ADDR, SLUG, theme)

    assert "{{" not in html
    assert "Acme Supply" in html
    assert "Tools that ship" in html
    assert "Build faster today" in html
    assert "Widget Pro" in html
    assert ADDR in html
    assert SLUG in html
    assert "#111111" in html
    assert "#222222" in html
    assert "#333333" in html
    assert "#444444" in html
    assert "🚀" in html


@pytest.mark.parametrize("theme", THEMES)
def test_invalid_palette_falls_back_to_defaults(theme):
    content = {
        "store_name": "X",
        "palette": {
            "primary": "red; } body { display:none",
            "accent": "not-a-color",
            "bg": "#12",
            "text": "#zzzzzz",
        },
    }
    html = render(content, ADDR, SLUG, theme)
    assert "red; } body { display:none" not in html
    assert DEFAULT_PALETTE["primary"] in html
    assert DEFAULT_PALETTE["accent"] in html
    assert DEFAULT_PALETTE["bg"] in html
    assert DEFAULT_PALETTE["text"] in html


@pytest.mark.parametrize("theme", THEMES)
def test_slug_in_script_uses_tojson(theme):
    html = render({"store_name": "X"}, ADDR, "weird-slug", theme)
    assert 'const SLUG = "weird-slug";' in html
