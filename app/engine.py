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
import colorsys
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
    Deliverable,
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

# The fabricated URL the first honest default replaced. Stores created before that
# fix persisted it into stores.delivery, so it is also a marker the repair matches.
LEGACY_DELIVERY_MARKER = "/files/"

# The restraint rules that bind EVERY buyer-facing line Tilla generates, not only the
# storefront's. generate()'s prompt below states them at length, with the field names
# and worked examples that only a storefront has; this is the compact form for the
# OTHER generated surface, app/growth.py's marketing copy (_build_prompt and
# _channel_prompt), which had no claims language at all.
#
# WHY it is shared rather than written twice: growth copy is the WORSE place for the
# docs/ISSUES.md #2 failure. A false capability on a storefront is on a page Tilla
# renders and can re-render; the same invention in a social post or marketing email is
# published OFF-platform under the merchant's own name, where nothing we do can
# retract it. Two independently-worded rules would have drifted the first time either
# was edited, and the drift would be invisible — so both growth prompt builders import
# this one string, and a test pins its vocabulary against generate()'s prompt.
#
# The traction clause has no counterpart in generate() because only growth has the
# input that invites it: the scheduler feeds real first-party numbers into the prompt
# (_performance_block), and "6 orders" paraphrased into "selling fast" is an invented
# demand claim built from a true figure.
COPY_RESTRAINT = (
    "CLAIMS AND COMMITMENTS — these bind every line you write. Describe only what the "
    "details above support. Never invent capabilities, features, credentials, "
    "integrations or coverage that is not stated there, and never enumerate what the "
    "product includes or covers unless the details list those items themselves. Never "
    "state a delivery frequency (daily, weekly, real-time, 24/7), a turnaround or "
    "response time (instant, within 24 hours), a quantity or coverage count, or a "
    "guarantee, refund or SLA unless the details state it. Never assert traction, "
    "demand or popularity — no sales volume, no 'trusted by', no 'selling fast', no "
    "'join thousands', no invented scarcity or deadline — unless a number given above "
    "says so, and never restate a number you were given as a vaguer, bigger claim "
    "(6 orders is not 'flying off the shelves'). Tone is not a claim: evocative and "
    "textural language is welcome ('rich, full-bodied', 'crafted with care', 'cut "
    "through the noise') — what is forbidden is the measurable promise, not the warmth."
)


def default_delivery_text(slug: str, product_name: str) -> str:
    """What a buyer receives from a store with no deliverable attached.

    The original default handed them a fabricated
    ``https://tilla.gudman.xyz/files/<slug>`` link. That path is not a route and
    never was, so every unconfigured store promised a download that 404s —
    labelled "(demo delivery link)", but only AFTER payment.

    A buy still settles on this text: refusing to sell without a deliverable would
    break every existing store, including the ones a marketplace reviewer buys from.

    WHY IT NO LONGER NAMES AN ENDPOINT (found 2026-08-05, live on a listed store).
    Both earlier wordings closed by telling the reader to
    ``POST /api/stores/<slug>/deliverable`` with their manage key. The reader is a
    BUYER who has just paid: they hold no manage key, the store is not theirs, and a
    developer instruction is the one line in the message they cannot act on. The
    attribution was never the defect — the merchant's fulfilment gap genuinely is not
    Tilla's failure — the AUDIENCE was. Nothing is lost by dropping it: the create
    response already hands that same instruction to the MERCHANT as
    ``fulfilment.note`` (``main.create_store_post``), which is where a merchant is
    looking, and the dashboard lists deliverables per product. What remains answers
    the buyer's three questions only — did my payment go through, why is nothing
    here, what can I do now — and it names the refund as the MERCHANT'S to give,
    because that is the mechanism: every buy settles non-custodially to the
    merchant's own payTo, so Tilla holds nothing to refund and a promise here would
    be one we could not keep.

    Lives here as one function rather than inline because
    ``scripts/repair_delivery_text`` rewrites older stores and must produce
    byte-identical text — two copies of a user-facing string that have to agree is
    exactly how the receipt and the persisted price came to disagree. ``slug`` no
    longer appears in the text and is kept regardless: the repair renders this and
    :func:`superseded_delivery_texts` from the same pair of arguments, and the
    superseded wordings do contain it."""
    return (
        f"Payment received — thank you. {product_name} has nothing attached for "
        "automatic delivery: the merchant has not set that up yet. If it is a "
        "physical item or a service, the merchant delivers it directly and will "
        "follow up. If you expected a file or a licence key, ask the merchant to "
        "send it. Your payment went straight to the merchant's wallet, not to "
        "Tilla, so a refund is theirs to give if they cannot deliver."
    )


