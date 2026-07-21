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

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app import checkout, config, engine, screening
from app.db import get_session
from app.engine import GenerationUnavailable
from app.limiter import limiter
from app.models import (
    AffiliateAccrual,
    EmailSubscriber,
    EventLog,
    GrowthDraft,
    Order,
    Store,
    log_event,
)

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


def _build_prompt(store: Store, slug: str, perf_block: str = "") -> str:
    """Build the generation prompt from the persisted, already-screened content
    only — no request body ever reaches the LLM. ``perf_block`` (M17.3) is an
    optional compact FIRST-PARTY performance summary (own-DB numbers only, from
    17.2's aggregates); no buyer-supplied text or fetched content ever enters."""
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
        f"Store URL: {url}\n"
        f"{perf_block}\n"
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
    _fan_kit_into_outbox(session, store, kit)
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


# ===================================================================== outbox
# The draft outbox (M17.1). Every draft is queued copy a human copies out and
# posts themselves — INV-1: this module sends NOTHING. ``mark-published`` only
# records that the human already posted it. See test_no_outbound_posting_grep.


def _fan_kit_into_outbox(session: Session, store: Store, kit: GrowthKit) -> None:
    """Fan the already-screened kit into ``growth_drafts`` rows (additive to the
    unchanged kit response). The whole kit passed ``screening.screen`` fail-closed
    in the caller before this runs, so every row is stored ``screening_status='allow'``
    / ``status='pending'``; the ``content_sha256`` taken here is the tamper anchor
    that makes an approved draft immutable."""
    items = [("social", body) for body in kit.social_posts]
    items.append(("launch_tweet", kit.launch_tweet))
    items.append(("email_subject", kit.email_subject))
    for channel, body in items:
        session.add(
            GrowthDraft(
                store_id=store.id,
                channel=channel,
                body=body,
                source="manual",
                status="pending",
                content_sha256=hashlib.sha256(body.encode()).hexdigest(),
                screening_status="allow",
            )
        )


def _draft_view(draft: GrowthDraft) -> dict:
    """Serialize a draft for a read path. A ``blocked`` draft's body is withheld —
    unscreened/blocked copy never leaves through any read path."""
    return {
        "id": draft.id,
        "channel": draft.channel,
        "body": None if draft.status == "blocked" else draft.body,
        "source": draft.source,
        "status": draft.status,
        "content_sha256": draft.content_sha256,
        "screening_status": draft.screening_status,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "approved_at": draft.approved_at.isoformat() if draft.approved_at else None,
        "published_at": draft.published_at.isoformat() if draft.published_at else None,
        "performance_note": draft.performance_note,
    }


def _load_owned_draft(
    request: Request, slug: str, draft_id: int, session: Session
) -> tuple[Store, GrowthDraft]:
    """Same merchant/manage-key gate as the kit, then scope the draft to the store —
    a draft belonging to another store 404s (IDOR-blocked), never leaks."""
    store = _load_owned_live_store(request, slug, session)
    draft = session.scalar(
        select(GrowthDraft).where(
            GrowthDraft.id == draft_id, GrowthDraft.store_id == store.id
        )
    )
    if draft is None:
        raise HTTPException(404, "draft not found")
    return store, draft


class MarkPublished(BaseModel):
    """``mark-published`` records a human's out-of-band post. ``url`` is an optional
    receipt link — it triggers no send."""

    model_config = ConfigDict(extra="forbid")

    url: Annotated[str, Field(max_length=500)] | None = None


