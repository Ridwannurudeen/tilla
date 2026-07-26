# M17 — Autonomous full growth agent (VISION §4 → buildable spec)

**Status: BUILT AND SHIPPED** *(updated 2026-07-26 — this header said "SPEC"; the module has since been built, tested and deployed. See `docs/VISION.md` for the built state. The migration numbers quoted below are frozen at authoring time and are NOT current — the deployed head is `0030_creation_block_floor`.)* Originally derived from `docs/VISION.md` §4. The seed IS built and live:
`app/growth.py` — merchant-gated `POST`/`GET /api/stores/{slug}/growth-kit`,
prompt built EXCLUSIVELY from persisted screened `store.content`, strict
`GrowthKit` schema (`extra="forbid"`, 280/78 caps), re-screened fail-closed
(BLOCK→422, unavailable→503), 6/hour cap, persisted to append-only `event_log`
(`growth.kit_generated`). Every increment below grows FROM those exact seams.

**Standing rule (M13, restated as INV-1): PUBLISH IS PERMANENTLY A USER ACTION.**
No SMTP send, no social API call, no webhook fan-out of drafts, ever, in any
increment. The agent plans, drafts, measures, and queues; a human publishes.

## Threat model (LLM output headed outward)

| Threat | Defense | Test |
|---|---|---|
| Unscreened LLM copy reaches a human channel | Every draft — calendar-generated or on-demand — passes `screening.screen()` fail-closed BEFORE persistence in the outbox (BLOCK ⇒ draft stored as `blocked`, body withheld from all reads; unavailable ⇒ retry, never served). Same contract as `growth.py` today. | `test_draft_blocked_never_readable` |
| Prompt injection via store content steering copy | Prompt stays built ONLY from already-screened `store.content` + first-party performance numbers (own DB aggregates) — no fetched web content, no buyer-supplied text (order emails, waitlist addresses NEVER enter a prompt). Strict output schemas `extra="forbid"` per channel. | `test_prompt_excludes_buyer_data` |
| LLM spend runaway (scheduler = unattended spend) | Hard caps: per-store `6/day` scheduled generations + global `TILLA_GROWTH_DAILY_MAX` (default 50) enforced in the scheduler tick (event_log count check), on top of the existing 6/hour endpoint limiter; token spend logged per run (the M12 pattern). Scheduler is flag-gated `TILLA_GROWTH_SCHED_ENABLED`, default OFF. | `test_daily_cap_halts_tick` |
| IDOR on calendars/outbox/metrics | All new endpoints ride the existing seams only: `growth._load_owned_live_store` (manage-key or owning merchant) / `dashboard._require_merchant` + `_owned_store`. | `test_outbox_idor_blocked` |
| Approval forgery / approve-then-mutate | Drafts are immutable post-creation; `approve` flips status only; content hash recorded at approval; an edited draft is a NEW draft (`pending`). | `test_draft_immutable_after_approval` |
| "Autonomy theater" (claiming posting) | No external-credential config keys exist in this module at all; grep-assert in CI: no `smtplib`, no social hostnames, no `httpx.post` to non-Tilla hosts in `app/growth*.py`. | `test_no_outbound_posting_grep` |

## Increments

### 17.1 — Draft outbox (draft → approve, publish stays human) (M)
Migration `0013_growth_agent` (additive; renumber to next free head at build —
0010 pending elsewhere, M15/M16 take 0011/0012): table `growth_drafts` —
`id, store_id, channel ('social'|'email_subject'|'launch_tweet'), body,
source ('manual'|'scheduled'), status ('pending'|'blocked'|'approved'|'published'|
'discarded'), content_sha256, screening_status, created_at, approved_at,
published_at, performance_note`. `growth_kit_post` additionally fans its kit items
into rows (existing response unchanged — additive). New merchant endpoints in
`app/growth.py`: `GET /api/stores/{slug}/growth/outbox` (list, blocked bodies
withheld), `POST .../outbox/{id}/approve`, `POST .../outbox/{id}/discard`,
`POST .../outbox/{id}/mark-published` — mark-published records that the HUMAN
posted it (optional `url` field for the receipt), it sends nothing.