def superseded_delivery_texts(slug: str, product_name: str) -> tuple[str, ...]:
    """Every earlier wording of :func:`default_delivery_text`, rendered for a store.

    The repair rewrites text TILLA generated and must never touch text a merchant
    wrote. Matching on an exact render of a previous default is what proves
    authorship: a merchant's own words cannot collide with one of these, and a
    store repaired under an older wording is still recognised rather than frozen
    at whatever the default said the day it was created.

    Append here when the wording changes; never edit an entry, or the stores
    holding that exact text stop being repairable."""
    return (
        # v1, 2026-07-28. Said "no downloadable file", which is right for a report
        # and wrong for the watch, jersey and candle stores that also carried it.
        (
            f"Payment received — thank you. {product_name} "
            "has no downloadable file attached yet: the merchant has not "
            "configured fulfilment. If this is your store, attach the real "
            f"goods with POST /api/stores/{slug}/deliverable using your manage "
            "key, and buyers will receive a signed download link or licence key "
            "instead of this message."
        ),
        # v2, superseded 2026-08-05. Stopped assuming the goods were digital — the
        # fix v1 needed — but still closed with the endpoint and the manage key, so
        # it handed a paying buyer instructions only the merchant could follow.
        (
            f"Payment received — thank you. {product_name} has nothing attached for "
            "automatic delivery. If it is a physical item or a service, the merchant "
            "delivers it directly and will follow up. If you expected a file or a "
            "licence key, the merchant has not configured fulfilment yet. If this is "
            f"your store, attach the goods with POST /api/stores/{slug}/deliverable "
            "using your manage key and buyers will receive a signed download link or "
            "licence key instead of this message."
        ),
    )


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


class IdempotentReplay(RuntimeError):
    """This paid create-store's (payer, Idempotency-Key) pair ALREADY funded a store,
    discovered at the insert rather than by the pre-check: two requests carrying one
    key raced, and this one lost the unique index. Carries the winner's ``slug`` so
    ``main.create_store_post`` can answer 409 with the store the caller already owns.

    It exists to keep that outcome OUT of the slug-collision branch. That branch
    ``rmtree``s the store directory, and a loser that had resolved the same slug as
    the winner would delete the winner's COMMITTED, PAID files — buyer charged, store
    URL 404. This is not an exotic race: a live create runs ~15s and agent clients
    give up around 10s and retry, so both attempts are in the pipeline at once."""

    def __init__(self, slug: str):
        super().__init__(slug)
        self.slug = slug


