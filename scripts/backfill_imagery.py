#!/usr/bin/env python3
"""One-off backfill: add photography to stores generated before app/imagery.py existed.

Deliberately NOT :func:`app.engine.upgrade_store`. That regenerates a store's copy,
which would rewrite the brand and blurbs of stores that already carry settled orders
and of the three cited in ``docs/PROOF-onchain.md``. This asks the model for image
text ONLY, derived from the copy each store already has, and leaves every word of that
copy — and every Product row and price — untouched.

"Image text" is search text plus, for a store selling something with no physical form,
a ``hero_art_prompt``: a generation-only atmosphere line that is never sent to the stock
provider (see ``app.imagery._generated_hero``). Those stores are exactly the ones a
search-only backfill leaves with nothing at all.

Usage (on the VPS, where the key and the stores live):
    /opt/tilla/.venv/bin/python -m scripts.backfill_imagery highland-roast
    /opt/tilla/.venv/bin/python -m scripts.backfill_imagery --all --dry-run

`--dry-run` stops after printing the search text the model produced, before anything
is fetched or written. A store that already has a hero photograph is skipped, so the
script is safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app import engine, imagery, screening
from app.db import SessionLocal
from app.models import Store

# The generation prompt carries the full rules for this text (product noun first, empty
# when nothing can honestly be photographed). They are restated here rather than
# imported because engine's prompt is one string built for a whole store; only the
# photography half applies to a backfill.
PROMPT = """You are writing stock-photography search text for an EXISTING online store.
Do not rewrite, improve or comment on the store's copy — it is already published and
must not change. Return ONLY search text.

Store: {store_name}
Tagline: {tagline}
Headline: {headline}
Products, in order:
{products}

Output ONLY valid JSON (no markdown) with EXACTLY these keys:
  hero_image_query: 2-6 words naming a real, photographable scene that shows what this
    brand sells, phrased the way a stock photographer would caption it.
  hero_image_subject: 2-4 concrete visible nouns from that scene, space-separated.
  lifestyle_queries: an array of 0 to 3 further photographable scenes, same style,
    showing the product in use or the world around it.
  products: an array with EXACTLY {count} objects, in the SAME ORDER as listed above,
    each with image_query and image_subject.
  hero_art_prompt: see the last paragraph. Empty string unless it applies.

A product's image_query must make THAT ITEM the main subject of the frame, not a
background detail — 'close up of black compression leggings', not 'woman at the gym'.
Do not add other products (shoes, phones, laptops, mugs) that would compete with it.
image_subject's FIRST word must be the item's own everyday noun ('leggings', 'scarf',
'candle', 'espresso') — never a material or generic word like 'fabric', 'product' or
'item'. Put the product noun first and supporting detail after it.

CRITICAL: if what is being sold cannot honestly be photographed — software, a template,
an ebook, a digital download, a subscription, a service, anything with no physical form
— return an EMPTY STRING for every image_query and image_subject and an EMPTY ARRAY for
lifestyle_queries. Never substitute a generic desk, laptop or office scene for a product
that has no photograph. Empty is correct and expected.

