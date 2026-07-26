"""Autoescaped Jinja2 rendering for Tilla store themes.

LLM-generated brand copy is treated as data, never as a template: only the
theme files under themes/ are compiled as Jinja2 templates (SSTI ban).
HTML autoescaping protects the page body, but it does not protect the CSS
custom-property context the palette values render into, so palette colors
are separately validated against a strict hex pattern with a safe fallback.
"""

import re
from typing import Mapping

from jinja2 import FileSystemLoader, select_autoescape
from jinja2.sandbox import SandboxedEnvironment

from app import config, imagery, payment
from app.config import PUBLIC_BASE_URL, THEMES_DIR

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")

DEFAULT_PALETTE = {
    "primary": "#6C5CE7",
    "accent": "#00D1B2",
    "bg": "#0B0B12",
    "text": "#F5F5FA",
}

# SandboxedEnvironment (not plain Environment): a theme is third-party code once
# M15.2 lets an external author supply one, and autoescape only escapes OUTPUT —
# it does NOT stop template EXECUTION. The sandbox blocks the attribute-access
# gadget chains (``__init__.__globals__.__builtins__`` …) that would otherwise
# give an installed theme in-process RCE, keeping INV-1 (declarative-only) true.
_env = SandboxedEnvironment(
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
# Type pairing: the token is a bare keyword only. Every quoted font stack lives in
# the theme's own CSS, because autoescape would turn quotes in a templated stack
# into &#39; and break the declaration. "grotesk" is today's typography exactly, so
# it is the default and pre-axis stores are unaffected.
_DNA_TYPE = {
    "grotesk": "grotesk",
    "serif-display": "serif-display",
    "serif": "serif",
    "mono-display": "mono-display",
}


def _safe_enum(value: object, mapping: Mapping, fallback: str) -> str:
    """Map a whitelisted Design DNA enum value to its token value, falling back
    on anything else (wrong type included) — the enum analogue of _safe_hex."""
    return mapping[value] if isinstance(value, str) and value in mapping else fallback


# Owned by :mod:`app.imagery`, which writes these files. Aliased rather than
# re-declared so the renderer, the machine feeds and the image route can never drift
# apart on what a valid photograph path looks like.
_IMAGE_PATH = imagery.IMAGE_PATH


def _safe_url(value: object) -> str:
    """An absolute https URL, or "". Photo credit links are third-party strings that
    land in an ``href``, and autoescaping does NOT defuse a ``javascript:`` URL —
    escaping protects the attribute's delimiters, not its scheme. So the scheme is
    checked here rather than trusted, the same fail-closed contract as _safe_hex."""
    if not isinstance(value, str) or len(value) > 300:
        return ""
    return value if value.startswith("https://") else ""


def _safe_image(raw: object) -> dict | None:
    """Validate one persisted photograph before it can reach a template, or return
    None so the theme falls back to its generative texture.

    Every field is re-checked here even though :mod:`app.imagery` computed it: a
    store's ``content`` is persisted JSON that can also arrive from an imported
    ``store.json``, so the renderer treats it as untrusted input rather than
    assuming its own writer produced it. ``path`` in particular is matched against
    an exact digest shape — no traversal, no absolute URL, no other extension.
    """
    if not isinstance(raw, Mapping):
        return None
    path = raw.get("path")
    if not isinstance(path, str) or not _IMAGE_PATH.fullmatch(path):
        return None

    def _dim(value: object, fallback: int) -> int:
        return value if isinstance(value, int) and 0 < value <= 8000 else fallback

    return {
        "src": path,
        "alt": str(raw.get("alt") or "")[:200],
        "width": _dim(raw.get("width"), 1200),
        "height": _dim(raw.get("height"), 627),
        "credit": str(raw.get("credit") or "")[:120],
        "credit_url": _safe_url(raw.get("credit_url")),
        "source_url": _safe_url(raw.get("source_url")),
    }


def _imagery_ctx(content: Mapping) -> dict:
    """The store-level photography tokens: the hero image, the lifestyle band, and
    the photographer credits the provider's licence requires. Each is empty for a
    store with no photography — every theme guards on that and renders exactly as it
    did before photography existed."""
    raw = content.get("imagery")
    if not isinstance(raw, Mapping):
        raw = {}
    # Hard-capped at imagery.MAX_LIFESTYLE, not merely trusted to be within it: the
    # editorial theme letters these plates (PL. 01.A/B/C) by index, so a longer list
    # arriving from a hand-edited or imported store.json would raise mid-render and
    # break the page. The bound belongs on this side of the template.
    lifestyle = [
        image
        for image in (_safe_image(item) for item in (raw.get("lifestyle") or []))
        if image is not None
    ][: imagery.MAX_LIFESTYLE]
    credits = [
        {
            "credit": str(item.get("credit") or "")[:120],
            "credit_url": _safe_url(item.get("credit_url")),
            "source_url": _safe_url(item.get("source_url")),
        }
        for item in imagery.credits(content)
        if item.get("credit")
    ]
    return {
        "HERO_IMAGE": _safe_image(raw.get("hero")),
        "LIFESTYLE": lifestyle,
        "PHOTO_CREDITS": credits,
    }


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
        "DNA_TYPE": _safe_enum(dna.get("type_pair"), _DNA_TYPE, _DNA_TYPE["grotesk"]),
    }