**Accept (binary):** claimed ONLY if migration up/down/up passes prod-shape;
existing `test_growth.py` green unchanged; new tests
`test_kit_fans_into_outbox`, `test_outbox_idor_blocked`,
`test_draft_blocked_never_readable`, `test_draft_immutable_after_approval`,
`test_mark_published_sends_nothing` (respx: zero outbound), grep test
`test_no_outbound_posting_grep`.

### 17.2 — Performance readback (closing the loop with data Tilla already has) (S/M)
`GET /api/stores/{slug}/growth/performance` (owner-gated): pure-DB aggregates —
orders/revenue by day (`Order`), affiliate-attributed sales + top referrers
(`AffiliateAccrual`, `app/affiliates.py`), waitlist growth (`EmailSubscriber`),
kit/draft history (`event_log` `growth.*` rows) — over a bounded window
(`?days≤90`). No RPC, no LLM, no new writes. This is the feedback input for 17.3.

**Accept:** `test_performance_shapes` (seeded fixtures → exact aggregates),
`test_performance_idor_blocked`, `test_performance_no_pii` (no buyer emails/
wallets beyond truncated display forms), full suite green.

### 17.3 — Content calendar + performance-aware drafts (M)
`POST /api/stores/{slug}/growth/calendar` (owner-gated): a strict Pydantic plan
`{cadence: 'daily'|'weekly', channels ⊆ {social, email_subject}, active: bool}`
stored in `event_log` (`growth.calendar_set`; latest row wins — no new table).
A background scheduler loop (the `agent_reaper_loop`/webhook-loop pattern in
`app/agentic.py`; flag `TILLA_GROWTH_SCHED_ENABLED`, default OFF ⇒ zero LLM
spend) ticks daily: for each active calendar, builds the prompt via the existing
`growth._build_prompt` EXTENDED with a compact performance block from 17.2's
aggregates (numbers only, first-party), calls `engine._post_generation`, validates
per-channel schema, re-screens fail-closed, writes `growth_drafts` rows
(`source='scheduled'`) under the 17.1 caps. Outage ⇒ skip tick, log, retry next
tick (never a crash-loop; the M12 outage contract).

**Accept:** claimed ONLY if a scheduled tick produces screened drafts on a live
store with the flag on (event_log rows + outbox listing as artifact). Tests:
`test_calendar_validation`, `test_scheduler_flag_off_zero_llm` (mock asserts no
`_post_generation` call), `test_daily_cap_halts_tick`,
`test_scheduled_draft_rescreened`, `test_prompt_excludes_buyer_data`.

### 17.4 — Multi-channel draft shapes (S)
Extend the per-channel schema set: `email_body` (subject 78 + plain-text body
≤2000, no HTML — XSS-safe by construction, JSON-only like today) and
`product_update` blurb (≤500). Same generation/screening/outbox pipeline; still
nothing sends. Waitlist broadcast remains DORMANT exactly as M13 shipped it
(SMTP unset ⇒ no-op) — this increment does NOT wire drafts to it.

**Accept:** schema tests per channel; `test_email_body_never_html`
(tags in LLM output ⇒ GenerationUnavailable→503 path); suite green.

## Parking (honest)

- **USER-gated — any actual sending:** SMTP broadcast of an approved `email_body`
  to the M13 waitlist requires operator SMTP creds AND a per-send human trigger;
  even then it is a separate future increment with its own spec — NOT part of
  M17. Social posting requires platform API creds and stays permanently outside
  code per INV-1 (the human copies from the outbox).
- **EXTERNALLY-BLOCKED — channel performance ingestion (likes/impressions):**
  missing dependency = external platform APIs + creds. Until then
  `performance_note` on `mark-published` is the honest manual field, and 17.2
  measures only first-party signals (orders/affiliates/waitlist).
- **PARKED — demand check:** VISION's own precondition is demonstrated merchant
  demand beyond one-shot kits; 17.3's flag stays OFF in prod until at least one
  real merchant asks for a calendar (the flag-flip is the demand receipt).

## Build order + size
1. 17.1 outbox (M) → 2. 17.2 performance readback (S/M) → 3. 17.3 calendar +
scheduler (M) → 4. 17.4 channels (S). Total buildable-now: ~2.5–3 focused days.
