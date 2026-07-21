"""M17.3 content-calendar scheduler — the flag-gated background drafting loop.

For each live store with an ACTIVE calendar plan (17.3), one tick builds a
performance-aware prompt (17.2 aggregates, first-party numbers only), generates a
kit through the EXACT ``growth._generate_kit`` seam, screens every draft fail-closed,
and queues the calendar's channels in the outbox as ``source='scheduled'`` rows.

INV-1 (restated): this module sends NOTHING — it drafts, screens, and queues; a
human publishes. It matches the ``growth*.py`` glob so the no-outbound grep test
(``test_no_outbound_posting_grep``) covers it: no mail-send library, no social host,
and no outbound HTTP client is imported here — every draft is queued in the DB only.

Spend + safety gates (all off the M12 background-loop contract):
  - ``TILLA_GROWTH_SCHED_ENABLED`` default OFF  ⇒ the tick is a no-op, zero LLM spend.
  - per-store ``GROWTH_SCHED_STORE_DAILY_MAX`` scheduled generations/day (event_log
    count) + a global ``GROWTH_DAILY_MAX`` ceiling across all stores.
  - per-store cadence pacing (weekly ⇒ skip if a run happened in the last 7 days).
  - an LLM outage / screening outage on one store SKIPS that store, logs, and retries
    next tick — never a crash-loop, never a partial serve.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config, growth
from app.db import SessionLocal
from app.engine import GenerationUnavailable
from app.models import EventLog, Store, log_event

logger = logging.getLogger("tilla")


def _runs_since(session: Session, store_id: int, since: datetime) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(EventLog)
            .where(
                EventLog.store_id == store_id,
                EventLog.event == "growth.scheduled_run",
                EventLog.ts >= since,
            )
        )
        or 0
    )


def _global_runs_since(session: Session, since: datetime) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(EventLog)
            .where(
                EventLog.event == "growth.scheduled_run",
                EventLog.ts >= since,
            )
        )
        or 0
    )


def _active_calendars(session: Session) -> list[tuple[Store, dict]]:
    """Every live store whose LATEST calendar plan is active."""
    store_ids = session.scalars(
        select(EventLog.store_id)
        .where(EventLog.event == "growth.calendar_set")
        .distinct()
    ).all()
    out: list[tuple[Store, dict]] = []
    for sid in store_ids:
        if sid is None:
            continue
        plan = growth.latest_calendar(session, sid)
        if not plan or not plan.get("active"):
            continue
        store = session.get(Store, sid)
        if store is None or store.status != "live":
            continue
        out.append((store, plan))
    return out


def _cadence_ready(
    session: Session, store_id: int, cadence: str, now: datetime
) -> bool:
    """Weekly cadence draws once per 7 days; daily is paced by the daily loop tick."""
    if cadence == "weekly":
        return _runs_since(session, store_id, now - timedelta(days=7)) == 0
    return True


def _generate_for_store(session: Session, store: Store, plan: dict) -> bool:
    """Generate + screen + queue one store's scheduled drafts. Returns True iff a
    generation actually ran (an LLM call was made). Screening BLOCK still persists a
    withheld ``blocked`` row (the 17.1-review binding); screening outage persists
    nothing (skip, retry next tick)."""
    perf = growth.store_performance(session, store, 30)
    prompt = growth._build_prompt(store, store.slug, growth._performance_block(perf))
    try:
        kit, llm_in, llm_out = growth._generate_kit(prompt)
    except GenerationUnavailable:
        logger.warning("growth scheduler: generation unavailable for %s", store.slug)
        return False
    channels = list(plan.get("channels", []))
    dispositions: dict[str, int] = {"allow": 0, "blocked": 0, "unavailable": 0}
    for channel, body in growth._scheduled_drafts_from_kit(kit, channels):
        disp = growth.screen_and_store_scheduled(session, store, channel, body)
        dispositions[disp] += 1
    log_event(
        session,
        "growth",
        "growth.scheduled_run",
        store_id=store.id,
        data={
            "llm_input_tokens": llm_in,
            "llm_output_tokens": llm_out,
            "channels": channels,
            "dispositions": dispositions,
        },
    )
    session.commit()
    return True


def run_scheduler_tick(now: datetime | None = None) -> int:
    """One scheduler pass. Returns the number of stores that generated. A no-op when
    the flag is OFF (zero LLM spend). Each store is guarded by the per-store daily
    cap, the global daily ceiling, and its cadence; a failure on one store never
    aborts the tick."""
    if not config.GROWTH_SCHED_ENABLED:
        return 0
    now = now or datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    generated = 0
    with SessionLocal() as session:
        if _global_runs_since(session, day_start) >= config.GROWTH_DAILY_MAX:
            logger.warning("growth scheduler: global daily cap reached; skipping tick")
            return 0
        calendars = _active_calendars(session)
    for store, plan in calendars:
        try:
            with SessionLocal() as session:
                if _global_runs_since(session, day_start) >= config.GROWTH_DAILY_MAX:
                    break
                if (
                    _runs_since(session, store.id, day_start)
                    >= config.GROWTH_SCHED_STORE_DAILY_MAX
                ):
                    continue
                if not _cadence_ready(session, store.id, plan.get("cadence"), now):
                    continue
                store = session.get(Store, store.id)
                if store is None or store.status != "live":
                    continue
                if _generate_for_store(session, store, plan):
                    generated += 1
        except Exception:
            logger.exception("growth scheduler: tick failed for a store")
    return generated


async def growth_scheduler_loop() -> None:
    """Background loop (the webhook/attest-loop pattern): flag-gated, restart-safe,
    outage-tolerant. Never started in tests (gated behind SWEEP_ENABLED in main)."""
    logger.info(
        "growth scheduler loop started (interval=%ss)", config.GROWTH_SCHED_INTERVAL
    )
    while True:
        try:
            await asyncio.to_thread(run_scheduler_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("growth scheduler tick failed")
        await asyncio.sleep(config.GROWTH_SCHED_INTERVAL)
