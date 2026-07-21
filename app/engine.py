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
import sys
import unicodedata

import requests
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import config, screening
from app.db import SessionLocal
from app.delivery import mint_manage_key
from app.models import Product, Store, get_or_create_merchant, log_event
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
    emoji: str = Field(default="🛍️", max_length=8)
    palette: dict = Field(default_factory=dict)
    # The LLM's theme suggestion (used only when the caller didn't pick one). A
    # value outside the allowed set is coerced to the default so a stray
    # suggestion can never fail generation.
    theme: str = Field(default="original")

    @field_validator("theme")
    @classmethod
    def _coerce_theme(cls, v):
        return v if v in config.ALLOWED_THEMES else "original"


def _resolve_theme(name: str | None) -> str:
    """Map a short theme name (API- or LLM-supplied) to its template filename,
    falling back to the default for anything outside the allowed set."""
    return f"{name}.html" if name in config.ALLOWED_THEMES else config.DEFAULT_THEME


def _write_store_pages(d, content: dict, addr: str, slug: str, theme: str) -> None:
    """Write the two nginx-served static assets for a live store: index.html
    (the chosen theme) and og.svg (the Open Graph card referenced from its head)."""
    (d / "index.html").write_text(
        render_theme(content, addr, slug, theme), encoding="utf-8"
    )
    (d / "og.svg").write_text(render_og(content, slug), encoding="utf-8")


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


def generate(desc):
    prompt = (
        "You are a world-class brand designer and DTC copywriter. A solo entrepreneur wants to sell "
        "something. Turn their description into a polished one-product storefront.\n\n"
        f'Merchant description: "{desc}"\n\n'
        "Output ONLY valid JSON (no markdown) with EXACTLY these keys: "
        "store_name (short brand), tagline (<=6 words), hero_headline (punchy, <=8 words), "
        "hero_subcopy (1 sentence), product_name, product_blurb (1-2 sentences, benefit-led), "
        "cta_text (<=4 words), price_usdt (number), emoji (single emoji for the brand), "
        "palette (object: primary, accent, bg, text as hex colors — modern, high-contrast, premium), "
        "theme (one of exactly: original, bold, editorial — pick the layout that best fits the brand: "
        "original = sleek modern gradient, bold = high-energy uppercase, editorial = elegant serif). "
        "Make copy crisp and compelling, no placeholders."
    )
    r = requests.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    r.raise_for_status()
    text = r.json()["content"][0]["text"]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("LLM did not return JSON: " + text[:200])
    raw = json.loads(m.group(0))
    data = GeneratedContent.model_validate(raw).model_dump()
    # A missing price_usdt defaults to 0 (pydantic doesn't validate defaults);
    # a 0 amount would make checkout auto-confirm with no payment. Clamp any
    # non-positive price up to a sane floor before it can reach a live store.
    if data["price_usdt"] <= 0:
        logger.warning(
            "generated content had no valid price; coercing to %.2f USDT",
            MIN_PRICE_USDT,
        )
        data["price_usdt"] = MIN_PRICE_USDT
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
    theme_file = _resolve_theme(theme or content.get("theme"))
    status = screening.scan_with_retry(_screening_text(desc, content))
    price_micro = int(round(float(content.get("price_usdt", 0)) * 1e6))
    pending = status == "pending"
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
                session.add(
                    Product(
                        store_id=store.id,
                        name=content.get("product_name", ""),
                        price_micro=price_micro,
                        active=True,
                    )
                )
                log_event(
                    session,
                    "api",
                    "store.created",
                    store_id=store.id,
                    data={"slug": slug},
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
                status = screening.scan_with_retry(
                    _screening_text(store.description or "", content)
                )
            except screening.ScreeningBlocked:
                logger.warning("resume_pending: %s blocked by screening", store.slug)
                store.status = "blocked"
                log_event(session, "resume", "store.blocked", store_id=store.id)
                session.commit()
                shutil.rmtree(STORES_DIR / store.slug, ignore_errors=True)
                continue
            if status != "allow":
                continue
            d = STORES_DIR / store.slug
            d.mkdir(parents=True, exist_ok=True)
            _write_store_pages(d, content, store.pay_to, store.slug, store.theme)
            _mark_store_json_live(d, store.slug)
            store.status = "live"
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
