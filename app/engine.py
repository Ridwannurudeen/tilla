#!/usr/bin/env python3
"""Tilla store engine: prompt -> generated brand+content -> premium live store.
Usage: python3 -m app.engine "I sell a Notion productivity template for $9" [receive_address]
Env: TILLA_LLM_KEY (Anthropic), TILLA_LLM_MODEL (optional), TILLA_STORES_DIR (default /opt/tilla/stores)
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
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

from app import config, providers, screening
from app.db import SessionLocal
from app.delivery import mint_manage_key
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

    @field_validator("*", mode="before")
    @classmethod
    def _coerce_axis(cls, v, info):
        field = cls.model_fields[info.field_name]
        return v if v in get_args(field.annotation) else field.default


class ProductContent(BaseModel):
    """One product in a store's catalog (docs multi-product). price_usdt allows 0
    here — generate() clamps any non-positive price to MIN_PRICE_USDT before it can
    reach a Product row (whose CHECK enforces price_micro > 0)."""

    name: str = Field(default="", max_length=120)
    blurb: str = Field(default="", max_length=400)
    price_usdt: float = Field(default=0, ge=0, le=10000)
    cta_text: str = Field(default="Buy now", max_length=40)


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
    design_dna: DesignDNA | None = None

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
    for i, item in enumerate(old):
        if not isinstance(item, dict):
            continue
        key = item.get("id")
        if key is None and i < len(active):
            key = active[i].id
        extras[key] = (str(item.get("blurb", "")), str(item.get("cta_text", "Buy now")))
    if extras_override:
        extras.update(extras_override)
    content["products"] = [
        {
            "id": p.id,
            "name": p.name,
            "price_usdt": p.price_micro / 1e6,
            "blurb": extras.get(p.id, ("", "Buy now"))[0],
            "cta_text": extras.get(p.id, ("", "Buy now"))[1],
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


def unique_slug(base: str) -> str:
    """Resolve `base` to a slug that is neither a reserved app route nor an
    existing store (on disk or in the DB), appending a numeric suffix on
    collision. The base is truncated to leave room for the suffix so the final
    slug always stays within SLUG_MAX_LEN and matches SLUG_PATTERN (else
    checkout 422s)."""
    slug = base if base not in config.RESERVED_SLUGS else f"{base}-store"
    slug = slug[: config.SLUG_MAX_LEN]
    candidate = slug
    n = 2
    while _slug_taken(candidate):
        suffix = f"-{n}"
        candidate = slug[: config.SLUG_MAX_LEN - len(suffix)] + suffix
        n += 1
    assert re.fullmatch(config.SLUG_PATTERN, candidate), candidate
    return candidate


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
        "model": MODEL,
        "max_tokens": 1024,
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
        "benefit-led), price_usdt (number), cta_text (<=4 words)) — a focused catalog of related "
        "items that fit the brand; use a single item when the merchant clearly sells one thing, "
        "otherwise 2 to 4 distinct items, "
        "emoji (single emoji for the brand), "
        "palette (object: primary, accent, bg, text as hex colors — modern, high-contrast, premium; "
        "bg must be decisively near-dark or near-light, never mid-gray, with text strongly contrasting it), "
        "theme (one of exactly: original, bold, editorial — pick the layout that best fits the brand: "
        "original = maximal flagship with kinetic type and a generative block mosaic, "
        "bold = loud brutalist uppercase with hard offset shadows, "
        "editorial = quiet numbered-ledger magazine, understated luxury), "
        "design_dna (object with EXACTLY these keys, each value one of the exact options listed: "
        "scale (one of exactly: compact, balanced, dramatic, monumental), "
        "weight (one of exactly: light, regular, heavy), "
        "rhythm (one of exactly: tight, roomy, airy), "
        "hero (one of exactly: stacked, split, offset), "
        "texture (one of exactly: sparse, medium, dense) "
        "— pick the combination that expresses the brand's personality: a loud, maximal brand "
        "wants heavy weight, dramatic or monumental scale, dense texture, offset hero; a refined, "
        "understated brand wants light weight, airy rhythm, sparse texture, stacked hero). "
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
    theme_file = _resolve_theme(theme or content.get("theme"))
    outcome = screening.screen(_screening_text(desc, content))
    pending = outcome.status == "pending"
    # Per-store capability secret returned ONCE to the paid caller (the store
    # owner by construction). Only its sha256 hash is persisted.
    manage_key, manage_key_hash = mint_manage_key()

    # Resolve the slug and write everything slug-dependent inside a short retry
    # loop: stores.slug is UNIQUE, so a concurrent create that grabbed the same
    # candidate between our check and insert raises IntegrityError — we clean up,
    # re-slug (now DB-aware, so it skips the committed row) and re-render once.
    for attempt in range(2):
        slug = unique_slug(slugify(content.get("store_name") or desc))
        store_delivery = delivery
        if store_delivery is None:
            store_delivery = (
                f"✅ Thank you! Your {content.get('product_name', 'product')} is ready: "
                f"https://tilla.gudman.xyz/files/{slug} (demo delivery link)"
            )

        d = STORES_DIR / slug
        d.mkdir(parents=True, exist_ok=True)

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
                            price_micro=max(
                                int(round(float(item.get("price_usdt", 0)) * 1e6)),
                                int(MIN_PRICE_USDT * 1e6),
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
            break
        except IntegrityError:
            shutil.rmtree(d, ignore_errors=True)
            if attempt == 1:
                raise

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
        "store_name": content.get("store_name", ""),
        "url": f"https://tilla.gudman.xyz/s/{slug}/",
        "product_name": content.get("product_name", ""),
        "price_usdt": content.get("price_usdt", 0),
        "manage_key": manage_key,
    }


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
    d = STORES_DIR / store.slug
    d.mkdir(parents=True, exist_ok=True)
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
