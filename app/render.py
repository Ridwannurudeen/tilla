"""Autoescaped Jinja2 rendering for Tilla store themes.

LLM-generated brand copy is treated as data, never as a template: only the
theme files under themes/ are compiled as Jinja2 templates (SSTI ban).
HTML autoescaping protects the page body, but it does not protect the CSS
custom-property context the palette values render into, so palette colors
are separately validated against a strict hex pattern with a safe fallback.
"""

import re
from typing import Mapping

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import PUBLIC_BASE_URL, THEMES_DIR

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")

DEFAULT_PALETTE = {
    "primary": "#6C5CE7",
    "accent": "#00D1B2",
    "bg": "#0B0B12",
    "text": "#F5F5FA",
}

_env = Environment(
    loader=FileSystemLoader(str(THEMES_DIR)),
    autoescape=select_autoescape(["html"], default_for_string=True, default=True),
)


def _safe_hex(value: object, fallback: str) -> str:
    text = str(value) if value is not None else ""
    return text if _HEX_COLOR.fullmatch(text) else fallback


def _palette_ctx(content: Mapping) -> dict:
    """The four validated palette hex values (safe fallback on anything not a
    strict hex color), shared by the store themes and the OG image."""
    palette = content.get("palette") or {}
    if not isinstance(palette, Mapping):
        palette = {}
    return {
        "C_PRIMARY": _safe_hex(palette.get("primary"), DEFAULT_PALETTE["primary"]),
        "C_ACCENT": _safe_hex(palette.get("accent"), DEFAULT_PALETTE["accent"]),
        "C_BG": _safe_hex(palette.get("bg"), DEFAULT_PALETTE["bg"]),
        "C_TEXT": _safe_hex(palette.get("text"), DEFAULT_PALETTE["text"]),
    }


def _seo_ctx(content: Mapping, slug: str) -> dict:
    """Canonical URL, OG image path, meta description and a schema.org/Product
    JSON-LD object for the theme <head>. The JSON-LD is emitted with `| tojson`
    (which unicode-escapes `<`, `>`, `&`) so untrusted copy can't break out of
    the <script type="application/ld+json"> block."""
    base = PUBLIC_BASE_URL.rstrip("/")
    canonical = f"{base}/s/{slug}/"
    og_image = f"{base}/s/{slug}/og.svg"
    store_name = str(content.get("store_name", "My Store"))
    description = str(content.get("hero_subcopy") or content.get("tagline") or "")
    product_name = str(content.get("product_name", "")) or store_name
    jsonld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product_name,
        "description": str(content.get("product_blurb", "")),
        "image": og_image,
        "brand": {"@type": "Brand", "name": store_name},
        "offers": {
            "@type": "Offer",
            "price": str(content.get("price_usdt", 0)),
            "priceCurrency": "USDT",
            "url": canonical,
            "availability": "https://schema.org/InStock",
        },
    }
    return {
        "CANONICAL_URL": canonical,
        "OG_IMAGE": og_image,
        "META_DESCRIPTION": description,
        "PRODUCT_JSONLD": jsonld,
    }


def _store_ctx(content: Mapping, addr: str, slug: str) -> dict:
    """The full 15-token store-theme context (+ additive palette/SEO). Shared by
    :func:`render` and the M15.2 install-time :func:`render_source` check so a
    candidate theme is exercised with the exact context a live one receives."""
    return {
        "SLUG": slug,
        "STORE_NAME": str(content.get("store_name", "My Store")),
        "TAGLINE": str(content.get("tagline", "")),
        "HERO_HEADLINE": str(content.get("hero_headline", "")),
        "HERO_SUBCOPY": str(content.get("hero_subcopy", "")),
        "PRODUCT_NAME": str(content.get("product_name", "")),
        "PRODUCT_BLURB": str(content.get("product_blurb", "")),
        "CTA_TEXT": str(content.get("cta_text", "Buy now")),
        "PRICE": str(content.get("price_usdt", 0)),
        "EMOJI": str(content.get("emoji", "🛍️")),
        "ADDR": addr,
        **_palette_ctx(content),
        **_seo_ctx(content, slug),
    }


def render(content: Mapping, addr: str, slug: str, theme: str = "original.html") -> str:
    """Render a store theme from generator output. `content` is untrusted
    (LLM-produced); `addr`/`slug` are our own already-validated values."""
    template = _env.get_template(theme)
    return template.render(**_store_ctx(content, addr, slug))


def render_source(source: str, content: Mapping, addr: str, slug: str) -> str:
    """Render an UNTRUSTED candidate theme *source string* through the same
    autoescaped env (M15.2 install-time XSS-corpus gate) — `{% include %}` still
    resolves against the loader-owned ``themes/``, and string autoescape is on
    (``default_for_string=True``), so a candidate theme never renders under
    weaker escaping than a live one. Never used on the request path."""
    return _env.from_string(source).render(**_store_ctx(content, addr, slug))


def render_shell(template: str) -> str:
    """Render a data-free template (the M9 ``_dashboard.html`` shell) through the
    same autoescaped env as the store themes. The shell carries no merchant data —
    every store/order/refund string is fetched client-side and written via
    textContent — so this is purely to serve it from the one hardened env."""
    return _env.get_template(template).render()


def render_og(content: Mapping, slug: str) -> str:
    """Render the per-store Open Graph card (SVG, 1200x630). Served statically at
    /s/<slug>/og.svg and referenced from og:image / twitter:image. Autoescaped
    like the themes, so untrusted copy stays inert inside the SVG text nodes."""
    ctx = {
        "STORE_NAME": str(content.get("store_name", "My Store")),
        "TAGLINE": str(content.get("tagline", "")),
        "PRODUCT_NAME": str(content.get("product_name", "")),
        "PRICE": str(content.get("price_usdt", 0)),
        "EMOJI": str(content.get("emoji", "🛍️")),
        **_palette_ctx(content),
    }
    return _env.get_template("og.svg").render(**ctx)
