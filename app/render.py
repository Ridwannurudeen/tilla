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

from app.config import THEMES_DIR

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


def render(content: Mapping, addr: str, slug: str, theme: str = "original.html") -> str:
    """Render a store theme from generator output. `content` is untrusted
    (LLM-produced); `addr`/`slug` are our own already-validated values."""
    palette = content.get("palette") or {}
    if not isinstance(palette, Mapping):
        palette = {}
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
        "C_PRIMARY": _safe_hex(palette.get("primary"), DEFAULT_PALETTE["primary"]),
        "C_ACCENT": _safe_hex(palette.get("accent"), DEFAULT_PALETTE["accent"]),
        "C_BG": _safe_hex(palette.get("bg"), DEFAULT_PALETTE["bg"]),
        "C_TEXT": _safe_hex(palette.get("text"), DEFAULT_PALETTE["text"]),
    }
    template = _env.get_template(theme)
    return template.render(**ctx)
