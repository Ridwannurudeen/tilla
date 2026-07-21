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


def _relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of an already-validated hex color
    (https://www.w3.org/TR/WCAG21/#dfn-relative-luminance). Short #RGB/#RGBA
    forms are expanded; alpha digits are ignored."""
    digits = hex_color[1:]
    if len(digits) in (3, 4):
        digits = "".join(d * 2 for d in digits)
    srgb = [int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _on_primary(primary: str) -> str:
    """Pure "#000" or "#fff" — whichever has the higher WCAG contrast ratio
    against the validated primary color."""
    lum = _relative_luminance(primary)
    contrast_black = (lum + 0.05) / 0.05
    contrast_white = 1.05 / (lum + 0.05)
    return "#000" if contrast_black >= contrast_white else "#fff"


def _palette_ctx(content: Mapping) -> dict:
    """The four validated palette hex values (safe fallback on anything not a
    strict hex color), shared by the store themes and the OG image, plus the
    derived C_ON_PRIMARY (pure #000/#fff by WCAG contrast against C_PRIMARY)."""
    palette = content.get("palette") or {}
    if not isinstance(palette, Mapping):
        palette = {}
    return {
        "C_PRIMARY": _safe_hex(palette.get("primary"), DEFAULT_PALETTE["primary"]),
        "C_ACCENT": _safe_hex(palette.get("accent"), DEFAULT_PALETTE["accent"]),
        "C_BG": _safe_hex(palette.get("bg"), DEFAULT_PALETTE["bg"]),
        "C_TEXT": _safe_hex(palette.get("text"), DEFAULT_PALETTE["text"]),
        "C_ON_PRIMARY": _on_primary(
            _safe_hex(palette.get("primary"), DEFAULT_PALETTE["primary"])
        ),
    }


# Design DNA axis whitelists (docs/DESIGN-DNA.md): each enum value the LLM may
# pick maps onto a server-owned token value. Anything outside a whitelist falls
# back to the default, so a bogus value never reaches a style context — the
# same fail-closed contract as _safe_hex. The defaults (balanced / regular /
# roomy / stacked / medium) reproduce the pre-DNA look exactly.
_DNA_SCALE = {
    "compact": "1.18",
    "balanced": "1.25",
    "dramatic": "1.34",
    "monumental": "1.5",
}
_DNA_WEIGHT = {"light": "300", "regular": "450", "heavy": "700"}
_DNA_SPACE = {"tight": "0.82", "roomy": "1", "airy": "1.35"}
_DNA_HERO = {"stacked": "stacked", "split": "split", "offset": "offset"}
_DNA_TEXTURE = {"sparse": "sparse", "medium": "medium", "dense": "dense"}


def _safe_enum(value: object, mapping: Mapping, fallback: str) -> str:
    """Map a whitelisted Design DNA enum value to its token value, falling back
    on anything else (wrong type included) — the enum analogue of _safe_hex."""
    return mapping[value] if isinstance(value, str) and value in mapping else fallback


def _dna_ctx(content: Mapping) -> dict:
    """The five validated Design DNA tokens (docs/DESIGN-DNA.md). A store whose
    content has no design_dna — or a partial/invalid one — gets the defaults on
    every missing/bogus axis, so pre-DNA stores render with the current look."""
    dna = content.get("design_dna") or {}
    if not isinstance(dna, Mapping):
        dna = {}
    return {
        "DNA_SCALE": _safe_enum(dna.get("scale"), _DNA_SCALE, _DNA_SCALE["balanced"]),
        "DNA_WEIGHT": _safe_enum(
            dna.get("weight"), _DNA_WEIGHT, _DNA_WEIGHT["regular"]
        ),
        "DNA_SPACE": _safe_enum(dna.get("rhythm"), _DNA_SPACE, _DNA_SPACE["roomy"]),
        "DNA_HERO": _safe_enum(dna.get("hero"), _DNA_HERO, _DNA_HERO["stacked"]),
        "DNA_TEXTURE": _safe_enum(
            dna.get("texture"), _DNA_TEXTURE, _DNA_TEXTURE["medium"]
        ),
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


def render(content: Mapping, addr: str, slug: str, theme: str = "original.html") -> str:
    """Render a store theme from generator output. `content` is untrusted
    (LLM-produced); `addr`/`slug` are our own already-validated values."""
    ctx = {
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
        **_dna_ctx(content),
        **_seo_ctx(content, slug),
    }
    template = _env.get_template(theme)
    return template.render(**ctx)


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