def _seo_ctx(content: Mapping, slug: str, base_url: str | None = None) -> dict:
    """Canonical URL, OG image path, meta description and a schema.org/Product
    JSON-LD object for the theme <head>. The JSON-LD is emitted with `| tojson`
    (which unicode-escapes `<`, `>`, `&`) so untrusted copy can't break out of
    the <script type="application/ld+json"> block.

    ``base_url`` overrides the platform base for a Phase 4 custom-domain render: the
    store is served at the domain root, so canonical/OG resolve to ``https://<domain>/``
    (and its ``og.png``), never the ``/s/<slug>/`` platform path."""
    if base_url is not None:
        base = base_url.rstrip("/")
        canonical = f"{base}/"
        og_image = f"{base}/og.png"
    else:
        base = PUBLIC_BASE_URL.rstrip("/")
        canonical = f"{base}/s/{slug}/"
        og_image = f"{base}/s/{slug}/og.png"
    store_name = str(content.get("store_name", "My Store"))
    description = str(content.get("hero_subcopy") or content.get("tagline") or "")
    product_name = str(content.get("product_name", "")) or store_name
    # Prefer the primary product's real photograph over the Open Graph card. The OG
    # card is a typographic brand tile — correct for a social unfurl, wrong as the
    # schema.org/Product image, which search and agent shopping surfaces read as a
    # picture OF THE PRODUCT. Falls back to the card when there is no photograph.
    products = content.get("products")
    primary_image = None
    if isinstance(products, list) and products and isinstance(products[0], Mapping):
        primary_image = _safe_image(products[0].get("image"))
    jsonld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product_name,
        "description": str(content.get("product_blurb", "")),
        "image": f"{canonical}{primary_image['src']}" if primary_image else og_image,
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


def _products_ctx(content: Mapping) -> dict:
    """The store's product catalog as display-safe dicts for the theme's product
    loop. Old single-product content (only the scalar product_* fields, no
    `products` list) coerces to a one-item catalog, so pre-multi-product stores
    render unchanged. Each item carries its 0-based ``index``, which the buy button
    hands to checkout to select the Nth active product (by id) — the catalog is
    rendered in the same order the Product rows were created."""
    raw = content.get("products")
    if not isinstance(raw, list) or not raw:
        raw = [
            {
                "name": content.get("product_name", ""),
                "blurb": content.get("product_blurb", ""),
                "price_usdt": content.get("price_usdt", 0),
                "cta_text": content.get("cta_text", "Buy now"),
            }
        ]
    products = [
        {
            "index": i,
            # The stable DB product id (populated by create_store/resync); the buy
            # button prefers it so a deactivate/reorder can't shift what's bought.
            # Empty when content predates it (pre-backfill) — the button falls back
            # to the index, which checkout still accepts.
            "id": str(item["id"]) if isinstance(item.get("id"), int) else "",
            "name": str(item.get("name", "")),
            "blurb": str(item.get("blurb", "")),
            "price": str(item.get("price_usdt", 0)),
            "cta": str(item.get("cta_text", "Buy now")),
            # The product's own photograph, or None. None is the normal case for
            # anything with no honest photograph (software, a template, a service),
            # and every theme falls back to its generative plate on None rather
            # than showing a stand-in.
            "image": _safe_image(item.get("image")),
        }
        for i, item in enumerate(raw)
        if isinstance(item, Mapping)
    ]
    if not products:  # every item malformed — fall back to the scalar primary
        products = [
            {
                "index": 0,
                "id": "",
                "name": str(content.get("product_name", "")),
                "blurb": str(content.get("product_blurb", "")),
                "price": str(content.get("price_usdt", 0)),
                "cta": str(content.get("cta_text", "Buy now")),
                "image": None,
            }
        ]
    return {"PRODUCTS": products}


