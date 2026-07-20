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
from pydantic import BaseModel, Field

from app import config, screening
from app.render import render as render_theme

logger = logging.getLogger("tilla")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
KEY = os.environ.get("TILLA_LLM_KEY", "")
MODEL = os.environ.get("TILLA_LLM_MODEL") or "claude-haiku-4-5"
STORES_DIR = config.STORES_DIR
DEFAULT_ADDR = "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"  # demo receive address
# floor for a live store; a non-positive price would auto-confirm checkout
MIN_PRICE_USDT = 1.0
DEFAULT_THEME = "original.html"  # matches app.render.render's default


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


def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return (s or "store")[: config.SLUG_MAX_LEN]


def unique_slug(base: str) -> str:
    """Resolve `base` to a slug that is neither a reserved app route nor an
    existing store directory, appending a numeric suffix on collision. The
    base is truncated to leave room for the suffix so the final slug always
    stays within SLUG_MAX_LEN and matches SLUG_PATTERN (else checkout 422s)."""
    slug = base if base not in config.RESERVED_SLUGS else f"{base}-store"
    slug = slug[: config.SLUG_MAX_LEN]
    candidate = slug
    n = 2
    while (STORES_DIR / candidate).exists():
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
        "palette (object: primary, accent, bg, text as hex colors — modern, high-contrast, premium). "
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


def create_store(desc, addr=None, delivery=None):
    """Full pipeline: prompt -> generate -> screen -> render -> deploy -> store.json.
    Raises screening.ScreeningBlocked (fail-closed) if the content is unsafe.
    Returns dict; a screening-unavailable outcome returns a pending_screening
    dict with no live page deployed, instead of failing the request outright.
    """
    addr = addr or DEFAULT_ADDR
    content = generate(desc)
    slug = unique_slug(slugify(content.get("store_name") or desc))
    if delivery is None:
        delivery = (
            f"✅ Thank you! Your {content.get('product_name', 'product')} is ready: "
            f"https://tilla.gudman.xyz/files/{slug} (demo delivery link)"
        )

    status = screening.scan_with_retry(_screening_text(desc, content))

    d = STORES_DIR / slug
    d.mkdir(parents=True, exist_ok=True)

    if status == "pending":
        # Persist everything needed to resume (render + go live) once screening
        # recovers — but write no index.html, so checkout stays 409'd until then.
        meta = {
            "slug": slug,
            "status": "pending_screening",
            "product_name": content.get("product_name", ""),
            "amount_usdt": content.get("price_usdt", 0),
            "pay_to": addr,
            "delivery": delivery,
            "description": desc,
            "content": content,
            "theme": DEFAULT_THEME,
        }
        (d / "store.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "slug": slug,
            "status": "pending_screening",
            "store_name": content.get("store_name", ""),
            "message": (
                "Store queued: content screening is temporarily unavailable. "
                "It will not go live until screening completes."
            ),
        }

    html = render_theme(content, addr, slug)
    (d / "index.html").write_text(html, encoding="utf-8")
    meta = {
        "slug": slug,
        "status": "live",
        "product_name": content.get("product_name", ""),
        "amount_usdt": content.get("price_usdt", 0),
        "pay_to": addr,
        "delivery": delivery,
    }
    (d / "store.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "slug": slug,
        "store_name": content.get("store_name", ""),
        "url": f"https://tilla.gudman.xyz/s/{slug}/",
        "product_name": content.get("product_name", ""),
        "price_usdt": content.get("price_usdt", 0),
    }


def resume_pending():
    """Retry screening for every store left in pending_screening (e.g. after a
    process restart). On ALLOW: render + write index.html + flip to live. On
    BLOCK: remove the store. Still unavailable: leave it for the next attempt.
    Defensive by design — a broken store dir is logged and skipped, never fatal,
    so this can run safely at startup."""
    if not STORES_DIR.exists():
        return
    for d in sorted(STORES_DIR.iterdir()):
        meta_path = d / "store.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("status") != "pending_screening":
                continue
            content = meta.get("content")
            if not isinstance(content, dict):
                continue
            try:
                status = screening.scan_with_retry(
                    _screening_text(meta.get("description", ""), content)
                )
            except screening.ScreeningBlocked:
                logger.warning("resume_pending: %s blocked by screening", d.name)
                shutil.rmtree(d, ignore_errors=True)
                continue
            if status != "allow":
                continue
            addr = meta.get("pay_to", DEFAULT_ADDR)
            slug = meta.get("slug", d.name)
            theme = meta.get("theme", DEFAULT_THEME)
            html = render_theme(content, addr, slug, theme)
            (d / "index.html").write_text(html, encoding="utf-8")
            meta["status"] = "live"
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("resume_pending: %s screened clean, now live", d.name)
        except Exception:
            logger.exception("resume_pending: skipping broken store %s", d.name)


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
