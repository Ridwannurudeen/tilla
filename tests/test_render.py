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


@pytest.mark.parametrize("theme", THEMES)
def test_checkout_partial_included_with_ids_and_constants(theme):
    # The shared M5 partial is included in every theme via {% include %}; its
    # markup ids + hardcoded JS constants must be present in the rendered page.
    html = render({"store_name": "X"}, ADDR, SLUG, theme)
    for el_id in (
        "co",
        "coAmount",
        "coAddr",
        "coStatus",
        "coDelivery",
        "payWalletBtn",
        "coQR",
        "coCountdown",
        "coReceipt",
        "claimBtn",
        "coClaim",
    ):
        assert f'id="{el_id}"' in html, el_id
    # USDT0 contract, X Layer chain 0xc4, and the ERC-20 transfer selector
    assert "0x779ded0c9e1022225f8e0630b35a9b54be713736" in html
    assert '"0xc4"' in html
    assert "0xa9059cbb" in html
    # SLUG still enters the script through tojson (autoescape discipline preserved)
    assert f'const SLUG = "{SLUG}";' in html


@pytest.mark.parametrize("theme", THEMES)
def test_checkout_retry_resets_state_before_poll(theme):
    # Regression: startCheckout() must clear the prior order's terminal flags on
    # an expire-then-rebuy. Without the reset, CO.expired stays true from the
    # first order and poll() bails at `if (CO.paid || CO.expired) return`, so the
    # retry shows a live-looking panel that never polls payment status.
    html = render({"store_name": "X"}, ADDR, SLUG, theme)
    reset = "CO.paid = false; CO.expired = false; CO.txHash = null;"
    assert reset in html
    # the reset must run before startCheckout()'s poll() call, not after
    assert html.index(reset) < html.index("poll();")
