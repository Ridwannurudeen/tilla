#!/usr/bin/env python3
"""Tilla store engine: prompt -> generated brand+content -> premium live store.
Usage: python3 -m app.engine "I sell a Notion productivity template for $9" [receive_address]
Env: TILLA_LLM_KEY (Anthropic), TILLA_LLM_MODEL (optional), TILLA_STORES_DIR (default /opt/tilla/stores),
     TILLA_IMAGE_KEY (optional Pexels key; absent = stores generate with no photography)
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Mapping
from typing import Literal, get_args

import requests
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import config, imagery, palette, providers, screening
from app.db import SessionLocal
from app.delivery import mint_manage_key

# StoreImagery is imported by NAME as well as the module above: the
# GeneratedContent field is itself called `imagery`, so an annotation written as
# `imagery.StoreImagery` would resolve against the field once the class body binds
# that name.
from app.imagery import StoreImagery
from app.models import (
    Product,
    ScreeningReceipt,
    Store,
    get_or_create_merchant,
    log_event,
)
from app.render import render as render_theme
from app.render import render_og

logger = logging.getLogger("tilla")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
KEY = os.environ.get("TILLA_LLM_KEY", "")
MODEL = os.environ.get("TILLA_LLM_MODEL") or "claude-haiku-4-5"
STORES_DIR = config.STORES_DIR
DEFAULT_ADDR = "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"  # demo receive address
# floor for a live store; a non-positive price would auto-confirm checkout
MIN_PRICE_USDT = 1.0

# Photography runs in a background thread after the store is live, because the
# inline pipeline (provider searches + a vision check per image, sometimes
# generation) takes 30-90s and OKX's review client hangs up at ~30s — measured
# live 2026-07-27: their paid create-store attempts logged nginx 499 (client
# closed) at ~30s intervals while a photography-light create completed 200 in
# 15s. The store is fully live on its generative texture the moment the call
# returns — photography has been a progressive enhancement since the backfill —
# and the thread re-renders the page when the photos land. Tests that assert
# photos synchronously flip this off.
IMAGERY_ASYNC = True

# Bound on the generation-only atmosphere prompt (GeneratedContent.hero_art_prompt).
# Generous enough to hold one full sentence, because this text is handed straight to
# an image generator and a half-sentence draws a half-scene.
ART_PROMPT_MAX = 200


def clip_words(text: object, limit: int) -> str:
    """`text` truncated to at most `limit` characters, never mid-word.

    Shared by the create path (the GeneratedContent validator) and the backfill
    script deliberately: two copies of "shorten a prompt" would drift, and the
    whole point is that a store photographed by either route gets the same string.
    """
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    head, sep, _ = cut.rpartition(" ")
    return (head if sep else cut).rstrip(" ,;:-")


# One in-process retry on a transient Anthropic failure, spaced by this pause.
LLM_RETRY_SLEEP_SEC = 2.0


class GenerationUnavailable(RuntimeError):
    """The LLM generation step could not complete: an Anthropic outage (connect /
    timeout error, HTTP 429/5xx/529 after one retry), a non-transient 4xx (bad key),
    or malformed output. Callers (``_run_create_store`` / ``_run_upgrade_store``) map
    it to HTTP 503 + Retry-After, which is >= 400 so the x402 middleware skips
    settlement — a paid create-store during an outage moves ZERO funds."""


def _is_transient_status(code: int) -> bool:
    """HTTP statuses worth one retry: rate limiting and any server-side 5xx
    (429, 500-599 — covers Anthropic's 529 overloaded)."""
    return code == 429 or 500 <= code <= 599


# ------------------------------------------------------------------ store seed
# The slug is a store's one source of visual identity. The two functions below are
# the SAME constructions the mosaic already runs client-side in themes/*.html
# (FNV-1a then mulberry32), reimplemented here so the server can draw from the
# identical seed — one slug, one coherent identity across layout and texture.
def _fnv1a(text: str) -> int:
    """32-bit FNV-1a, matching the `fnv()` in the theme mosaic exactly."""
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _mulberry32(seed: int):
    """mulberry32 PRNG, matching the theme mosaic's generator exactly. Returns a
    callable yielding floats in [0, 1). Deterministic: one slug always replays the
    same sequence, so a re-render never changes a store's look."""
    state = seed & 0xFFFFFFFF

    def nxt() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state
        t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
        t = (((t + (((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF) ^ t) & (
            0xFFFFFFFF
        )
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 0x100000000

    return nxt


# ------------------------------------------------------------- design personas
# WHY personas and not five independent rolls: every axis value is individually
# safe (each maps to CSS the theme already owns), but independence is not the same
# as taste — `monumental` scale with `tight` rhythm and `dense` texture is valid
# CSS and a cramped mess. Each persona below is a coherent bundle someone would
# actually design, so a seeded pick is always intentional-looking. Variety then
# comes from WHICH persona, plus a seeded jitter inside it.
#
# This exists because the LLM, asked to choose the five axes freely, collapsed to
# roughly three answers across the whole live catalogue: `hero` was `stacked` on
# every editorial and original store and `offset` on every bold one, and `scale`
# was `balanced` on ten of fourteen. A model asked for free choice returns its
# modal answer. The seed does not have a modal answer.
_PERSONAS: dict[str, dict] = {
    "quiet-luxury": {
        "theme": "editorial",
        "scale": "compact",
        "weight": "light",
        "rhythm": "airy",
        "hero": "stacked",
        "texture": "sparse",
        "type_pair": "serif-display",
    },
    "editorial-classic": {
        "theme": "editorial",
        "scale": "balanced",
        "weight": "light",
        "rhythm": "roomy",
        "hero": "split",
        "texture": "sparse",
        "type_pair": "serif",
    },
    "gallery": {
        "theme": "editorial",
        "scale": "monumental",
        "weight": "light",
        "rhythm": "airy",
        "hero": "split",
        "texture": "sparse",
        "type_pair": "serif-display",
    },
    "bold-statement": {
        "theme": "bold",
        "scale": "dramatic",
        "weight": "heavy",
        "rhythm": "tight",
        "hero": "offset",
        "texture": "dense",
        "type_pair": "grotesk",
    },
    "poster": {
        "theme": "bold",
        "scale": "dramatic",
        "weight": "heavy",
        "rhythm": "roomy",
        "hero": "split",
        "texture": "dense",
        "type_pair": "grotesk",
    },
    "monument": {
        "theme": "bold",
        "scale": "monumental",
        "weight": "heavy",
        "rhythm": "airy",
        "hero": "stacked",
        "texture": "medium",
        "type_pair": "grotesk",
    },
    "zine": {
        "theme": "bold",
        "scale": "dramatic",
        "weight": "regular",
        "rhythm": "tight",
        "hero": "offset",
        "texture": "dense",
        "type_pair": "mono-display",
    },
    "technical": {
        "theme": "original",
        "scale": "compact",
        "weight": "regular",
        "rhythm": "tight",
        "hero": "split",
        "texture": "medium",
        "type_pair": "mono-display",
    },
    "warm-craft": {
        "theme": "original",
        "scale": "balanced",
        "weight": "regular",
        "rhythm": "roomy",
        "hero": "offset",
        "texture": "medium",
        "type_pair": "serif-display",
    },
    "minimal-shop": {
        "theme": "original",
        "scale": "compact",
        "weight": "light",
        "rhythm": "roomy",
        "hero": "stacked",
        "texture": "sparse",
        "type_pair": "grotesk",
    },
}
_PERSONA_NAMES = tuple(sorted(_PERSONAS))
# Axes the seed is allowed to nudge, with the neighbours that stay coherent for
# any persona. Two stores that land on the same persona still differ here.
_JITTER = {
    "texture": ("sparse", "medium", "dense"),
    "weight": ("light", "regular", "heavy"),
}
_DNA_AXES = ("scale", "weight", "rhythm", "hero", "texture", "type_pair")


def resolve_design(slug: str, content: Mapping) -> tuple[dict, str, str]:
    """Pick a store's visual identity: ``(design_dna, persona_name, theme_name)``.

    Deterministic in ``slug``, so the same store always resolves the same way and a
    re-render can never restyle a live storefront. The LLM may name a persona (a
    single judgement it makes far better than five independent axis picks); an
    absent or unrecognised name falls back to a seeded pick. Either way the seed
    then jitters two axes, so two stores sharing a persona are still distinct.

    Returns the five concrete axes in exactly the shape ``render._dna_ctx`` already
    consumes, so no template or renderer has to know personas exist."""
    rnd = _mulberry32(_fnv1a(slug))
    named = content.get("design_persona")
    if not isinstance(named, str) or named not in _PERSONAS:
        named = _PERSONA_NAMES[int(rnd() * len(_PERSONA_NAMES)) % len(_PERSONA_NAMES)]
    else:
        rnd()  # keep the sequence aligned whether or not the LLM named one
    persona = _PERSONAS[named]
    dna = {axis: persona[axis] for axis in _DNA_AXES}
    for axis, options in _JITTER.items():
        dna[axis] = options[int(rnd() * len(options)) % len(options)]
    return dna, named, persona["theme"]


def resolve_palette(slug: str, content: Mapping) -> dict:
    """The store's derived palette (see :mod:`app.palette`).

    Seeded from a SALTED slug hash so the colour draws are independent of the
    layout draws — seeding both from the bare slug would correlate persona with
    palette, and every `zine` store would come out the same colour."""
    return palette.resolve(content, _mulberry32(_fnv1a(slug + ":palette")))


class DesignDNA(BaseModel):
    """The five Design DNA style axes (docs/DESIGN-DNA.md): server-validated
    enums the LLM picks to express a brand's personality. An out-of-set value
    is coerced to the axis default — never a ValidationError that could block a
    sale — mirroring the fail-closed contract of ``_safe_hex`` / ``theme``."""

    scale: Literal["compact", "balanced", "dramatic", "monumental"] = "balanced"
    weight: Literal["light", "regular", "heavy"] = "regular"
    rhythm: Literal["tight", "roomy", "airy"] = "roomy"
    hero: Literal["stacked", "split", "offset"] = "stacked"
    texture: Literal["sparse", "medium", "dense"] = "medium"
    # Typography pairing. "grotesk" is today's stack on every theme, so it is the
    # default and any store predating this axis renders unchanged.
    type_pair: Literal["grotesk", "serif-display", "serif", "mono-display"] = "grotesk"

    @field_validator("*", mode="before")
    @classmethod
    def _coerce_axis(cls, v, info):
        field = cls.model_fields[info.field_name]
        return v if v in get_args(field.annotation) else field.default


class BrandBrief(BaseModel):
    """What the LLM is allowed to say about colour: a hue and two named
    relationships. It never supplies hex values any more — :mod:`app.palette`
    derives them, so contrast floors hold for every store rather than for the
    lucky ones. Every field is fail-closed: a bogus value is dropped to None and
    the seed decides that dimension instead of the request failing."""

    hue: float | None = None
    harmony: Literal["mono", "analogous", "complementary", "triadic"] | None = None
    mood: Literal["midnight", "ink", "paper", "bone"] | None = None

    @field_validator("hue", mode="before")
    @classmethod
    def _coerce_hue(cls, v):
        try:
            return float(v) % 360
        except (TypeError, ValueError):
            return None

    @field_validator("harmony", "mood", mode="before")
    @classmethod
    def _coerce_named(cls, v, info):
        allowed = get_args(get_args(cls.model_fields[info.field_name].annotation)[0])
        return v if v in allowed else None


class ProductContent(BaseModel):
    """One product in a store's catalog (docs multi-product). price_usdt allows 0
    here — generate() clamps any non-positive price to MIN_PRICE_USDT before it can
    reach a Product row (whose CHECK enforces price_micro > 0)."""

    name: str = Field(default="", max_length=120)
    blurb: str = Field(default="", max_length=400)
    price_usdt: float = Field(default=0, ge=0, le=10000)
    cta_text: str = Field(default="Buy now", max_length=40)
    # Photography search text, and ONLY search text. The model never supplies a
    # path or a URL — :mod:`app.imagery` resolves those server-side — so no model
    # output can reach an <img src>. `image_subject` is the accuracy mechanism: the
    # concrete nouns a candidate photo's own description must contain before it is
    # allowed to represent this product (see app/imagery.py::_score).
    image_query: str = Field(default="", max_length=120)
    image_subject: str = Field(default="", max_length=120)


class GeneratedContent(BaseModel):
    """Bounds on what the LLM is allowed to hand back before it ever reaches
    a template or a price tag."""

    store_name: str = Field(default="My Store", max_length=80)
    tagline: str = Field(default="", max_length=120)
    hero_headline: str = Field(default="", max_length=160)
    hero_subcopy: str = Field(default="", max_length=280)
    product_name: str = Field(default="", max_length=120)
    product_blurb: str = Field(default="", max_length=400)
    cta_text: str = Field(default="Buy now", max_length=40)
    price_usdt: float = Field(default=0, ge=0.01, le=10000)
    # The store catalog. The LLM may return several products; an old-style output
    # with only the scalar product_* fields is coerced to a one-item list by
    # _ensure_products, and products[0] is always mirrored back onto the scalar
    # fields so every existing single-product consumer (store.json, render
    # fallback, screening) keeps working unchanged.
    products: list[ProductContent] = Field(default_factory=list, max_length=8)
    emoji: str = Field(default="🛍️", max_length=8)
    palette: dict = Field(default_factory=dict)
    # The LLM's theme suggestion (used only when the caller didn't pick one). A
    # value outside the allowed set is coerced to the default so a stray
    # suggestion can never fail generation.
    theme: str = Field(default="original")
    # Optional style-axes pick (docs/DESIGN-DNA.md); stores/content without it
    # render exactly as before (render._dna_ctx falls back to the defaults).
    # NOTE: for a NEW store this is overwritten by resolve_design() from the slug
    # seed. It stays a field because existing stored content carries it and the
    # renderer reads it, so it must survive a validate/dump round trip.
    design_dna: DesignDNA | None = None
    # The colour brief and the named personality — the two things the model judges
    # better than a PRNG. Both must be DECLARED here or pydantic drops them
    # silently and the seed would quietly decide everything.
    brand: BrandBrief | None = None
    design_persona: str | None = None
    # Photography. The three *_quer*/subject fields are search text FROM the model;
    # `imagery` is the resolved result and is SERVER-OWNED — create_store overwrites
    # it wholesale from app.imagery.resolve(), exactly as it overwrites design_dna
    # from resolve_design(). It is declared here so it survives the validate/dump
    # round trip and so a re-render of a live store keeps its photographs.
    hero_image_query: str = Field(default="", max_length=120)
    hero_image_subject: str = Field(default="", max_length=120)
    lifestyle_queries: list[str] = Field(default_factory=list, max_length=3)
    # Generation-only atmosphere for the hero of a store selling something with no
    # physical form. Deliberately NOT a search query: it is never sent to the stock
    # provider, because a real photograph of a real desk asserts something about
    # goods that do not exist (see app.imagery._generated_hero).
    hero_art_prompt: str = Field(default="", max_length=ART_PROMPT_MAX)
    imagery: StoreImagery | None = None

    @field_validator("hero_art_prompt", mode="before")
    @classmethod
    def _clip_art_prompt(cls, v):
        """Truncate on a WORD boundary rather than reject or chop mid-word. This
        string is handed verbatim to an image generator, and a first pass over the
        live digital stores cut every prompt mid-word ('…suggesting financ',
        '…notebooks and war') — the generator is then drawing from a fragment."""
        return clip_words(v, ART_PROMPT_MAX) if isinstance(v, str) else ""

    @field_validator("lifestyle_queries", mode="before")
    @classmethod
    def _coerce_lifestyle(cls, v):
        """Keep only strings, capped — a stray shape here must not fail a paid
        create-store, and imagery is cosmetic."""
        if not isinstance(v, list):
            return []
        return [s[:120] for s in v if isinstance(s, str) and s.strip()][:3]

    @field_validator("imagery", mode="before")
    @classmethod
    def _coerce_imagery(cls, v):
        """Drop a malformed imagery block rather than failing generation. A model
        that invents this key (it is never asked for) is ignored here, and
        create_store overwrites the field regardless."""
        return v if isinstance(v, dict) else None

    @field_validator("design_persona", mode="before")
    @classmethod
    def _coerce_persona(cls, v):
        """An unrecognised persona name becomes None so the slug seed picks one —
        never a ValidationError that would fail a paid create-store."""
        return v if isinstance(v, str) and v in _PERSONAS else None

    @field_validator("theme")
    @classmethod
    def _coerce_theme(cls, v):
        return v if v in config.ALLOWED_THEMES else "original"

    @field_validator("design_dna", mode="before")
    @classmethod
    def _coerce_design_dna(cls, v):
        # A stray non-object design_dna (string/list/number) is dropped rather
        # than failing generation; the renderer then uses the default look.
        return v if isinstance(v, dict) else None

    @model_validator(mode="after")
    def _ensure_products(self):
        """Reconcile the catalog with the legacy scalar fields, both ways: an
        old-style output with no products[] synthesizes a one-item catalog from
        the scalar product_* fields; then products[0] is mirrored back onto the
        scalar fields so every single-product consumer keeps working."""
        if not self.products:
            self.products = [
                ProductContent(
                    name=self.product_name,
                    blurb=self.product_blurb,
                    price_usdt=self.price_usdt,
                    cta_text=self.cta_text,
                )
            ]
        primary = self.products[0]
        self.product_name = primary.name
        self.product_blurb = primary.blurb
        self.cta_text = primary.cta_text
        self.price_usdt = primary.price_usdt
        return self


def _resolve_theme(name: str | None) -> str:
    """Map a short theme name (API- or LLM-supplied) to its template filename,
    falling back to the default for anything outside the allowed set (built-ins
    ∪ active theme plugins, per M15.2)."""
    return (
        f"{name}.html"
        if name in providers.allowed_theme_names()
        else config.DEFAULT_THEME
    )


_RSVG_BIN = shutil.which("rsvg-convert")


def _write_og_png(svg_path, png_path) -> None:
    """Best-effort rasterize og.svg -> og.png (1200x630) with rsvg-convert, because
    social scrapers (X / Discord / Telegram / Slack) do not render an SVG og:image.
    The PNG is cosmetic and this runs inside the paid create-store path, so ANY
    failure (binary absent on dev/Windows, rsvg error or timeout) is logged and
    swallowed — it must never break store creation or change fund flow. og.svg
    stays the source of truth; a missing og.png simply 404s until the next render."""
    if _RSVG_BIN is None:
        return
    try:
        subprocess.run(
            [_RSVG_BIN, "-w", "1200", "-h", "630", str(svg_path), "-o", str(png_path)],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        logger.exception("og.png rasterization failed for %s", png_path)


def resync_catalog(session, store, extras_override=None) -> None:
    """Rebuild ``store.content['products']`` from the store's ACTIVE Product rows
    (id order) and re-render the static pages, so the storefront catalog — and its
    index->product mapping — always reflects the live catalog after a dashboard
    edit. Display extras (blurb, cta_text) live only in content, not on the Product
    row, so they are preserved by product id; ``extras_override`` (``{product_id:
    (blurb, cta_text)}``) supplies them for a product just added or edited. A
    store whose content predates CRUD has no per-item ids yet — its content order
    equals create-time id order, so ids are backfilled positionally on first sync.
    Runs inside the caller's transaction (commits are the caller's job)."""
    from sqlalchemy.orm.attributes import flag_modified

    active = session.scalars(
        select(Product)
        .where(Product.store_id == store.id, Product.active.is_(True))
        .order_by(Product.id)
    ).all()
    content = dict(store.content or {})
    old = content.get("products") or []
    extras = {}
    # A product's photograph and the search text that found it are display extras
    # too, and they must follow the PRODUCT rather than its position: this function
    # rebuilds the list from the live rows, so deleting one product shifts every
    # later index. Carrying them by product id is what keeps a card's photo attached
    # to the item it actually depicts after a dashboard catalog edit — and keeps the
    # queries available so `upgrade_store` can re-resolve photography later.
    # Nothing here contacts the image provider: a catalog edit reuses the photos it
    # already has.
    carried: dict[object, dict] = {}
    for i, item in enumerate(old):
        if not isinstance(item, dict):
            continue
        key = item.get("id")
        if key is None and i < len(active):
            key = active[i].id
        extras[key] = (str(item.get("blurb", "")), str(item.get("cta_text", "Buy now")))
        passthrough = {}
        if isinstance(item.get("image"), dict):
            passthrough["image"] = item["image"]
        for field in ("image_query", "image_subject"):
            if isinstance(item.get(field), str) and item[field]:
                passthrough[field] = item[field]
        if passthrough:
            carried[key] = passthrough
    if extras_override:
        extras.update(extras_override)
    content["products"] = [
        {
            "id": p.id,
            "name": p.name,
            "price_usdt": p.price_micro / 1e6,
            "blurb": extras.get(p.id, ("", "Buy now"))[0],
            "cta_text": extras.get(p.id, ("", "Buy now"))[1],
            **carried.get(p.id, {}),
        }
        for p in active
    ]
    if content["products"]:
        primary = content["products"][0]
        content["product_name"] = primary["name"]
        content["product_blurb"] = primary["blurb"]
        content["cta_text"] = primary["cta_text"]
        content["price_usdt"] = primary["price_usdt"]
    store.content = content
    flag_modified(store, "content")
    d = STORES_DIR / store.slug
    d.mkdir(parents=True, exist_ok=True)
    _write_store_pages(d, content, store.pay_to, store.slug, store.theme)


def update_store_copy(session, store, updates: dict) -> None:
    """Merge plain-language copy edits (tagline / hero_subcopy) into a LIVE store's
    persisted ``content`` and re-render its static pages — WITHOUT an LLM
    regeneration. Only the caller-supplied, already-screened copy keys change; the
    catalog, palette, theme, and Design DNA are preserved untouched. Mirrors
    :func:`resync_catalog`'s persist+re-render contract; runs inside the caller's
    transaction (the commit is the caller's job)."""
    from sqlalchemy.orm.attributes import flag_modified

    content = dict(store.content or {})
    content.update(updates)
    store.content = content
    flag_modified(store, "content")
    d = STORES_DIR / store.slug
    d.mkdir(parents=True, exist_ok=True)
    _write_store_pages(d, content, store.pay_to, store.slug, store.theme)


def _write_store_pages(d, content: dict, addr: str, slug: str, theme: str) -> None:
    """Write the nginx-served static assets for a live store: index.html (the chosen
    theme), og.svg (the Open Graph card source), and og.png (its raster for social
    scrapers). The <head> references og.png as og:image/twitter:image."""
    (d / "index.html").write_text(
        render_theme(content, addr, slug, theme), encoding="utf-8"
    )
    (d / "og.svg").write_text(render_og(content, slug), encoding="utf-8")
    _write_og_png(d / "og.svg", d / "og.png")


def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return (s or "store")[: config.SLUG_MAX_LEN]


def _slug_taken(candidate: str) -> bool:
    """A slug is taken if a store dir exists on disk OR a DB row already holds
    it. The DB check catches blocked stores whose dir was removed but whose
    row keeps the slug (stores.slug is UNIQUE — a disk-only check would let a
    later collision IntegrityError out)."""
    if (STORES_DIR / candidate).exists():
        return True
    with SessionLocal() as session:
        return (
            session.scalar(select(Store.id).where(Store.slug == candidate)) is not None
        )


# Words too common to distinguish one brand from another, so never used as a
# collision suffix — "highland-roast-the" is no better than "highland-roast-2".
_SLUG_STOPWORDS = frozenset(
    """a an and are as at be by for from has have in is it its of on or our so that
    the their they this to was were will with you your we us he she him her""".split()
)
# Trade words, tried after the brand's own vocabulary is exhausted. Generic, but a
# storefront called "-studio" reads as a decision and "-2" reads as a duplicate.
_SLUG_TRADE_SUFFIXES = (
    "co",
    "studio",
    "shop",
    "works",
    "supply",
    "goods",
    "house",
    "atelier",
    "press",
    "lab",
)


def slug_words(content: Mapping) -> tuple[str, ...]:
    """Distinctive words from a store's own copy, best first, for use as a slug
    suffix. Drops stopwords, anything under three characters, and duplicates."""
    if not isinstance(content, Mapping):
        return ()
    parts: list[str] = []
    for key in ("tagline", "hero_headline", "product_name", "store_name"):
        value = content.get(key)
        if isinstance(value, str):
            parts.append(value)
    for product in content.get("products") or []:
        if isinstance(product, Mapping) and isinstance(product.get("name"), str):
            parts.append(product["name"])
    seen: list[str] = []
    for token in re.split(r"[^a-zA-Z0-9]+", " ".join(parts).lower()):
        if len(token) >= 3 and token not in _SLUG_STOPWORDS and token not in seen:
            seen.append(token)
    return tuple(seen)


def unique_slug(base: str, content: Mapping | None = None) -> str:
    """Resolve `base` to a slug that is neither a reserved app route nor an existing
    store (on disk or in the DB).

    On collision the suffix is drawn from the brand's OWN words first, then from a
    short list of trade words, and only then from a number. A numeric suffix is a
    tell: "highland-roast-2" announces that something generated it and that a
    near-identical store already exists. "highland-roast-goods" does not. The
    numeric tail is kept as the final fallback because it is the only one guaranteed
    to terminate.

    Every candidate is truncated to leave room for its suffix, so the result always
    matches SLUG_PATTERN and stays within SLUG_MAX_LEN (a longer slug 422s at
    checkout)."""
    slug = base if base not in config.RESERVED_SLUGS else f"{base}-store"
    slug = slug[: config.SLUG_MAX_LEN]
    if not _slug_taken(slug):
        assert re.fullmatch(config.SLUG_PATTERN, slug), slug
        return slug

    def with_suffix(suffix: str) -> str:
        room = config.SLUG_MAX_LEN - len(suffix) - 1
        return f"{slug[:room].rstrip('-')}-{suffix}"

    brand_words = tuple(w for w in slug_words(content or {}) if w not in slug)
    for suffix in brand_words + _SLUG_TRADE_SUFFIXES:
        candidate = with_suffix(suffix)
        if re.fullmatch(config.SLUG_PATTERN, candidate) and not _slug_taken(candidate):
            return candidate

    n = 2
    while True:
        candidate = with_suffix(str(n))
        if not _slug_taken(candidate):
            assert re.fullmatch(config.SLUG_PATTERN, candidate), candidate
            return candidate
        n += 1


def _post_generation(prompt: str) -> dict:
    """POST the generation prompt to Anthropic with exactly one retry on a transient
    outage (connect/timeout error, HTTP 429/5xx/529), spaced by LLM_RETRY_SLEEP_SEC.
    Returns the parsed 2xx response JSON. Raises GenerationUnavailable on a transient
    failure that survives the retry, or immediately on a non-transient error (a 4xx
    such as a bad key — retrying can't help). Never returns a non-2xx body."""
    headers = {
        "x-api-key": KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        # Raised from 1024 when photography search text was added to the schema:
        # four products now carry an image_query and an image_subject each, plus the
        # hero pair and up to three lifestyle queries. Measured output before that
        # change averaged 479 tokens and peaked at 558, so 1024 had real headroom —
        # but a truncated response is a failed paid create-store, and the extra
        # ceiling costs nothing unless it is used (output is billed per token).
        "model": MODEL,
        "max_tokens": 2048,
        # Same description SHOULD yield the same catalog. A customer bought twice
        # with identical input and got different product names, different prices
        # (5000 vs 2500) and a different product COUNT — reported 2026-07-27, see
        # docs/ISSUES.md #1. Greedy sampling removes most of that variance.
        #
        # NOT a determinism guarantee, and deliberately not described as one:
        # temperature 0 has never guaranteed byte-identical output. It is also
        # model-gated — accepted on the configured claude-haiku-4-5, but a 400 on
        # Opus 4.7+ / Sonnet 5 / Fable 5, and TILLA_LLM_MODEL is env-settable. The
        # request drops it and retries once if the model rejects it (below), so a
        # future model swap degrades to today's behaviour instead of failing every
        # paid create-store.
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    for attempt in range(2):  # initial try + at most one retry
        try:
            r = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=90)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 0:
                logger.warning("anthropic transient error, retrying once: %s", exc)
                time.sleep(LLM_RETRY_SLEEP_SEC)
                continue
            raise GenerationUnavailable(f"anthropic unreachable: {exc}") from exc
        except requests.RequestException as exc:
            raise GenerationUnavailable(f"anthropic request failed: {exc}") from exc
        if _is_transient_status(r.status_code):
            if attempt == 0:
                logger.warning("anthropic returned %d, retrying once", r.status_code)
                time.sleep(LLM_RETRY_SLEEP_SEC)
                continue
            raise GenerationUnavailable(f"anthropic returned {r.status_code}")
        # A model that refuses the sampling parameter (Opus 4.7+, Sonnet 5, Fable 5
        # all 400 on `temperature`) must not take the whole paid create-store down
        # with it: drop the field and retry once. Costs one wasted call the first
        # time TILLA_LLM_MODEL points at such a model, then behaves as it did
        # before determinism was added.
        if (
            r.status_code == 400
            and "temperature" in payload
            and "temperature" in r.text.lower()
        ):
            logger.warning("model %s rejected temperature; retrying without it", MODEL)
            payload.pop("temperature")
            continue
        try:
            r.raise_for_status()
        except requests.HTTPError as exc:
            logger.error("anthropic non-transient error %d", r.status_code)
            raise GenerationUnavailable(f"anthropic returned {r.status_code}") from exc
        return r.json()
    # Both attempts on a transient path raise above; this guards the contract.
    raise GenerationUnavailable("anthropic generation exhausted retries")


def generate(desc):
    prompt = (
        "You are a world-class brand designer and DTC copywriter. A solo entrepreneur wants to sell "
        "something. Turn their description into a polished storefront with a focused product catalog.\n\n"
        f'Merchant description: "{desc}"\n\n'
        "Output ONLY valid JSON (no markdown) with EXACTLY these keys: "
        "store_name (short brand), tagline (<=6 words), hero_headline (punchy, <=8 words), "
        "hero_subcopy (1 sentence), "
        "products (an array of 1 to 4 objects, each an object with: name, blurb (1-2 sentences, "
        "benefit-led), price_usdt (number), cta_text (<=4 words), image_query, image_subject) "
        "— a focused catalog of related "
        "items that fit the brand; use a single item when the merchant clearly sells one thing, "
        "otherwise 2 to 4 distinct items, "
        # PRICES ARE THE MERCHANT'S, NOT YOURS. A real store went live selling the
        # same service at 5000 one run and 2500 the next, from identical input,
        # because nothing here constrained the number (docs/ISSUES.md #1). An
        # invented price is a claim about someone's business, and a merchant who
        # does not read the generated catalog closely could publish it.
        "and for prices: if the merchant's description states a price, use that EXACT number for "
        "that item and do not adjust it. When no price is stated, choose a modest, conservative "
        "figure typical for that kind of item, keep every product in one coherent range, and "
        "never invent a large headline figure — an unstated price is a placeholder the merchant "
        "will correct, not a valuation of their work. "
        "emoji (single emoji for the brand), "
        # PHOTOGRAPHY. The store previously had none, and a wrong photo is worse
        # than no photo — a stock shot of a yoga mat on a store selling dumbbells
        # is a false product claim we made on the merchant's page. So the model is
        # asked for two different things per slot: the QUERY (what to search) and
        # the SUBJECT (the nouns a candidate photo's own description must contain
        # before app/imagery.py will use it). The subject is what turns "a search
        # returned something" into "this photo verifiably shows the product".
        #
        # The empty-field instruction is the honesty valve, and it matters more than
        # the rest: asked to illustrate a Notion template, stock photography can
        # only offer a stranger's laptop. The model is the best available judge of
        # whether a thing is photographable at all, so it is given explicit
        # permission to decline — and declining leaves the store on its generative
        # texture, which is the correct outcome, not a degraded one.
        "hero_image_query (2-6 words naming a real, photographable scene that shows what this "
        "brand sells, phrased the way a stock photographer would caption it, e.g. "
        "'woman lifting dumbbells in gym', 'espresso pouring into white cup'), "
        "hero_image_subject (2-4 concrete visible nouns from that scene, space-separated, "
        "no adjectives, e.g. 'dumbbell gym woman' or 'espresso cup coffee'), "
        "lifestyle_queries (an array of 0 to 3 further photographable scenes, same style, "
        "showing the product in use or the world around it), "
        "and for EACH product: image_query and image_subject. A product's image_query must make "
        "THAT ITEM the main subject of the frame, not a background detail — lead with the item and "
        "say so, e.g. 'close up of black compression leggings' rather than 'woman at the gym in "
        "leggings', 'folded wool scarf on a table' rather than 'person walking in winter'. Name the "
        "item first, keep it to what is being sold, and do not add other products (shoes, phones, "
        "laptops, mugs) that would compete with it for attention. image_subject is 2-4 concrete "
        "visible nouns that a photo MUST contain to depict that item, and its FIRST word must be "
        "the item's own everyday noun — the word a photographer would caption it with ('leggings', "
        "'scarf', 'candle', 'espresso'), never a material, texture, or generic word like 'fabric', "
        "'fold', 'product' or 'item'. Put the product noun first and supporting detail after it. "
        "CRITICAL: if what is being sold cannot honestly be photographed — software, a template, "
        "an ebook, a digital download, a subscription, a service, anything with no physical form — "
        "return an EMPTY STRING for every image_query and image_subject and an EMPTY ARRAY for "
        "lifestyle_queries. Never substitute a generic desk, laptop, office or abstract scene for "
        "a product that has no photograph. An empty field is correct and expected. "
        # The one thing those stores DO get. It is not a search query and is never
        # sent to a stock provider: a real photograph of a real desk asserts
        # something about goods that have no physical form, which is the claim the
        # rule above exists to prevent. A generated frame depicts nothing real, so
        # it can carry mood without carrying a lie — and the theme labels it.
        "hero_art_prompt (ONLY when you returned those fields empty because the goods have no "
        "physical form — otherwise return an EMPTY STRING): ONE sentence of AT MOST 20 WORDS "
        "describing a still, empty, ATMOSPHERIC scene to illustrate the brand's world. Describe "
        "light, material, colour and place — e.g. 'morning light across a clean oak desk with a "
        "linen notebook and a cup of black coffee' or 'calm studio corner with paper, brass "
        "instruments and soft shadow'. FORBIDDEN, with no exceptions: the product itself; any "
        "phone, laptop, tablet, monitor, screen or user interface; any person or part of a person; "
        "any text, signage, logo or brand mark. Measured on the live catalogue, those are exactly "
        "the two the model reaches for unprompted — an invoicing tool asked for a glowing phone, a "
        "team tool for seated teammates — so treat them as hard bans, not preferences. "
        # Colour and layout are no longer free choices. Asked to pick four hex
        # values and five style axes, the model returned essentially one palette
        # shape and three layouts across the entire catalogue — so the axes are
        # now seeded from the slug (resolve_design) and the palette is derived from
        # a single hue (app.palette). What is left here is the part the model is
        # genuinely better at than a PRNG: which hue suits what is being sold, and
        # which named personality the brand is.
        # ONE style question, deliberately. Measured against the live model: asked
        # for a hue it answers well and variously (25 for roasted coffee, 210 for a
        # productivity tool, 45 for beeswax). Asked in the same breath for a
        # harmony it said "analogous" three times out of three, and for a persona
        # it said "warm-craft" two out of three. The modal-answer problem is not
        # specific to the style axes — it appears wherever the model is offered a
        # menu. So it is asked only for the judgement that is genuinely semantic,
        # and everything else is seeded from the slug.
        "brand (object with EXACTLY one key: "
        "hue (a number 0-359, the brand's base colour on the colour wheel — "
        "0 red, 30 orange, 45 amber, 60 yellow, 120 green, 175 teal, 210 blue, "
        "265 violet, 320 pink; choose what the product itself evokes, e.g. "
        "roasted coffee near 25, fresh produce near 110, fintech near 215)). "
        "Do NOT output palette hex values, a theme, a design_dna object, a "
        "design_persona, a harmony or a mood; those are all computed. "
        "Make copy crisp and compelling, no placeholders."
    )
    resp = _post_generation(prompt)
    text = resp["content"][0]["text"]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        # Malformed output is an outage from the caller's view: fold into the same
        # 503 path as a real outage, never a 500 the buyer/agent can't act on.
        raise GenerationUnavailable("LLM did not return JSON: " + text[:200])
    try:
        raw = json.loads(m.group(0))
        data = GeneratedContent.model_validate(raw).model_dump()
    except (json.JSONDecodeError, ValidationError) as exc:
        # Braces present but unparseable / schema-invalid is still a bad-output
        # outage from the caller's view — fold into the same 503 path, not a 500.
        raise GenerationUnavailable(f"LLM returned unusable JSON: {exc}") from exc
    # A non-positive price would make checkout auto-confirm with no payment (and a
    # 0 would violate the Product price_micro>0 CHECK). Clamp every product up to a
    # sane floor, then re-mirror the primary product onto the scalar fields.
    for product in data["products"]:
        # USDT0 settles at 6dp, so quantise BEFORE the check: store.json is written
        # from this content before the Product row exists, and only a price already
        # at micro precision can round-trip through price_micro unchanged. A
        # sub-micro price is economically zero and falls into the clamp below.
        product["price_usdt"] = round(float(product["price_usdt"]), 6)
        if product["price_usdt"] <= 0:
            logger.warning(
                "generated product %r had no valid price; coercing to %.2f USDT",
                product.get("name", ""),
                MIN_PRICE_USDT,
            )
            product["price_usdt"] = MIN_PRICE_USDT
    primary = data["products"][0]
    data["product_name"] = primary["name"]
    data["product_blurb"] = primary["blurb"]
    data["cta_text"] = primary["cta_text"]
    data["price_usdt"] = primary["price_usdt"]
    # Surface token spend to the caller (create_store/upgrade_store log it to
    # event_log) and to journald, so llm cost is queryable per store. Under
    # reserved keys the caller strips before persisting/rendering content.
    usage = resp.get("usage") or {}
    data["_llm_in"] = int(usage.get("input_tokens", 0) or 0)
    data["_llm_out"] = int(usage.get("output_tokens", 0) or 0)
    logger.info(
        "llm usage: model=%s in=%d out=%d",
        MODEL,
        data["_llm_in"],
        data["_llm_out"],
    )
    return data


def _screening_text(desc: str, content: dict) -> str:
    """The text Warden screens, assembled BEFORE anything is rendered or fetched.

    The photography search text is included: it is model-generated, derived from the
    merchant's description, and it decides what images the store goes looking for —
    so a description steering toward disallowed imagery is caught here, on the same
    single screening call, before any photo is requested from the provider. That
    covers ``hero_art_prompt`` too: it never reaches a stock provider, but it is the
    literal instruction an image GENERATOR is handed, which is if anything the more
    important of the two to screen.

    The whole catalog is included too, not just the scalar product_* fields, which
    only ever carry the first product: every product's copy is model output that
    gets rendered, so all of it is screened on that same single call.

    What this does NOT cover, stated plainly: the provider's own description of a
    photo it returns (rendered as the ``alt`` text) is third-party text screened by
    nobody. It is length-capped and autoescaped, and every photo's provenance is
    persisted with the store so it can be audited or swapped, but it is not
    Warden-screened. See :mod:`app.imagery`.
    """
    extra_text = [
        content.get("hero_image_query", ""),
        content.get("hero_image_subject", ""),
        content.get("hero_art_prompt", ""),
        *(q for q in (content.get("lifestyle_queries") or []) if isinstance(q, str)),
    ]
    for product in content.get("products") or []:
        if isinstance(product, dict):
            extra_text.append(str(product.get("name", "")))
            extra_text.append(str(product.get("blurb", "")))
            extra_text.append(str(product.get("cta_text", "")))
            extra_text.append(str(product.get("image_query", "")))
            extra_text.append(str(product.get("image_subject", "")))
    return "\n".join(
        [
            desc,
            content.get("store_name", ""),
            content.get("tagline", ""),
            content.get("hero_headline", ""),
            content.get("hero_subcopy", ""),
            content.get("product_name", ""),
            content.get("product_blurb", ""),
            content.get("cta_text", ""),
            content.get("emoji", ""),
            *extra_text,
        ]
    )


def _persist_receipt(session, store_id: int, receipt) -> None:
    """Write one ScreeningReceipt row bound to `store_id`, in the caller's txn. A
    None receipt (defensive: a paid/demo screen that returned no receipt) is a
    no-op."""
    if receipt is None:
        return
    session.add(
        ScreeningReceipt(
            store_id=store_id,
            mode=receipt.mode,
            verdict=receipt.verdict,
            risk_level=receipt.risk_level,
            endpoint=receipt.endpoint,
            amount_micro=receipt.amount_micro,
            tx_hash=receipt.tx_hash,
        )
    )


def create_store(desc, addr=None, delivery=None, theme=None):
    """Full pipeline: prompt -> generate -> screen -> render -> persist.
    Raises screening.ScreeningBlocked (fail-closed) if the content is unsafe.
    Returns dict; a screening-unavailable outcome returns a pending_screening
    dict with no live page deployed, instead of failing the request outright.

    `theme` is the caller's explicit choice (short name, already validated); when
    None the LLM's own suggestion is used. The store keeps the resolved theme.

    Writes to BOTH the DB (source of truth for checkout and resume) and disk:
    index.html so nginx keeps serving /s/<slug>/, and store.json as one-milestone
    rollback insurance so the pre-M2 app can still read a store it didn't create.
    """
    addr = addr or DEFAULT_ADDR
    content = generate(desc)
    # Strip the token-usage sidecar keys before content is persisted/rendered; they
    # go into the store.created event instead so spend stays queryable from event_log.
    llm_in = content.pop("_llm_in", 0)
    llm_out = content.pop("_llm_out", 0)
    # theme_file is resolved inside the slug loop below, once the slug (and so the
    # seeded persona) is known.
    outcome = screening.screen(_screening_text(desc, content))
    pending = outcome.status == "pending"
    # Per-store capability secret returned ONCE to the paid caller (the store
    # owner by construction). Only its sha256 hash is persisted.
    manage_key, manage_key_hash = mint_manage_key()

    # What the receipt quotes. resync_catalog rebuilds content from the persisted
    # Product rows, so the live storefront can differ from the generation values
    # this dict starts as; the live-store branch replaces it with the persisted
    # catalog so a receipt can never promise a price checkout will not honour.
    receipt_content = content
    # Resolve the slug and write everything slug-dependent inside a short retry
    # loop: stores.slug is UNIQUE, so a concurrent create that grabbed the same
    # candidate between our check and insert raises IntegrityError — we clean up,
    # re-slug (now DB-aware, so it skips the committed row) and re-render once.
    for attempt in range(2):
        slug = unique_slug(slugify(content.get("store_name") or desc), content)
        # Visual identity is seeded from the FINAL slug, so it is decided here rather
        # than at generation time (and re-decided if the retry below re-slugs). An
        # explicit caller `theme` still wins — only the LLM's own theme guess is
        # displaced, because that guess clustered as hard as its axis picks did.
        seeded_dna, persona, seeded_theme = resolve_design(slug, content)
        content["design_dna"] = seeded_dna
        content["design_persona"] = persona
        # Colour is derived from one hue by a stated relationship, with contrast
        # floors enforced, rather than taken as four unrelated hex values.
        content["palette"] = resolve_palette(slug, content)
        theme_file = _resolve_theme(theme or seeded_theme)
        store_delivery = delivery
        if store_delivery is None:
            store_delivery = (
                f"✅ Thank you! Your {content.get('product_name', 'product')} is ready: "
                f"https://tilla.gudman.xyz/files/{slug} (demo delivery link)"
            )

        d = STORES_DIR / slug
        d.mkdir(parents=True, exist_ok=True)
        # Photography, resolved here rather than at generation time for the same
        # reason the palette and the DNA are: it is seeded from the FINAL slug (the
        # seed breaks ties between equally-relevant candidates, so a store's photos
        # are varied across stores and fixed for any one store), and the files land
        # in that slug's own directory. Resolved even when screening is pending, so
        # the resume path re-renders WITH the photographs instead of losing them.
        #
        # On the rare re-slug retry below this runs again against the new directory,
        # costing a few extra provider calls — accepted, because it keeps the images,
        # the palette and the DNA all derived from one slug with no special case.
        #
        # Fails open by construction (see app/imagery.py): no key, a provider
        # outage, or nothing that verifiably depicts the product all leave this
        # empty, and the store renders on its generative texture exactly as it did
        # before photography existed.
        #
        # ASYNC BY DEFAULT (IMAGERY_ASYNC): the paid caller gets the live store in
        # seconds and _resolve_imagery_async rerenders when the photos land.
        if not IMAGERY_ASYNC:
            imagery.apply(
                content,
                imagery.resolve(content, d, _mulberry32(_fnv1a(slug + ":imagery"))),
            )

        if pending:
            # Persist everything needed to resume (render + go live) once
            # screening recovers — but write no index.html, so checkout stays
            # 409'd until then.
            meta = {
                "slug": slug,
                "status": "pending_screening",
                "product_name": content.get("product_name", ""),
                "amount_usdt": content.get("price_usdt", 0),
                "pay_to": addr,
                "delivery": store_delivery,
                "description": desc,
                "content": content,
                "theme": theme_file,
            }
        else:
            _write_store_pages(d, content, addr, slug, theme_file)
            meta = {
                "slug": slug,
                "status": "live",
                "product_name": content.get("product_name", ""),
                "amount_usdt": content.get("price_usdt", 0),
                "pay_to": addr,
                "delivery": store_delivery,
                # Render inputs, so an import + re-render can rebuild index.html.
                "description": desc,
                "content": content,
                "theme": theme_file,
            }
        (d / "store.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        try:
            with SessionLocal() as session:
                merchant = get_or_create_merchant(session, addr)
                store = Store(
                    slug=slug,
                    merchant_id=merchant.id,
                    status="pending_screening" if pending else "live",
                    pay_to=addr,
                    manage_key_hash=manage_key_hash,
                    delivery=store_delivery,
                    description=desc,
                    # Persist content for LIVE stores too (not just pending), so a
                    # later theme fix can re-render their static index.html.
                    content=content,
                    theme=theme_file,
                )
                session.add(store)
                session.flush()
                # One Product row per catalog item, in content order — the first
                # (lowest id) is the primary product every single-product code
                # path selects. The price floor guarantees price_micro > 0 (the
                # Product CHECK) even if an unclamped item ever slips through.
                catalog = content.get("products") or [
                    {
                        "name": content.get("product_name", ""),
                        "price_usdt": content.get("price_usdt", 0),
                    }
                ]
                for item in catalog:
                    session.add(
                        Product(
                            store_id=store.id,
                            name=str(item.get("name", "")),
                            # Guards the CHECK only. A stated sub-1-USDT price is
                            # legitimate — the catalog schema allows ge=0, the
                            # dashboard ge=0.01, and Tilla's own services sell at
                            # 0.01-0.05 — so this must never silently reprice a
                            # merchant's catalog; generate() has already coerced
                            # any non-positive price to MIN_PRICE_USDT.
                            price_micro=max(
                                int(round(float(item.get("price_usdt", 0)) * 1e6)),
                                1,
                            ),
                            active=True,
                        )
                    )
                # For a live store, re-derive the catalog from the just-inserted
                # Product rows so content carries their stable ids and the storefront
                # renders id-based buy buttons (re-renders index.html once more with
                # ids). Pending stores get this when resume_pending flips them live.
                if not pending:
                    resync_catalog(session, store)
                    receipt_content = dict(store.content or {})
                # Record the screening receipt in the SAME txn as the store row. A
                # pending (screening-unavailable) create has no verdict, so no
                # receipt — one is written when resume_pending flips it live.
                if not pending:
                    _persist_receipt(session, store.id, outcome.receipt)
                log_event(
                    session,
                    "api",
                    "store.created",
                    store_id=store.id,
                    data={"slug": slug, "llm_in": llm_in, "llm_out": llm_out},
                )
                log_event(
                    session,
                    "api",
                    "store.screening_pending" if pending else "store.live",
                    store_id=store.id,
                )
                session.commit()
                created_store_id = store.id
            break
        except IntegrityError:
            shutil.rmtree(d, ignore_errors=True)
            if attempt == 1:
                raise

    if IMAGERY_ASYNC and imagery.enabled():
        threading.Thread(
            target=_resolve_imagery_async,
            args=(created_store_id, slug),
            name=f"imagery-{slug}",
            daemon=True,
        ).start()

    if pending:
        return {
            "slug": slug,
            "status": "pending_screening",
            "store_name": content.get("store_name", ""),
            "manage_key": manage_key,
            "message": (
                "Store queued: content screening is temporarily unavailable. "
                "It will not go live until screening completes."
            ),
        }
    return {
        "slug": slug,
        "store_name": receipt_content.get("store_name", ""),
        "url": f"https://tilla.gudman.xyz/s/{slug}/",
        "product_name": receipt_content.get("product_name", ""),
        "price_usdt": receipt_content.get("price_usdt", 0),
        "manage_key": manage_key,
    }


def _resolve_imagery_async(store_id: int, slug: str) -> None:
    """Resolve a fresh store's photography AFTER its create call has returned.

    The paid caller already has a fully live store on its generative texture; this
    thread adds the photographs and re-renders when they land. It re-reads the
    store's content from the DB at BOTH ends: at the start so the queries resolved
    are the persisted ones, and again before writing, so a catalog edit that
    happened while the provider was searching is never clobbered by a stale copy
    (photos attach per product id via imagery.apply, same as the backfill).
    Fail-open like every other imagery path — any error leaves the store exactly
    as the create call delivered it, and only this thread dies.
    """
    try:
        with SessionLocal() as session:
            store = session.get(Store, store_id)
            if store is None or not isinstance(store.content, dict):
                return
            content = json.loads(json.dumps(store.content))
        d = STORES_DIR / slug
        resolved = imagery.resolve(content, d, _mulberry32(_fnv1a(slug + ":imagery")))
        if (
            resolved.hero is None
            and not any(image is not None for image in resolved.products)
            and not resolved.lifestyle
        ):
            return  # nothing cleared the bar; the texture stands
        with SessionLocal() as session:
            store = session.get(Store, store_id)
            if store is None or not isinstance(store.content, dict):
                return
            content = json.loads(json.dumps(store.content))
            imagery.apply(content, resolved)
            store.content = content
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(store, "content")
            meta_path = d / "store.json"
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["content"] = content
                meta_path.write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            # Pending stores get their render (with these photos) from
            # resume_pending; re-rendering here would create the index.html that
            # keeps checkout 409'd until screening clears.
            if store.status == "live":
                _write_store_pages(d, content, store.pay_to, slug, store.theme)
            session.commit()
        logger.info("imagery: async resolve landed for %s", slug)
    except Exception:
        logger.exception(
            "imagery: async resolve failed for %s (store keeps its texture)", slug
        )


def upgrade_store(session, store, description=None, theme=None) -> dict:
    """M10 upgrade-store seam: regenerate a live store's copy (and optionally its
    theme), re-screen FAIL-CLOSED, then apply + re-render. Screening runs BEFORE any
    write: ScreeningBlocked (unsafe) and ScreeningUnavailable (screening down) both
    propagate with the store untouched, so the old live page keeps serving and the
    paid endpoint returns >=400 (the x402 middleware skips settle, zero funds move).
    Only an explicit ALLOW updates stores.content/description/theme and re-renders
    index.html. The product row + price are untouched (price changes stay on the
    pricing route). Commits in the caller's txn."""
    desc = description if description is not None else (store.description or "")
    content = generate(desc)
    llm_in = content.pop("_llm_in", 0)
    llm_out = content.pop("_llm_out", 0)
    theme_file = _resolve_theme(theme) if theme else store.theme
    outcome = screening.screen(_screening_text(desc, content))
    if outcome.status != "allow":
        # Screening unavailable — refuse rather than deploy unscreened content or
        # strand the live store in a pending state.
        raise screening.ScreeningUnavailable(
            "content screening temporarily unavailable"
        )
    store.content = content
    if description is not None:
        store.description = desc
    store.theme = theme_file
    # Re-derive the catalog from the ACTIVE Product rows before rendering. generate()
    # is asked for 1-4 products with LLM-chosen prices and NO ids, but an upgrade must
    # not change what the store SELLS — the Product rows (and their prices) are
    # deliberately untouched here. Rendering the generated list directly desynced the
    # page from checkout: buy buttons fall back to product_index when content items
    # lack ids, so an invented 2nd-4th item 400'd ("product_index out of range") and
    # the first charged the OLD product at the OLD price whatever the page showed.
    # resync_catalog rewrites content['products'] from the real rows (keeping the new
    # blurb/cta by id) so page and checkout agree.
    resync_catalog(session, store)
    content = store.content
    d = STORES_DIR / store.slug
    d.mkdir(parents=True, exist_ok=True)
    # Photography is re-resolved AFTER the resync, never before it: the resynced list
    # is the real Product rows, whereas the freshly generated list may hold invented
    # items in a different order, so resolving against the generated list would attach
    # photographs positionally to products that are not the ones being sold. The
    # resync carried each item's image_query across by id, so the search text is
    # still here to resolve from.
    #
    # An upgrade regenerates the store's whole presentation, so its photography is
    # regenerated with it rather than left describing the previous copy. Same
    # fail-open contract as create: if the provider is unreachable the store simply
    # keeps the photographs it already had.
    resolved = imagery.resolve(content, d, _mulberry32(_fnv1a(store.slug + ":imagery")))
    if any(image is not None for image in resolved.products) or resolved.hero:
        imagery.apply(content, resolved)
        from sqlalchemy.orm.attributes import flag_modified

        store.content = content
        flag_modified(store, "content")
    _write_store_pages(d, content, store.pay_to, store.slug, theme_file)
    _persist_receipt(session, store.id, outcome.receipt)
    log_event(
        session,
        "api",
        "store.upgraded",
        store_id=store.id,
        data={"slug": store.slug, "llm_in": llm_in, "llm_out": llm_out},
    )
    session.commit()
    return {
        "slug": store.slug,
        "url": f"https://tilla.gudman.xyz/s/{store.slug}/",
        "status": "upgraded",
        "store_name": content.get("store_name", ""),
        "theme": theme_file,
    }


def resume_pending():
    """Retry screening for every store left in pending_screening (e.g. after a
    process restart). Reads pending stores from the DB — the content column
    holds every render input. On ALLOW: render + write index.html + flip the
    row live. On BLOCK: mark the row blocked and remove the store dir. Still
    unavailable: leave it for the next attempt. Defensive — a store missing its
    content is logged and skipped, never fatal, so this is safe at startup."""
    with SessionLocal() as session:
        stores = session.scalars(
            select(Store).where(Store.status == "pending_screening")
        ).all()
        for store in stores:
            content = store.content
            if not isinstance(content, dict):
                logger.warning(
                    "resume_pending: %s has no content, skipping", store.slug
                )
                continue
            try:
                outcome = screening.screen(
                    _screening_text(store.description or "", content)
                )
            except screening.ScreeningBlocked:
                logger.warning("resume_pending: %s blocked by screening", store.slug)
                store.status = "blocked"
                log_event(session, "resume", "store.blocked", store_id=store.id)
                session.commit()
                shutil.rmtree(STORES_DIR / store.slug, ignore_errors=True)
                continue
            if outcome.status != "allow":
                continue
            d = STORES_DIR / store.slug
            d.mkdir(parents=True, exist_ok=True)
            # Re-derive the catalog from the Product rows (populates ids -> id-based
            # buy buttons) and render the static pages in one pass.
            resync_catalog(session, store)
            _mark_store_json_live(d, store.slug)
            store.status = "live"
            _persist_receipt(session, store.id, outcome.receipt)
            log_event(session, "resume", "store.live", store_id=store.id)
            session.commit()
            logger.info("resume_pending: %s screened clean, now live", store.slug)


def rerender_stores() -> dict:
    """Re-render index.html for every live store from its persisted content, so a
    theme change (e.g. the exact-amount checkout row) reaches already-deployed
    static pages instead of leaving them serving stale HTML. Idempotent; never
    deletes. A live store with no persisted content is logged and skipped (never
    fatal), mirroring resume_pending — such a store predates content persistence
    and must be re-created or re-imported to recover its render inputs."""
    rendered = skipped = 0
    with SessionLocal() as session:
        stores = session.scalars(select(Store).where(Store.status == "live")).all()
        for store in stores:
            content = store.content
            if not isinstance(content, dict):
                logger.warning(
                    "rerender_stores: %s has no persisted content, skipping",
                    store.slug,
                )
                skipped += 1
                continue
            try:
                d = STORES_DIR / store.slug
                d.mkdir(parents=True, exist_ok=True)
                _write_store_pages(d, content, store.pay_to, store.slug, store.theme)
                rendered += 1
            except Exception:
                # Runs at startup — one store that fails to render (e.g. a
                # missing template or bad content) must never crash the whole
                # service and take checkout down. Log and skip it.
                logger.exception("rerender_stores: failed to render %s", store.slug)
                skipped += 1
    logger.info("rerender_stores: rendered=%d skipped=%d", rendered, skipped)
    return {"rendered": rendered, "skipped": skipped}


def _mark_store_json_live(store_dir, slug):
    """Flip the on-disk store.json to status=live so the rollback-insurance copy
    stays in step with the DB (a pre-M2 checkout reads status from store.json)."""
    meta_path = store_dir / "store.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["status"] = "live"
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (ValueError, OSError):
        logger.warning("resume_pending: could not update store.json for %s", slug)


def main():
    if len(sys.argv) < 2:
        print(
            "usage: python3 -m app.engine '<what you sell>' [receive_address] [delivery]"
        )
        sys.exit(1)
    if not KEY:
        print("ERROR: TILLA_LLM_KEY not set")
        sys.exit(1)
    addr = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_ADDR
    delivery = sys.argv[3] if len(sys.argv) > 3 else None
    print(f"[*] generating store from: {sys.argv[1]!r}")
    r = create_store(sys.argv[1], addr, delivery)
    print(f"[*] result -> {r}")
    print("RESULT:", json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