ONLY in that case, also return hero_art_prompt: one sentence describing an ATMOSPHERIC
scene to illustrate the brand's world, which must NOT depict the product, a screen, an
interface, or any text. Describe light, material, colour and place — e.g. 'morning light
across a clean oak desk with a linen notebook and a cup of black coffee'. No people's
faces, no logos, no signage. It is never searched for; it is drawn, and the page says so.
If the goods CAN be photographed, return an empty string for it."""


def ask_for_queries(content: dict) -> dict:
    """One model call: search text for this store's existing copy. Raises
    GenerationUnavailable on an outage (the caller skips that store)."""
    products = content.get("products") or []
    listing = "\n".join(
        f"  {i + 1}. {p.get('name', '')} — {p.get('blurb', '')}"
        for i, p in enumerate(products)
        if isinstance(p, dict)
    )
    resp = engine._post_generation(
        PROMPT.format(
            store_name=content.get("store_name", ""),
            tagline=content.get("tagline", ""),
            headline=content.get("hero_headline", ""),
            products=listing,
            count=len(products),
        )
    )
    text = resp["content"][0]["text"]
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise engine.GenerationUnavailable("model returned no JSON: " + text[:200])
    return json.loads(match.group(0))


def merge_queries(content: dict, raw: dict) -> dict:
    """Copy `content` with the model's search text merged in, validated and bounded.

    Returns a NEW dict; the store's persisted content is only replaced once the whole
    backfill for that store has succeeded.
    """
    merged = json.loads(json.dumps(content))  # deep copy, JSON-safe by construction
    merged["hero_image_query"] = str(raw.get("hero_image_query") or "")[:120]
    merged["hero_image_subject"] = str(raw.get("hero_image_subject") or "")[:120]
    merged["lifestyle_queries"] = [
        s[:120]
        for s in (raw.get("lifestyle_queries") or [])
        if isinstance(s, str) and s.strip()
    ][: imagery.MAX_LIFESTYLE]
    # Generation-only, and only meaningful when the hero query came back empty —
    # a photographable store keeps its photograph. Bounded to the same length the
    # GeneratedContent field declares.
    merged["hero_art_prompt"] = (
        str(raw.get("hero_art_prompt") or "")[:160]
        if not merged["hero_image_query"]
        else ""
    )
    supplied = raw.get("products") or []
    for i, product in enumerate(merged.get("products") or []):
        if not isinstance(product, dict):
            continue
        entry = (
            supplied[i] if i < len(supplied) and isinstance(supplied[i], dict) else {}
        )
        product["image_query"] = str(entry.get("image_query") or "")[:120]
        product["image_subject"] = str(entry.get("image_subject") or "")[:120]
    return merged


def describe(slug: str, content: dict) -> None:
    print(f"  hero     : {content['hero_image_query']!r}")
    if content.get("hero_art_prompt"):
        print(f"  hero art : {content['hero_art_prompt']!r} (generated, not searched)")
    print(f"  lifestyle: {content['lifestyle_queries']}")
    for product in content.get("products") or []:
        if isinstance(product, dict):
            print(
                f"  {str(product.get('name', ''))[:24]:24} "
                f"q={product.get('image_query')!r} subj={product.get('image_subject')!r}"
            )


def backfill(slug: str, dry_run: bool, force: bool = False) -> str:
    """Add photography to one store. Returns a one-line outcome for the summary."""
    with SessionLocal() as session:
        store = session.scalar(select(Store).where(Store.slug == slug))
        if store is None:
            return "not found"
        if store.status != "live":
            return f"skipped (status={store.status})"
        content = store.content if isinstance(store.content, dict) else None
        if content is None:
            return "skipped (no content)"
        if (content.get("imagery") or {}).get("hero") and not force:
            return "skipped (already photographed)"

        try:
            raw = ask_for_queries(content)
        except (engine.GenerationUnavailable, json.JSONDecodeError, KeyError) as exc:
            return f"skipped (model: {exc})"
        merged = merge_queries(content, raw)
        describe(slug, merged)
        if dry_run:
            return "dry run — nothing fetched or written"

        # The search text is newly model-generated, so it is screened on the same
        # fail-closed contract as any other generated content before it is used.
        # ScreeningBlocked propagates deliberately: a blocked store is a stop, not a
        # store to quietly photograph anyway.
        outcome = screening.screen(
            engine._screening_text(store.description or "", merged)
        )
        if outcome.status != "allow":
            return "skipped (screening unavailable)"
        # The receipt MUST be persisted, not just read for its verdict. With
        # TILLA_WARDEN_PAID=1 a screen settles a real x402 hire on-chain, and the
        # first version of this script dropped the receipt — 0.1 USDT0 moved
        # (0x8a6235b3…) with no row to account for it. Every other screening call
        # site records one; this one does too.
        engine._persist_receipt(session, store.id, outcome.receipt)

        store_dir = engine.STORES_DIR / slug
        store_dir.mkdir(parents=True, exist_ok=True)
        resolved = imagery.resolve(
            merged, store_dir, engine._mulberry32(engine._fnv1a(slug + ":imagery"))
        )
        imagery.apply(merged, resolved)
        photos = sum(1 for i in resolved.products if i is not None)
        if resolved.hero is None and not photos and not resolved.lifestyle:
            return "no photograph cleared the relevance bar — left as it was"

        store.content = merged
        flag_modified(store, "content")
        # store.json is the rollback copy create_store wrote; keep it in step without
        # disturbing the other keys it holds (pay_to, delivery, theme, status).
        meta_path = store_dir / "store.json"
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["content"] = merged
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        engine._write_store_pages(store_dir, merged, store.pay_to, slug, store.theme)
        session.commit()
        return (
            f"hero={'yes' if resolved.hero else 'no'} "
            f"products={photos}/{len(merged.get('products') or [])} "
            f"lifestyle={len(resolved.lifestyle)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slugs", nargs="*", help="stores to backfill")
    parser.add_argument("--all", action="store_true", help="every live store")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-resolve stores that already have photographs, so existing ones "
        "are re-checked against the current selection rules",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the search text and stop, before anything is fetched or written",
    )
    args = parser.parse_args()

    if not imagery.enabled():
        print("TILLA_IMAGE_KEY is not set — nothing to do.", file=sys.stderr)
        return 1
    slugs = list(args.slugs)
    if args.all:
        with SessionLocal() as session:
            slugs = list(
                session.scalars(
                    select(Store.slug).where(Store.status == "live").order_by(Store.id)
                )
            )
    if not slugs:
        parser.error("name at least one slug, or pass --all")

    results = {}
    for slug in slugs:
        print(f"\n[{slug}]")
        results[slug] = backfill(slug, args.dry_run, args.force)
        print(f"  -> {results[slug]}")
    print("\n=== summary ===")
    for slug, outcome in results.items():
        print(f"  {slug:22} {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
