"""Phase 3 AI growth agent — a minimal, real slice.

`POST` + `GET /api/stores/{slug}/growth-kit`: a merchant-gated endpoint that turns a
live store's already-screened content into a small, ready-to-copy marketing kit
(three social posts, a launch tweet, an email subject line). It reuses the exact
create-store LLM + screening seams and adds NO fund-moving or external-posting code.

Funds-safety / abuse invariants:
  (a) NO on-chain call, NO SMTP, NO social/webhook fan-out — the kit is generated,
      screened, and returned as JSON only. Publishing it is a user action outside
      the system (the M13 listings-are-user-owned rule).
  (b) The prompt is built EXCLUSIVELY from the persisted, Warden-screened
      ``store.content`` + the public store URL — there is no new user-controlled
      prompt surface, so the merchant cannot inject a fresh prompt via this route.
  (c) LLM outage / malformed / oversized output folds into GenerationUnavailable →
      503 + Retry-After (the proven create-store outage contract), never a 500 and
      never a truncated kit.
  (d) The generated kit is RE-SCREENED fail-closed before it is ever returned
      (BLOCK → 422, screening unavailable → 503), so unscreened text never leaves.
  (e) XSS-safe by construction: the kit is only ever emitted as JSON (nosniff),
      never rendered into any store page or template.

Persistence is the existing append-only ``event_log`` (``growth.kit_generated``),
which also serves the GET read-back for free — no new table, no schema change.
"""

from __future__ import annotations

import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app import config, engine, screening
from app.db import get_session
from app.engine import GenerationUnavailable
from app.limiter import limiter
from app.models import EventLog, Store, log_event

router = APIRouter()

# nosniff only: the kit is authenticated content, so it is never publicly cached
# (unlike the agent read surfaces). It is emitted as JSON in every case.
_KIT_HEADERS = {"X-Content-Type-Options": "nosniff"}

_ShortPost = Annotated[str, Field(min_length=1, max_length=280)]


class GrowthKit(BaseModel):
    """The strict shape the LLM must return. ``extra='forbid'`` + the length caps
    strait-jacket the output so a prompt-injected store can only ever yield a
    small, bounded, re-screened kit (or fold into a 503)."""

    model_config = ConfigDict(extra="forbid")

    social_posts: Annotated[list[_ShortPost], Field(min_length=3, max_length=3)]
    launch_tweet: Annotated[str, Field(min_length=1, max_length=280)]
    email_subject: Annotated[str, Field(min_length=1, max_length=78)]


def _content(store: Store) -> dict:
    return store.content if isinstance(store.content, dict) else {}


def _build_prompt(store: Store, slug: str) -> str:
    """Build the generation prompt from the persisted, already-screened content
    only — no request body ever reaches the LLM."""
    c = _content(store)
    url = f"{config.PUBLIC_BASE_URL.rstrip('/')}/s/{slug}/"
    return (
        "You are a growth marketer writing launch copy for a solo merchant's "
        "one-product storefront. Using ONLY the store details below, write a concise "
        "marketing kit.\n\n"
        f"Store name: {c.get('store_name', '')}\n"
        f"Tagline: {c.get('tagline', '')}\n"
        f"Headline: {c.get('hero_headline', '')}\n"
        f"Subcopy: {c.get('hero_subcopy', '')}\n"
        f"Product: {c.get('product_name', '')}\n"
        f"Product description: {c.get('product_blurb', '')}\n"
        f"Price: {c.get('price_usdt', '')} USDT\n"
        f"Store URL: {url}\n\n"
        "Output ONLY valid JSON (no markdown) with EXACTLY these keys: "
        "social_posts (array of EXACTLY 3 punchy social posts, each at most 280 "
        "characters), launch_tweet (one launch announcement tweet, at most 280 "
        "characters), email_subject (one email subject line, at most 78 characters). "
        "Make the copy compelling and on-brand. No placeholders, no hashtag spam."
    )


def _generate_kit(prompt: str) -> tuple[GrowthKit, int, int]:
    """Call the shared Anthropic seam and validate the output against GrowthKit.
    Any transient outage, malformed body, or schema/length violation raises
    GenerationUnavailable — the caller maps it to a 503 (never a 500, never a
    partial kit). Returns the kit plus its input/output token counts."""
    resp = engine._post_generation(prompt)
    try:
        text = resp["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GenerationUnavailable("growth LLM returned an unexpected shape") from exc
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise GenerationUnavailable("growth LLM did not return JSON: " + text[:200])
    try:
        raw = json.loads(m.group(0))
        kit = GrowthKit.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise GenerationUnavailable(
            f"growth LLM returned unusable JSON: {exc}"
        ) from exc
    usage = resp.get("usage") or {}
    return (
        kit,
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
    )


def _kit_text(kit: GrowthKit) -> str:
    return "\n".join([*kit.social_posts, kit.launch_tweet, kit.email_subject])


def _load_owned_live_store(request: Request, slug: str, session: Session) -> Store:
    """404 unknown, auth via the per-store manage key OR the owning merchant, then
    409 unless the store is live — the same seam/order as /add-product."""
    from app.main import _require_store_key

    store = session.scalar(select(Store).where(Store.slug == slug))
    if store is None:
        raise HTTPException(404, "store not found")
    _require_store_key(request, store, session)
    if store.status != "live":
        raise HTTPException(409, "store is not live")
    return store


@router.post("/api/stores/{slug}/growth-kit")
@limiter.limit("6/hour")
def growth_kit_post(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Generate a marketing kit from the store's persisted content. Merchant-gated,
    LLM-spend-capped at 6/hour, output re-screened fail-closed. Takes no body."""
    store = _load_owned_live_store(request, slug, session)
    try:
        kit, llm_in, llm_out = _generate_kit(_build_prompt(store, slug))
    except GenerationUnavailable as exc:
        raise HTTPException(
            503,
            "growth kit generation temporarily unavailable — retry shortly",
            headers={"Retry-After": "60"},
        ) from exc
    # Re-screen the generated copy fail-closed: an unscreened kit is never returned.
    try:
        outcome = screening.screen(_kit_text(kit))
    except screening.ScreeningBlocked as exc:
        raise HTTPException(422, "generated kit did not pass safety screening") from exc
    if outcome.status != "allow":
        raise HTTPException(
            503,
            "content screening temporarily unavailable — retry shortly",
            headers={"Retry-After": "60"},
        )
    data = kit.model_dump()
    log_event(session, "growth", "growth.kit_generated", store_id=store.id, data=data)
    session.commit()
    return JSONResponse(data, headers=_KIT_HEADERS)


@router.get("/api/stores/{slug}/growth-kit")
def growth_kit_get(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Return the most recently generated kit for the store (same auth), so a
    merchant/agent can re-fetch without re-spending tokens. 404 if none yet."""
    store = _load_owned_live_store(request, slug, session)
    event = session.scalar(
        select(EventLog)
        .where(
            EventLog.store_id == store.id,
            EventLog.event == "growth.kit_generated",
        )
        .order_by(EventLog.id.desc())
    )
    if event is None or not event.data:
        raise HTTPException(404, "no kit generated yet")
    return JSONResponse(event.data, headers=_KIT_HEADERS)