@router.get("/api/stores/{slug}/growth/outbox")
def growth_outbox_list(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """List the store's draft outbox (merchant-gated, blocked bodies withheld)."""
    store = _load_owned_live_store(request, slug, session)
    drafts = session.scalars(
        select(GrowthDraft)
        .where(GrowthDraft.store_id == store.id)
        .order_by(GrowthDraft.id.desc())
    ).all()
    return JSONResponse(
        {"drafts": [_draft_view(d) for d in drafts]}, headers=_KIT_HEADERS
    )


@router.post("/api/stores/{slug}/growth/outbox/{draft_id}/approve")
def growth_outbox_approve(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    draft_id: int = Path(..., ge=1),
    session: Session = Depends(get_session),
):
    """Flip a pending draft to approved (status-only; the body/hash are immutable)."""
    _, draft = _load_owned_draft(request, slug, draft_id, session)
    now = datetime.now(timezone.utc)
    changed = session.execute(
        update(GrowthDraft)
        .where(GrowthDraft.id == draft.id, GrowthDraft.status == "pending")
        .values(status="approved", approved_at=now)
    ).rowcount
    if not changed:
        raise HTTPException(409, "draft is not pending")
    session.commit()
    session.refresh(draft)
    return JSONResponse(_draft_view(draft), headers=_KIT_HEADERS)


@router.post("/api/stores/{slug}/growth/outbox/{draft_id}/discard")
def growth_outbox_discard(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    draft_id: int = Path(..., ge=1),
    session: Session = Depends(get_session),
):
    """Discard a pending or approved draft (never a published one)."""
    _, draft = _load_owned_draft(request, slug, draft_id, session)
    changed = session.execute(
        update(GrowthDraft)
        .where(
            GrowthDraft.id == draft.id,
            GrowthDraft.status.in_(("pending", "approved")),
        )
        .values(status="discarded")
    ).rowcount
    if not changed:
        raise HTTPException(409, "draft cannot be discarded")
    session.commit()
    session.refresh(draft)
    return JSONResponse(_draft_view(draft), headers=_KIT_HEADERS)


@router.post("/api/stores/{slug}/growth/outbox/{draft_id}/mark-published")
def growth_outbox_mark_published(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    draft_id: int = Path(..., ge=1),
    payload: MarkPublished = Body(default_factory=MarkPublished),
    session: Session = Depends(get_session),
):
    """RECORD that the human posted an approved draft (INV-1: sends nothing). The
    optional ``url`` is stored as the receipt — no outbound call is ever made."""
    _, draft = _load_owned_draft(request, slug, draft_id, session)
    now = datetime.now(timezone.utc)
    changed = session.execute(
        update(GrowthDraft)
        .where(GrowthDraft.id == draft.id, GrowthDraft.status == "approved")
        .values(status="published", published_at=now, performance_note=payload.url)
    ).rowcount
    if not changed:
        raise HTTPException(409, "draft is not approved")
    session.commit()
    session.refresh(draft)
    return JSONResponse(_draft_view(draft), headers=_KIT_HEADERS)


# ============================================================ performance (M17.2)
# Read-only, owner-gated, pure-DB aggregates over data Tilla already holds — the
# feedback input for the 17.3 content calendar. NO RPC, NO LLM, NO new writes, and
# NO buyer PII (a referrer wallet is only ever emitted in a truncated display form).

_MAX_PERFORMANCE_DAYS = 90
_DEFAULT_PERFORMANCE_DAYS = 30


def _display_addr(addr: str | None) -> str | None:
    """Truncated display form of an EVM address (0x1234…cdef) — never the full
    wallet, so the performance surface leaks no complete counterparty address."""
    if not addr:
        return None
    return addr[:6] + "…" + addr[-4:] if len(addr) >= 12 else addr


def store_performance(session: Session, store: Store, days: int) -> dict:
    """First-party performance aggregates for ``store`` over the last ``days``:
    delivered orders/revenue by day, affiliate-attributed sales + top referrers,
    waitlist growth, and the kit/draft history. Pure DB reads — every number comes
    from rows Tilla already persisted; no buyer email or full wallet is emitted."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Delivered orders + revenue, bucketed by calendar day (SQLite date()).
    day_col = func.date(Order.created_at)
    day_rows = session.execute(
        select(
            day_col.label("d"),
            func.count(Order.id),
            func.coalesce(func.sum(Order.expected_micro), 0),
        )
        .where(
            Order.store_id == store.id,
            Order.status.in_(checkout.TERMINAL_DELIVERED),
            Order.created_at >= since,
        )
        .group_by(day_col)
        .order_by(day_col)
    ).all()
    orders_by_day = [
        {"date": d, "orders": int(c), "revenue_micro": int(rev)}
        for d, c, rev in day_rows
    ]
    total_orders = sum(r["orders"] for r in orders_by_day)
    total_revenue_micro = sum(r["revenue_micro"] for r in orders_by_day)

    # Affiliate-attributed sales + top referrers (accrued OR paid, not voided).
    aff_rows = session.execute(
        select(
            AffiliateAccrual.referrer_addr,
            func.count(AffiliateAccrual.id),
            func.coalesce(func.sum(AffiliateAccrual.accrued_micro), 0),
        )
        .where(
            AffiliateAccrual.store_id == store.id,
            AffiliateAccrual.status.in_(("accrued", "paid")),
            AffiliateAccrual.created_at >= since,
        )
        .group_by(AffiliateAccrual.referrer_addr)
        .order_by(func.sum(AffiliateAccrual.accrued_micro).desc())
    ).all()
    top_referrers = [
        {
            "referrer": _display_addr(addr),
            "sales": int(cnt),
            "accrued_micro": int(micro),
        }
        for addr, cnt, micro in aff_rows
    ]

    # Waitlist growth (active, non-removed): total held + new inside the window.
    waitlist_total = int(
        session.scalar(
            select(func.count())
            .select_from(EmailSubscriber)
            .where(
                EmailSubscriber.store_id == store.id,
                EmailSubscriber.source == "waitlist",
                EmailSubscriber.removed_at.is_(None),
            )
        )
        or 0
    )
    waitlist_new = int(
        session.scalar(
            select(func.count())
            .select_from(EmailSubscriber)
            .where(
                EmailSubscriber.store_id == store.id,
                EmailSubscriber.source == "waitlist",
                EmailSubscriber.removed_at.is_(None),
                EmailSubscriber.created_at >= since,
            )
        )
        or 0
    )

    # Kit/draft history: growth.* event_log rows in the window, counted by event.
    event_rows = session.execute(
        select(EventLog.event, func.count(EventLog.id))
        .where(
            EventLog.store_id == store.id,
            EventLog.event.like("growth.%"),
            EventLog.ts >= since,
        )
        .group_by(EventLog.event)
    ).all()
    kit_events = {event: int(cnt) for event, cnt in event_rows}

    # Draft outbox counts by status (all-time — the outbox is the working queue).
    draft_rows = session.execute(
        select(GrowthDraft.status, func.count(GrowthDraft.id))
        .where(GrowthDraft.store_id == store.id)
        .group_by(GrowthDraft.status)
    ).all()
    drafts_by_status = {status: int(cnt) for status, cnt in draft_rows}

    return {
        "window_days": days,
        "orders": {
            "total": total_orders,
            "revenue_micro": total_revenue_micro,
            "by_day": orders_by_day,
        },
        "affiliates": {
            "attributed_sales": sum(r["sales"] for r in top_referrers),
            "accrued_micro": sum(r["accrued_micro"] for r in top_referrers),
            "top_referrers": top_referrers,
        },
        "waitlist": {"total": waitlist_total, "new_in_window": waitlist_new},
        "growth_activity": {
            "events": kit_events,
            "drafts_by_status": drafts_by_status,
        },
    }


@router.get("/api/stores/{slug}/growth/performance")
def growth_performance(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    days: int = Query(_DEFAULT_PERFORMANCE_DAYS, ge=1, le=_MAX_PERFORMANCE_DAYS),
    session: Session = Depends(get_session),
):
    """Owner-gated, read-only performance readback over a bounded window (days≤90).
    Same auth seam as the growth kit/outbox; no LLM, no RPC, no writes."""
    store = _load_owned_live_store(request, slug, session)
    return JSONResponse(store_performance(session, store, days), headers=_KIT_HEADERS)


# ========================================================= content calendar (M17.3)
# A merchant sets a cadence + channels; a flag-gated background scheduler drafts
# performance-aware copy on that cadence, screens it fail-closed, and queues it in
# the outbox (INV-1: it still sends NOTHING). The plan is the latest event_log row —
# no new table. Scheduled generation reuses the exact _build_prompt/_generate_kit and
# screening seams as the on-demand kit.

# The channels a calendar may schedule. 17.4 widens the outbox channel set, but the
# scheduler drafts only from the kit's own channels.
SCHEDULED_CHANNELS = ("social", "email_subject")


def _performance_block(perf: dict) -> str:
    """A compact FIRST-PARTY performance line for the generation prompt — numbers
    only, straight from 17.2's own-DB aggregates. No buyer text, no PII."""
    orders = perf.get("orders", {})
    waitlist = perf.get("waitlist", {})
    affiliates = perf.get("affiliates", {})
    revenue = int(orders.get("revenue_micro", 0)) / 1_000_000
    return (
        f"Recent performance (first-party, last {perf.get('window_days', 0)}d): "
        f"{int(orders.get('total', 0))} orders, {revenue:g} USDT revenue; "
        f"waitlist {int(waitlist.get('total', 0))} "
        f"(+{int(waitlist.get('new_in_window', 0))} new); "
        f"affiliate-attributed sales {int(affiliates.get('attributed_sales', 0))}. "
        "Let these numbers inform the angle; do NOT quote them verbatim."
    )


class CalendarPlan(BaseModel):
    """The merchant's content-calendar plan. Strict + tiny: a cadence, the channels
    to schedule (a subset of the kit's channels), and an active switch. Stored as the
    latest ``growth.calendar_set`` event_log row (no new table)."""

    model_config = ConfigDict(extra="forbid")

    cadence: Literal["daily", "weekly"]
    channels: Annotated[
        list[Literal["social", "email_subject"]], Field(min_length=1, max_length=2)
    ]
    active: bool = True

    @field_validator("channels")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("channels must be unique")
        return v


def latest_calendar(session: Session, store_id: int) -> dict | None:
    """The store's current calendar plan (latest row wins), or None if never set."""
    event = session.scalar(
        select(EventLog)
        .where(
            EventLog.store_id == store_id,
            EventLog.event == "growth.calendar_set",
        )
        .order_by(EventLog.id.desc())
    )
    return event.data if event is not None and event.data else None


@router.post("/api/stores/{slug}/growth/calendar")
def growth_calendar_set(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    plan: CalendarPlan = Body(...),
    session: Session = Depends(get_session),
):
    """Owner-gated: set the store's content-calendar plan (latest row wins). Records
    intent only — the scheduler stays dormant until TILLA_GROWTH_SCHED_ENABLED and
    sends nothing regardless."""
    store = _load_owned_live_store(request, slug, session)
    data = plan.model_dump()
    log_event(session, "growth", "growth.calendar_set", store_id=store.id, data=data)
    session.commit()
    return JSONResponse(data, headers=_KIT_HEADERS)


@router.get("/api/stores/{slug}/growth/calendar")
def growth_calendar_get(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    session: Session = Depends(get_session),
):
    """Owner-gated: read back the store's current calendar plan (404 if unset)."""
    store = _load_owned_live_store(request, slug, session)
    plan = latest_calendar(session, store.id)
    if plan is None:
        raise HTTPException(404, "no calendar set")
    return JSONResponse(plan, headers=_KIT_HEADERS)


def _scheduled_drafts_from_kit(
    kit: GrowthKit, channels: list[str]
) -> list[tuple[str, str]]:
    """Map a generated kit to (channel, body) drafts for the calendar's channels."""
    drafts: list[tuple[str, str]] = []
    if "social" in channels:
        drafts.extend(("social", post) for post in kit.social_posts)
    if "email_subject" in channels:
        drafts.append(("email_subject", kit.email_subject))
    return drafts


def screen_and_store_scheduled(
    session: Session, store: Store, channel: str, body: str
) -> str:
    """Screen one scheduled draft fail-closed, then persist it in the outbox with
    ``source='scheduled'``. Returns the disposition:

    - ``'allow'``   → a ``pending`` row (screened, readable);
    - ``'blocked'`` → a ``blocked`` row IS written (the body is stored but withheld
      from every read path by ``_draft_view``) — the M17.1-review binding that the
      screening-BLOCK path actually persists state, not silently drop it;
    - ``'unavailable'`` → NO row (screening was ambiguous/down): the M12 outage
      contract — skip, retry next tick, never serve unscreened copy.
    """
    try:
        outcome = screening.screen(body)
    except screening.ScreeningBlocked:
        session.add(
            GrowthDraft(
                store_id=store.id,
                channel=channel,
                body=body,
                source="scheduled",
                status="blocked",
                content_sha256=hashlib.sha256(body.encode()).hexdigest(),
                screening_status="blocked",
            )
        )
        return "blocked"
    if outcome.status != "allow":
        return "unavailable"
    session.add(
        GrowthDraft(
            store_id=store.id,
            channel=channel,
            body=body,
            source="scheduled",
            status="pending",
            content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            screening_status="allow",
        )
    )
    return "allow"


# ================================================= multi-channel draft shapes (M17.4)
# Two richer on-demand channels — a full email_body (subject + plain-text body) and a
# product_update blurb — through the EXACT generate → screen → outbox pipeline as the
# kit. Still nothing sends (INV-1). XSS-safe by construction: the shapes forbid HTML
# ('<'/'>') at validation, so an LLM that emits a tag folds into GenerationUnavailable
# → 503 (never a persisted draft), and the copy is only ever emitted as JSON.


def _reject_html(v: str) -> str:
    if "<" in v or ">" in v:
        raise ValueError("HTML markup is not allowed")
    return v


class EmailBody(BaseModel):
    """A full marketing email: a subject line + a plain-text body, no HTML."""

    model_config = ConfigDict(extra="forbid")

    subject: Annotated[str, Field(min_length=1, max_length=78)]
    body: Annotated[str, Field(min_length=1, max_length=2000)]

    _no_html = field_validator("subject", "body")(_reject_html)


class ProductUpdate(BaseModel):
    """A short product-update blurb, no HTML."""

    model_config = ConfigDict(extra="forbid")

    blurb: Annotated[str, Field(min_length=1, max_length=500)]

    _no_html = field_validator("blurb")(_reject_html)


# The on-demand channels 17.4 adds (kit channels stay on the kit endpoint).
_CHANNEL_INSTRUCTIONS = {
    "email_body": (
        "Output ONLY valid JSON (no markdown) with EXACTLY these keys: subject (an "
        "email subject line, at most 78 characters) and body (the plain-text email "
        "body, at most 2000 characters). Plain text ONLY — absolutely no HTML tags."
    ),
    "product_update": (
        "Output ONLY valid JSON (no markdown) with EXACTLY this key: blurb (a short "
        "product-update announcement, at most 500 characters). Plain text ONLY — "
        "absolutely no HTML tags."
    ),
}


def _channel_prompt(store: Store, slug: str, channel: str) -> str:
    """Build a channel-specific prompt from the persisted, already-screened store
    content only — the same injection-safe surface as ``_build_prompt``."""
    c = _content(store)
    url = f"{config.PUBLIC_BASE_URL.rstrip('/')}/s/{slug}/"
    return (
        "You are a growth marketer writing launch copy for a solo merchant's "
        "one-product storefront. Using ONLY the store details below, write the "
        "requested piece.\n\n"
        f"Store name: {c.get('store_name', '')}\n"
        f"Tagline: {c.get('tagline', '')}\n"
        f"Headline: {c.get('hero_headline', '')}\n"
        f"Subcopy: {c.get('hero_subcopy', '')}\n"
        f"Product: {c.get('product_name', '')}\n"
        f"Product description: {c.get('product_blurb', '')}\n"
        f"Price: {c.get('price_usdt', '')} USDT\n"
        f"Store URL: {url}\n\n" + _CHANNEL_INSTRUCTIONS[channel]
    )


def _generate_channel(channel: str, prompt: str) -> tuple[str, int, int]:
    """Generate + validate one channel's draft. Any outage, malformed body, schema/
    length violation, or HTML in the output raises GenerationUnavailable (→ 503) —
    never a partial or unsafe draft. Returns the draft body text + token counts."""
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
        if channel == "email_body":
            model = EmailBody.model_validate(raw)
            body = f"{model.subject}\n\n{model.body}"
        else:
            body = ProductUpdate.model_validate(raw).blurb
    except (json.JSONDecodeError, ValidationError) as exc:
        raise GenerationUnavailable(
            f"growth LLM returned unusable JSON: {exc}"
        ) from exc
    usage = resp.get("usage") or {}
    return (
        body,
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
    )


class ChannelDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Literal["email_body", "product_update"]


@router.post("/api/stores/{slug}/growth/draft")
@limiter.limit("6/hour")
def growth_channel_draft(
    request: Request,
    slug: str = Path(..., pattern=config.SLUG_PATTERN),
    payload: ChannelDraftRequest = Body(...),
    session: Session = Depends(get_session),
):
    """Generate one multi-channel draft (email_body | product_update) from the store's
    persisted content, screen it fail-closed, and queue it in the outbox. Merchant-
    gated, 6/hour, re-screened (BLOCK → 422, unavailable → 503). Sends nothing."""
    store = _load_owned_live_store(request, slug, session)
    channel = payload.channel
    try:
        body, _llm_in, _llm_out = _generate_channel(
            channel, _channel_prompt(store, slug, channel)
        )
    except GenerationUnavailable as exc:
        raise HTTPException(
            503,
            "growth draft generation temporarily unavailable — retry shortly",
            headers={"Retry-After": "60"},
        ) from exc
    try:
        outcome = screening.screen(body)
    except screening.ScreeningBlocked as exc:
        raise HTTPException(
            422, "generated draft did not pass safety screening"
        ) from exc
    if outcome.status != "allow":
        raise HTTPException(
            503,
            "content screening temporarily unavailable — retry shortly",
            headers={"Retry-After": "60"},
        )
    draft = GrowthDraft(
        store_id=store.id,
        channel=channel,
        body=body,
        source="manual",
        status="pending",
        content_sha256=hashlib.sha256(body.encode()).hexdigest(),
        screening_status="allow",
    )
    session.add(draft)
    log_event(
        session,
        "growth",
        "growth.channel_draft_generated",
        store_id=store.id,
        data={"channel": channel},
    )
    session.commit()
    session.refresh(draft)
    return JSONResponse(_draft_view(draft), headers=_KIT_HEADERS)