def _idempotency_holder(addr: str | None, key: str | None) -> str | None:
    """The slug of the store this (payer, Idempotency-Key) pair already funded, or
    None. Read on a FRESH session: by the time an IntegrityError reaches its handler
    the failing transaction has been rolled back and closed, so the row the winning
    request committed is only visible from a new one."""
    if not addr or not key:
        return None
    with SessionLocal() as session:
        return session.scalar(
            select(Store.slug).where(
                Store.create_idempotency_addr == addr,
                Store.create_idempotency_key == key,
            )
        )


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
    (blurb, cta_text)}``) supplies them for a product just added or edited. An
    id-less content item — content predating CRUD, or a catalog just generated by
    ``upgrade_store`` — is attached to a row by exact NAME, never by position.
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
    # An id-less item is attached by exact NAME (docs/ISSUES.md #9, 2026-08-05). The
    # POSITIONAL backfill this replaces was written for pre-CRUD content, where order
    # provably IS id order because the rows were created FROM that content — but
    # `upgrade_store` hands this function id-less items too, and those are freshly
    # GENERATED: their order and count are the model's, so a blurb describing one
    # product attached to a different one. False text about a real product, on the
    # page and (since #6) in every feed. Legacy content still carries correctly under
    # the name rule: its names equal the row names by construction, and every
    # dashboard rename resyncs content from the rows, so the two never drift. Same
    # names are consumed in id order (first unclaimed wins) so duplicates resolve
    # deterministically. One old item and one active row can only mean each other, so
    # a single-product store the model renamed still carries; anything else that
    # matches nothing carries NOTHING and that product renders an empty blurb ("" /
    # "Buy now") — honest and merchant-fixable, unlike text about goods they do not
    # sell.
    by_name: dict[str, list[int]] = {}
    for p in active:
        by_name.setdefault(p.name.strip().casefold(), []).append(p.id)
    for item in old:
        if not isinstance(item, dict):
            continue
        key = item.get("id")
        if key is None:
            same = by_name.get(str(item.get("name", "")).strip().casefold())
            if same:
                key = same.pop(0)
            elif len(old) == 1 and len(active) == 1:
                key = active[0].id
            else:
                continue
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
    else:
        # No active product: the scalar primary MUST go too. render._catalog falls
        # back to these fields when the list is empty (that fallback is what makes
        # pre-multi-product stores render), so leaving them would rebuild a working
        # buy button for the product just deactivated — a storefront selling
        # something the checkout would then refuse.
        for key in ("product_name", "product_blurb", "cta_text", "price_usdt"):
            content.pop(key, None)
    store.content = content
    flag_modified(store, "content")
    d = STORES_DIR / store.slug
    d.mkdir(parents=True, exist_ok=True)
    _write_store_pages(
        d,
        content,
        store.pay_to,
        store.slug,
        store.theme,
        sandbox=store.status == "sandbox",
    )


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
    _write_store_pages(
        d,
        content,
        store.pay_to,
        store.slug,
        store.theme,
        sandbox=store.status == "sandbox",
    )


