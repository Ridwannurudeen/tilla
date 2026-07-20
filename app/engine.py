#!/usr/bin/env python3
"""Tilla P1 store engine: prompt -> generated brand+content -> premium live store.
Usage: python3 make_store.py "I sell a Notion productivity template for $9" [receive_address]
Env: TILLA_LLM_KEY (Anthropic), TILLA_LLM_MODEL (optional), TILLA_STORES_DIR (default /opt/tilla/stores)
"""

import os
import re
import sys
import json
import pathlib
import unicodedata
import requests

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
KEY = os.environ.get("TILLA_LLM_KEY", "")
MODEL = os.environ.get("TILLA_LLM_MODEL") or "claude-haiku-4-5"
STORES_DIR = pathlib.Path(os.environ.get("TILLA_STORES_DIR", "/opt/tilla/stores"))
THEMES_DIR = pathlib.Path(__file__).resolve().parent.parent / "themes"
DEFAULT_ADDR = "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"  # demo receive address

TEMPLATE = (THEMES_DIR / "original.html").read_text(encoding="utf-8")


def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return (s or "store")[:40]


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
    return json.loads(m.group(0))


def render(c, addr, slug):
    pal = c.get("palette", {}) or {}
    repl = {
        "{{SLUG}}": slug,
        "{{STORE_NAME}}": str(c.get("store_name", "My Store")),
        "{{TAGLINE}}": str(c.get("tagline", "")),
        "{{HERO_HEADLINE}}": str(c.get("hero_headline", "")),
        "{{HERO_SUBCOPY}}": str(c.get("hero_subcopy", "")),
        "{{PRODUCT_NAME}}": str(c.get("product_name", "")),
        "{{PRODUCT_BLURB}}": str(c.get("product_blurb", "")),
        "{{CTA_TEXT}}": str(c.get("cta_text", "Buy now")),
        "{{PRICE}}": str(c.get("price_usdt", 0)),
        "{{EMOJI}}": str(c.get("emoji", "🛍️")),
        "{{ADDR}}": addr,
        "{{C_PRIMARY}}": pal.get("primary", "#6C5CE7"),
        "{{C_ACCENT}}": pal.get("accent", "#00D1B2"),
        "{{C_BG}}": pal.get("bg", "#0B0B12"),
        "{{C_TEXT}}": pal.get("text", "#F5F5FA"),
    }
    html = TEMPLATE
    for k, v in repl.items():
        html = html.replace(k, v)
    return html


def deploy(slug, html):
    d = STORES_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(html, encoding="utf-8")
    return d / "index.html"


def create_store(desc, addr=None, delivery=None):
    """Full pipeline: prompt -> generate -> render -> deploy -> store.json. Returns dict."""
    addr = addr or DEFAULT_ADDR
    c = generate(desc)
    slug = slugify(c.get("store_name", desc))
    html = render(c, addr, slug)
    deploy(slug, html)
    if delivery is None:
        delivery = (
            f"✅ Thank you! Your {c.get('product_name', 'product')} is ready: "
            f"https://tilla.gudman.xyz/files/{slug} (demo delivery link)"
        )
    meta = {
        "slug": slug,
        "product_name": c.get("product_name", ""),
        "amount_usdt": c.get("price_usdt", 0),
        "pay_to": addr,
        "delivery": delivery,
    }
    (STORES_DIR / slug / "store.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2)
    )
    return {
        "slug": slug,
        "store_name": c.get("store_name", ""),
        "url": f"https://tilla.gudman.xyz/s/{slug}/",
        "product_name": c.get("product_name", ""),
        "price_usdt": c.get("price_usdt", 0),
    }


def main():
    if len(sys.argv) < 2:
        print("usage: make_store.py '<what you sell>' [receive_address] [delivery]")
        sys.exit(1)
    if not KEY:
        print("ERROR: TILLA_LLM_KEY not set")
        sys.exit(1)
    addr = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_ADDR
    delivery = sys.argv[3] if len(sys.argv) > 3 else None
    print(f"[*] generating store from: {sys.argv[1]!r}")
    r = create_store(sys.argv[1], addr, delivery)
    print(f"[*] deployed -> {r['url']}")
    print("RESULT:", json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