def _store_ctx(
    content: Mapping, addr: str, slug: str, base_url: str | None = None
) -> dict:
    """The full 15-token store-theme context (+ additive palette/SEO). Shared by
    :func:`render` and the M15.2 install-time :func:`render_source` check so a
    candidate theme is exercised with the exact context a live one receives.
    ``base_url`` (Phase 4 custom domains) re-points canonical/OG at the domain root."""
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
        # 18.3 checkout chain honesty: the canonical settlement chain the order is
        # pinned to (v1 mints every order on X Layer 196), named explicitly on the
        # page, plus the optional operator-configured bridge link (empty => absent).
        "CHAIN_NAME": "X Layer",
        "CHAIN_ID": payment.CANONICAL_CHAIN.chain_id,
        "BRIDGE_URL": config.BRIDGE_URL,
        **_palette_ctx(content),
        **_dna_ctx(content),
        **_products_ctx(content),
        **_imagery_ctx(content),
        **_seo_ctx(content, slug, base_url),
    }


def render(
    content: Mapping,
    addr: str,
    slug: str,
    theme: str = "original.html",
    base_url: str | None = None,
) -> str:
    """Render a store theme from generator output. `content` is untrusted
    (LLM-produced); `addr`/`slug` are our own already-validated values. ``base_url``
    (Phase 4 custom domains) re-points canonical/OG at ``https://<domain>/``."""
    template = _env.get_template(theme)
    return template.render(**_store_ctx(content, addr, slug, base_url))


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


def render_profile(address: str, stores: list) -> str:
    """Render the PUBLIC link-in-bio merchant profile — a shareable list of a
    merchant's live, public stores. ``stores`` is a list of ``{slug, name,
    description, url}`` dicts whose name/description are screened LLM copy, rendered
    through the same autoescaped env as the store themes so they stay inert text
    (an SSTI/XSS payload in a store name renders as literal characters, never
    markup or a template expression). ``address`` is a pre-validated, lowercased
    EVM address — already public (every store's on-chain receive wallet)."""
    return _env.get_template("_profile.html").render(
        ADDRESS=address,
        ADDRESS_SHORT=f"{address[:6]}…{address[-4:]}",
        BASE_URL=PUBLIC_BASE_URL.rstrip("/"),
        STORES=stores,
        STORE_COUNT=len(stores),
    )


def render_og(content: Mapping, slug: str) -> str:
    """Render the per-store Open Graph card (SVG, 1200x630). Served statically at
    /s/<slug>/og.svg and referenced from og:image / twitter:image. Autoescaped
    like the themes, so untrusted copy stays inert inside the SVG text nodes."""
    store_name = str(content.get("store_name", "My Store"))
    # Monogram for the OG card: rsvg-convert (the og.png rasterizer) has no
    # colour-emoji font, so an arbitrary merchant emoji can rasterise to tofu.
    # The first alphanumeric letter of the brand always renders (DejaVu), so the
    # card's identity mark is never broken.
    store_initial = (next((c for c in store_name if c.isalnum()), "").upper() or "◆")[
        :1
    ]
    ctx = {
        "STORE_NAME": store_name,
        "STORE_INITIAL": store_initial,
        "TAGLINE": str(content.get("tagline", "")),
        "PRODUCT_NAME": str(content.get("product_name", "")),
        "PRICE": str(content.get("price_usdt", 0)),
        "EMOJI": str(content.get("emoji", "🛍️")),
        **_palette_ctx(content),
    }
    return _env.get_template("og.svg").render(**ctx)