def _write_store_pages(
    d, content: dict, addr: str, slug: str, theme: str, sandbox: bool = False
) -> None:
    """Write the nginx-served static assets for a live store: index.html (the chosen
    theme), og.svg (the Open Graph card source), and og.png (its raster for social
    scrapers). The <head> references og.png as og:image/twitter:image. ``sandbox``
    renders the page as the non-payable sample it is, instead of a storefront whose
    Buy button 404s."""
    (d / "index.html").write_text(
        render_theme(content, addr, slug, theme, sandbox=sandbox), encoding="utf-8"
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
        # Not "a focused product catalog" — that opening line primed plural products
        # against the ONE-product default below, the same early-instruction-vs-later-
        # rule contradiction that made the first issue-#2 fix inert (see that comment).
        "You are a world-class brand designer and DTC copywriter. A solo entrepreneur wants to sell "
        "something. Turn their description into a polished storefront selling exactly what they "
        "described — nothing more.\n\n"
        f'Merchant description: "{desc}"\n\n'
        "Output ONLY valid JSON (no markdown) with EXACTLY these keys: "
        # THE HERO FIELDS CARRY THE SAME ANCHOR AS blurb, for the reason in the comment
        # below: a constraint stated only in a later paragraph loses to the field spec.
        # Until now only `blurb` had one, and every worked example in the claims rule
        # was blurb-shaped, so the rule read as products-only — while hero_subcopy is
        # the field that travels FURTHEST. agentic.py::_store_description returns
        # `hero_subcopy or tagline or store.description`, so this generated line — not
        # the merchant's own words — is the store description in feed.json, llms.txt,
        # the discovery surfaces, the Google RSS item, the meta description and
        # og:description. An unanchored hero_subcopy is the machine-readable claim a
        # buying agent parses BEFORE it pays.
        "store_name (short brand), "
        "tagline (<=6 words, claiming ONLY what the merchant stated), "
        "hero_headline (punchy, <=8 words, claiming ONLY what the merchant stated), "
        "hero_subcopy (1 sentence, claiming ONLY what the merchant stated — this line is "
        "published as the store's machine-readable description, so other agents read it "
        "before deciding to pay), "
        # NOT "benefit-led". That word sat here while the claims rule below said the
        # opposite, and the model followed the instruction attached to the field: a
        # thin description still produced invented "team credibility" and "tokenomics".
        # The constraint has to live ON the field, not only in a later paragraph.
        "products (an array of 1 to 4 objects, each an object with: name, blurb (1-2 sentences, "
        "claiming ONLY what the merchant stated — see the claims rule below), "
        "price_usdt (number), cta_text (<=4 words), image_query, image_subject) "
        # ONE PRODUCT IS THE DEFAULT (docs/ISSUES.md #3). This used to read "use a
        # single item when the merchant clearly sells one thing, otherwise 2 to 4
        # distinct items" — so multi-product was what happened whenever the model
        # could not judge the description "clearly" singular, which is exactly the
        # one-line-description case. "I sell consulting" became a three-tier ladder
        # including "Project Consulting" at 2500 USDT. An invented tier is not just
        # invented copy: it is a product the merchant does not sell, at a price they
        # never chose. A merchant who wants more adds them deliberately via
        # POST /api/merchant/stores/{slug}/products, which cannot fabricate anything.
        "— ONE product unless the merchant's own description names more than one distinct thing "
        "they sell; then give one product per item they actually named, up to 4. Never invent a "
        "tiered ladder (Basic/Premium, Daily/Weekly, Starter/Pro, Standard/Deep-Dive) and never "
        "split a single offering into several priced levels — a tier the merchant did not describe "
        "is a product they do not sell at a price they did not choose. One product from a short "
        "description is the correct output, "
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
        # NAMES ARE THE MERCHANT'S TOO — the same rule as prices, and a worse
        # failure when broken. A merchant who writes "my shop is called Ember &
        # Oak" and gets "Lumina" has had their own brand replaced by a generated
        # one, and store_name mints the slug (see unique_slug below), which is
        # permanent — a rename keeps the original slug forever.
        "and for names: if the merchant's description states a store name or a product name, use "
        "that EXACT name — never translate, shorten, prettify or substitute it. Invent a name only "
        "when the merchant has not stated one. "
        # An invented name is the one invented string that cannot be corrected: it mints
        # the slug (unique_slug below) and a rename keeps the original slug forever. So
        # the licence to invent one must not double as a licence to mint a credential —
        # "Certified Signals Co." asserts an accreditation in the store's own URL.
        "When you do invent a name, it must NOT contain a credential, certification or authority "
        "word (Certified, Licensed, Official, Accredited, Institutional) unless the merchant used "
        "that word themselves — a name is permanent because it mints the store's URL, and a "
        "credential in a name is a claim. "
        # CLAIMS ARE THE MERCHANT'S TOO — same rule as prices and names, and the one
        # that reaches a buyer's wallet. Asked for a "benefit-led" blurb from a thin
        # description, the model fills the space with impressive specifics: a store
        # selling token due-diligence reports was published with copy promising
        # "team credibility and tokenomics analysis" — words the merchant never
        # wrote, describing analysis their product does not produce. The merchant
        # reported it themselves (docs/ISSUES.md #2). That is not marketing polish;
        # it is a false advertisement we printed on someone else's storefront, and
        # the buyer who pays on the strength of it is the one harmed.
        # The scope list is explicit because the rule USED to open "and for claims:" with
        # every elaboration and example below it shaped like a product blurb — so it read
        # as a products-only rule, and the hero fields (which is what agents and search
        # engines actually consume) fell outside anything the model was told to restrain.
        "and for claims, in EVERY copy field (tagline, hero_headline, hero_subcopy, blurb, "
        "cta_text): describe only what the merchant's description supports. Never invent "
        "specific capabilities, features, credentials, integrations, guarantees or coverage the "
        "merchant did not state. Do NOT enumerate what a product examines, includes or covers "
        "unless the merchant listed those items themselves — naming the checks a service performs "
        "is a factual claim about their product, not marketing. Concretely: from 'I sell signed "
        "token due-diligence reports for EVM tokens', write 'Signed due-diligence analysis for any "
        "EVM token' and STOP; do NOT add 'covering contract security, tokenomics, team credibility' "
        "or any similar list, because you cannot know which checks that merchant actually runs. "
        # A second worked example, on a HERO field, because the first one is a blurb and
        # a blurb-shaped example is exactly how this rule came to be read as
        # products-only. Credentials are the class the hero fields attract: a subcopy
        # line has room for authority and no room for detail.
        "The same applies to the hero fields: from 'I sell yoga classes', hero_subcopy must NOT be "
        "'Certified instructors bring a decade of studio experience to every session' — the "
        "certification and the decade are credentials, and credentials are the merchant's to claim, "
        "never yours to add. "
        "A SHORT description must yield SHORT copy — one plain sentence restating what they sell is "
        "the correct output, not a failure to fill space. Vague and true beats detailed and false. "
        # QUANTIFIABLE COMMITMENTS ARE THE MERCHANT'S TOO — the residual edge left
        # after #1, #2 and #3 (docs/ISSUES.md), found 2026-08-05 while verifying #3.
        # "I sell trading signals" produced the blurb "Daily trading signals to guide
        # your market moves": one adjective, and the storefront now promises a
        # delivery cadence the merchant never offered. The claims rule above reads as
        # satisfied, because a measurable promise is not a capability or a tier — it
        # leaks in at WORD scale, small enough to survive a careful merchant reading
        # their own catalog, and a buyer who pays for a daily feed is still mis-sold.
        # Deliberately NOT a ban on flavour: the merchant who reported #2 praised the
        # copy's texture, and "rich, full-bodied" asserts nothing checkable. The line
        # is measurability, not warmth — numbers go, tone stays.
        "and for commitments, in EVERY copy field (tagline, hero_headline, hero_subcopy, blurb, "
        "cta_text): never state a delivery frequency (daily, weekly, monthly, real-time, 24/7), "
        "a turnaround or response time (instant, same-day, within 24 hours), a quantity or "
        "coverage count (500+ tokens, every chain, unlimited requests), or a guarantee, refund "
        "or SLA — unless the merchant's description states it. If they DID state one ('daily "
        "signals', '48-hour turnaround'), use it exactly as given and keep it. Concretely: from "
        "'I sell trading signals', write 'Trading signals to guide your market moves' and STOP; "
        "do NOT write 'Daily trading signals to guide your market moves', because the merchant "
        "never said daily and a buyer paying for a daily feed has been mis-sold. Tone is not a "
        "commitment: evocative and textural language is welcome ('rich, full-bodied', 'crafted "
        "with care', 'cut through the noise') — what is forbidden is the measurable promise, not "
        "the warmth. "
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


def _screening_text(desc: str, content: dict, *extra: str) -> str:
    """The text Warden screens, assembled BEFORE anything is rendered or fetched.

    ``extra`` carries caller-SUPPLIED buyer-facing strings (the fulfilment text and
    any deliverable payload). Model output is not the only thing a buyer reads: a
    merchant's own delivery message reaches them verbatim after payment, so it is
    screened on the SAME single call rather than a second paid hire. It rides here
    instead of inside ``content`` so it can never be mistaken for generated copy and
    persisted into the store's rendered content.

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
            *(t for t in extra if t),
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


def hue_from_hex(hex_color: str) -> float:
    """The HSL hue (0-360) of a ``#RRGGBB`` colour.

    The merchant's brand colour enters the palette as ONE number because that is
    the whole design of :mod:`app.palette`: a single hue plus a named harmony and
    mood derive the complete palette, with contrast floors enforced. Taking the
    hex verbatim into CSS would bypass every one of those guarantees; taking its
    hue keeps them all — the merchant picks the colour, the system keeps it
    legible."""
    raw = hex_color.lstrip("#")
    r, g, b = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
    h, _l, _s = colorsys.rgb_to_hls(r, g, b)
    return (h * 360.0) % 360.0


# What a caller does when the create response — the ONE place manage_key is ever shown
# — is lost. Asked for by 0xqdee on their paid create-store (2026-08-05): "add a
# recovery/status reference so a buyer whose response is lost can retrieve the result
# without paying again."
#
# WHY this is advertised rather than built: the recovery already existed and nothing
# said so. A store's merchant row is keyed on the receive_address
# (get_or_create_merchant below), and dashboard.resolve_merchant accepts a session
# token minted by signing a nonce with that same wallet — so whoever controls the
# receive_address can manage the store with NO manage_key. A real merchant did exactly
# that on 2026-08-05 (nonce -> personal_sign -> session -> PATCH a product). The
# manage_key is a headless convenience, not the only key: only its sha256 is persisted
# (delivery.hash_manage_key), so it can never be looked up or re-issued to anyone.
RECOVERY_NOTE = (
    "manage_key is shown once and cannot be re-issued — only its sha256 is stored, so "
    "a lost key cannot be looked up or resent. Losing it does not lose the store: the "
    "wallet you gave as receive_address owns this store. Sign in with that wallet at "
    "https://tilla.gudman.xyz/dashboard — POST /api/merchant/auth/nonce with the "
    "address, sign the message it returns, POST /api/merchant/auth/verify for a "
    "session token — and manage the store, its products, orders and fulfilment "
    "without the manage_key. Never pay to create this store again."
)

# WHY the sandbox variant says the opposite: a sandbox store was created WITHOUT a
# receive_address, so its merchant row is Tilla's own demo wallet (DEFAULT_ADDR) and
# the caller holds no wallet that can sign in for it. Handing them the sign-in recipe
# would name a wallet they do not have — the recovery advice above is FALSE here, and a
# false recovery path is worse than none. The manage_key half is identical: still shown
# once, still only stored as a hash.
SANDBOX_RECOVERY_NOTE = (
    "manage_key is shown once and cannot be re-issued — only its sha256 is stored, so "
    "a lost key cannot be looked up or resent. This sample was created without a "
    "receive_address, so it has no owning wallet and dashboard sign-in cannot recover "
    "it. Create the store again with a receive_address: that wallet then owns the "
    "store and can always sign in at https://tilla.gudman.xyz/dashboard to manage it, "
    "manage_key or not."
)


def create_store(
    desc,
    addr=None,
    delivery=None,
    theme=None,
    deliverable=None,
    notify_agent_id=None,
    brand_color=None,
    *,
    sandbox=False,
    idempotency_addr=None,
    idempotency_key=None,
    x402_nonce=None,
):
    """Full pipeline: prompt -> generate -> screen -> render -> persist.
    Raises screening.ScreeningBlocked (fail-closed) if the content is unsafe.
    Returns dict; a screening-unavailable outcome returns a pending_screening
    dict with no live page deployed, instead of failing the request outright.

    ``idempotency_addr``/``idempotency_key`` are the verified x402 payer and the
    client's Idempotency-Key; they are written on the Store row in the SAME
    transaction as the store itself, so a store can never exist without the key that
    funded it. Raises IdempotentReplay (carrying the existing slug) when that pair
    already funded a store — which the caller answers 409, so the duplicate payment
    never settles.

    ``x402_nonce`` is that payment's EIP-3009 authorization nonce, written on the same
    row in the same transaction. It is the only handle the settlement-failure hook has
    for finding this store if the payment that funded it never settles.

    `theme` is the caller's explicit choice (short name, already validated); when
    None the LLM's own suggestion is used. The store keeps the resolved theme.

    Writes to BOTH the DB (source of truth for checkout and resume) and disk:
    index.html so nginx keeps serving /s/<slug>/, and store.json as one-milestone
    rollback insurance so the pre-M2 app can still read a store it didn't create.
    """
    # A public API caller without a payout address gets the marketplace review
    # sample, not a merchant storefront: it remains non-payable. Internal callers
    # that use the historical default address keep their existing live-store
    # behavior unless they explicitly request this public sandbox mode.
    # The rendering pipeline still needs a valid address, so the preview uses the
    # historical demo address while its persisted state gates every checkout path.
    addr = addr or DEFAULT_ADDR
    content = generate(desc)
    # The merchant's stated brand colour beats the model's guess — same contract
    # as prices: what the merchant says is used exactly, auto fills the silence.
    # Only the HUE is taken (see hue_from_hex); harmony, mood and every contrast
    # floor still come from the palette system, so a merchant cannot pick a
    # colour that renders their own store illegible.
    if brand_color:
        brand = content.get("brand")
        brand = dict(brand) if isinstance(brand, Mapping) else {}
        brand["hue"] = hue_from_hex(brand_color)
        content["brand"] = brand
    # Strip the token-usage sidecar keys before content is persisted/rendered; they
    # go into the store.created event instead so spend stays queryable from event_log.
    llm_in = content.pop("_llm_in", 0)
    llm_out = content.pop("_llm_out", 0)
    # theme_file is resolved inside the slug loop below, once the slug (and so the
    # seeded persona) is known.
    # The caller's own fulfilment strings are screened on this SAME call — they are
    # buyer-facing text, and a second hire would double the 0.1 USDT screening cost
    # per create for no added safety.
    outcome = screening.screen(
        _screening_text(
            desc, content, delivery or "", (deliverable or {}).get("payload") or ""
        )
    )
    pending = outcome.status == "pending"
    store_status = "sandbox" if sandbox else "pending_screening" if pending else "live"
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
            store_delivery = default_delivery_text(
                slug, content.get("product_name", "your order")
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
                "status": store_status,
                "product_name": content.get("product_name", ""),
                "amount_usdt": content.get("price_usdt", 0),
                "pay_to": addr,
                "delivery": store_delivery,
                "description": desc,
                "content": content,
                "theme": theme_file,
            }
        else:
            _write_store_pages(d, content, addr, slug, theme_file, sandbox=sandbox)
            meta = {
                "slug": slug,
                "status": store_status,
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
                # First write wins. A merchant with several stores set this once;
                # a later create that omits it must not silently clear the only
                # channel we have to reach them.
                if notify_agent_id and merchant.contact_agent_id is None:
                    merchant.contact_agent_id = notify_agent_id
                store = Store(
                    slug=slug,
                    merchant_id=merchant.id,
                    status=store_status,
                    pay_to=addr,
                    visibility="hidden" if sandbox else "public",
                    manage_key_hash=manage_key_hash,
                    # In the SAME transaction as the store, never patched on after
                    # the commit. A store that committed with its key unrecorded is
                    # the exact defect this feature exists to prevent: the caller's
                    # retry would find no match, generate a second store and settle a
                    # second payment. Both stay NULL unless a paid caller sent an
                    # Idempotency-Key AND the middleware verified a payer to scope it
                    # to (main._idempotency_scope).
                    create_idempotency_addr=idempotency_addr,
                    create_idempotency_key=idempotency_key,
                    # Same transaction, same reason: the middleware settles AFTER this
                    # commit, so the nonce has to be on the row before the payment can
                    # fail. A nonce written after the commit leaves a window in which a
                    # settle failure cannot find the store it created — the very free
                    # store the compensator exists to quarantine.
                    create_x402_nonce=x402_nonce,
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
                # Real fulfilment, attached at birth. Without a Deliverable row a
                # buy can only hand back `store.delivery` text; with one, the
                # delivered order mints an Entitlement and the buy response carries
                # a signed download_url or a licence key. A fresh store has no prior
                # rows, so there is no version bump and nothing to deactivate — the
                # multi-version path stays in the manage-key upload endpoint, which
                # is also the only route that can accept a FILE (multipart cannot
                # ride this JSON create call).
                if deliverable:
                    session.add(
                        Deliverable(
                            store_id=store.id,
                            # NULL = the store-level default, which is what the
                            # upload endpoint writes when no product is named and
                            # what _active_deliverable falls back to. Binding this
                            # to the primary product instead would serve nobody: an
                            # order carrying no product_id would match no
                            # deliverable and silently fall back to text.
                            product_id=None,
                            kind=deliverable["kind"],
                            payload=deliverable.get("payload"),
                            max_activations=deliverable.get("max_activations"),
                            version=1,
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
            # NOT every IntegrityError here is a slug collision. Since the (payer,
            # Idempotency-Key) unique index exists, two requests carrying one key can
            # both clear the pre-check and both reach this insert — the normal case,
            # not an exotic one: a live create runs ~15s and agent clients give up
            # around 10s and retry. The loser trips the idempotency index, and
            # treating that as a slug collision would rmtree a directory that, in the
            # window where the loser resolved the SAME slug (the winner had not
            # committed yet when unique_slug ran), holds the WINNER'S committed, paid
            # store files: buyer charged, store URL 404. So ask the DB which it was.
            replay = _idempotency_holder(idempotency_addr, idempotency_key)
            if replay is not None:
                # The key already owns a store. Do not re-slug (a second store is the
                # thing being prevented) and do not touch the winner's directory. A
                # different slug means these files are ours alone and no DB row will
                # ever reference them, so they go — an orphan directory would serve a
                # rendered storefront that no store row backs.
                if replay != slug:
                    shutil.rmtree(d, ignore_errors=True)
                raise IdempotentReplay(replay) from None
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
            "status": store_status,
            "store_name": content.get("store_name", ""),
            "manage_key": manage_key,
            "recovery": SANDBOX_RECOVERY_NOTE if sandbox else RECOVERY_NOTE,
            "message": (
                "Sample created without a merchant payout address; it is hidden and "
                "non-payable."
                if sandbox
                else "Store queued: content screening is temporarily unavailable. "
                "It will not go live until screening completes."
            ),
            **({"visibility": "hidden", "payable": False} if sandbox else {}),
        }
    return {
        "slug": slug,
        "store_name": receipt_content.get("store_name", ""),
        "url": f"https://tilla.gudman.xyz/s/{slug}/",
        "product_name": receipt_content.get("product_name", ""),
        "price_usdt": receipt_content.get("price_usdt", 0),
        "manage_key": manage_key,
        "recovery": SANDBOX_RECOVERY_NOTE if sandbox else RECOVERY_NOTE,
        **(
            {"status": "sandbox", "visibility": "hidden", "payable": False}
            if sandbox
            else {}
        ),
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
    _write_store_pages(
        d,
        content,
        store.pay_to,
        store.slug,
        theme_file,
        sandbox=store.status == "sandbox",
    )
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
    and must be re-created or re-imported to recover its render inputs.

    Sandbox stores are re-rendered too. They have a served index.html exactly like a
    live store, so excluding them meant a theme fix could never reach the pages that
    needed it most — the sample stores are the ones that must say they cannot take
    payments. pending_screening stores stay out: they deliberately have no
    index.html, and writing one would open a checkout that screening has not cleared."""
    rendered = skipped = 0
    with SessionLocal() as session:
        stores = session.scalars(
            select(Store).where(Store.status.in_(("live", "sandbox")))
        ).all()
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
                _write_store_pages(
                    d,
                    content,
                    store.pay_to,
                    store.slug,
                    store.theme,
                    sandbox=store.status == "sandbox",
                )
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
