"""Roadmap Phase 3 A2A commissioned/custom-build escrow — the escrow pillar.

A buyer agent commissions a custom deliverable from a store's provider; the
:class:`~app.models.CommissionJob` walks a lifecycle with buyer protection and an
OPTIONAL evaluator that gates release:

    open -> budget_set -> funded -> submitted -> completed
                              \\-> disputed        \\-> disputed
     \\-> cancelled  \\-> cancelled

DORMANT by default: every /api/escrow/* endpoint 503s until ``TILLA_ESCROW`` is set
(the MPP/ACP dormant-mount pattern), so this — Tilla's only custodial-ADJACENT surface
— stays unreachable until an operator opts in.

NON-CUSTODIAL ABOVE ALL. Tilla holds no keys, sends no funds, and never auto-releases.
The two fund-relevant transitions are each driven by VERIFYING a user-signed on-chain
USDT0 transfer and RECORDING its hash — the exact ``app.self_serve`` / ``app.refunds``
verify-only pattern, never a send:

  * ``funded``    — the BUYER deposits ``budget_micro`` to ``escrow_addr`` (a holding
                    wallet the parties designate; Tilla never holds its keys). Verified
                    from=buyer, to=escrow, exact amount; the tx hash is recorded.
  * ``completed`` — the escrow holder releases ``budget_micro`` to the provider
                    (``store.pay_to``). Verified from=escrow, to=provider, exact amount,
                    not predating the deposit; authorized ONLY by the evaluator when one
                    is set, else the buyer. The tx hash is recorded.

Every state change is a race-proof conditional UPDATE (the M3 ``checkout.transition``
idiom), so no step ever runs twice. All party actions are authenticated by the M4 buyer
wallet-session (``delivery.load_session_token``) and fail closed on the wrong party.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Path, Request
from itsdangerous import BadSignature, SignatureExpired
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app import agentic, chain, config, delivery, payment
from app.db import SessionLocal
from app.limiter import limiter
from app.models import CommissionJob, Store, log_event
from app.screening import ScreeningBlocked, screen

router = APIRouter()

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
# No caching on a mutable job resource; nosniff on the JSON body (the ACP pattern).
_ESCROW_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
# Budget bounds (micro-USDT): positive, capped at 10k USDT (the M16 tier ceiling).
MAX_BUDGET_MICRO = 10_000_000_000


class EscrowError(Exception):
    """A deposit/release tx could not be verified. ``status_code`` maps to the HTTP
    response (400 verification failure, 409 conflict/idempotency)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    """Naive UTC, matching app.checkout/app.delivery — SQLite reads DateTimes back
    naive, so every comparison stays naive-to-naive."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------ gates + auth
def _require_enabled() -> None:
    if not config.ESCROW_ENABLED:
        raise HTTPException(503, "escrow is not enabled")


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


def _session_wallet(request: Request) -> str:
    """The lowercased wallet behind the caller's buyer session token (the M4
    ``delivery`` session machinery, reused verbatim). 503 when sign-in is
    unconfigured; 401 on a missing/expired/forged token. Buyers, providers, and
    evaluators are all just wallets, so one session mechanism authenticates all
    three parties and the per-action party check fails closed on a mismatch."""
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    try:
        return delivery.load_session_token(_bearer(request)).lower()
    except SignatureExpired:
        raise HTTPException(401, "session expired") from None
    except BadSignature:
        raise HTTPException(401, "invalid session") from None


def _parse_json(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, "invalid JSON body") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "body must be a JSON object")
    return data


def _require_party(caller: str, required: str, label: str) -> None:
    """Fail closed on the wrong party — the authenticated wallet must equal the
    action's required party (both lowercased)."""
    if caller.lower() != (required or "").lower():
        raise HTTPException(403, f"only the {label} may perform this action")


# ------------------------------------------------------------ validation
def _require_addr(value, field: str) -> str:
    if not isinstance(value, str) or not _EVM_ADDRESS.fullmatch(value):
        raise HTTPException(422, f"{field} must be a 0x-prefixed 20-byte EVM address")
    return value.lower()


def _require_tx(value) -> str:
    if not isinstance(value, str) or not _TX_HASH.fullmatch(value):
        raise HTTPException(422, "tx_hash must be a 0x-prefixed 32-byte hash")
    return value.lower()


def _require_text(value, field: str, maxlen: int) -> str:
    if not isinstance(value, str):
        raise HTTPException(422, f"{field} is required")
    text = value.strip()
    if not text:
        raise HTTPException(422, f"{field} is required")
    if len(text) > maxlen:
        raise HTTPException(422, f"{field} exceeds {maxlen} characters")
    return text


def _require_slug(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(config.SLUG_PATTERN, value):
        raise HTTPException(422, "store must be a valid slug")
    return value


def _require_budget(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise HTTPException(422, "budget_micro must be an integer")
    if value <= 0 or value > MAX_BUDGET_MICRO:
        raise HTTPException(422, "budget_micro is out of range")
    return value


# ------------------------------------------------------------ state machine
def transition(session: Session, job_id: str, from_states, to_state, **fields) -> bool:
    """Conditional status UPDATE (the ``checkout.transition`` idiom). Returns True iff
    exactly one row moved — a lost race returns False, so a transition never runs
    twice."""
    result = session.execute(
        update(CommissionJob)
        .where(
            CommissionJob.id == job_id,
            CommissionJob.status.in_(tuple(from_states)),
        )
        .values(status=to_state, **fields)
    )
    return result.rowcount == 1


def _get_job(session: Session, job_id: str) -> CommissionJob:
    job = session.get(CommissionJob, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


def _screen_or_raise(text: str) -> None:
    """Warden-screen untrusted job text fail-closed BEFORE it is stored: a BLOCK is a
    422 and any non-clear/unavailable verdict is a 503 (the review/self_serve gate)."""
    try:
        outcome = screen(text)
    except ScreeningBlocked as exc:
        raise HTTPException(422, "content did not pass safety screening") from exc
    if outcome.status != "allow":
        raise HTTPException(503, "content screening temporarily unavailable")


def _reject_reused_tx(session: Session, tx_hash: str, kind: str) -> None:
    """Clean 409 when a submitted tx already funds/releases another job (the
    self_serve pre-check; UNIQUE(funded_tx)/UNIQUE(released_tx) is the race backstop)."""
    col = CommissionJob.funded_tx if kind == "funded" else CommissionJob.released_tx
    if session.scalar(select(CommissionJob.id).where(col == tx_hash)) is not None:
        raise HTTPException(409, f"{kind} tx already recorded")


def _verify_transfer(
    cfg, tx_hash: str, want_from: str, want_to: str, amount: int, min_block=None
) -> int:
    """Verify ``tx_hash`` is a succeeded USDT0 transfer summing EXACTLY to ``amount``
    from ``want_from`` to ``want_to`` on ``cfg``'s chain, and return its block. Pins
    both endpoints + the exact amount (mirroring app.refunds / app.self_serve), so a
    stray/partial transfer can't be farmed. VERIFY-ONLY — this reads a receipt and
    NEVER sends. Raises :class:`EscrowError` on any mismatch; chain errors propagate."""
    receipt = chain.get_transaction_receipt(cfg, tx_hash)
    if receipt is None:
        raise EscrowError(400, "transaction not found")
    if str(receipt.get("status", "")).lower() != "0x1":
        raise EscrowError(400, "transaction did not succeed on-chain")
    want_from_topic = chain.pad_address(want_from).lower()
    want_to_topic = chain.pad_address(want_to).lower()
    matched_any = False
    total = 0
    block: int | None = None
    for lg in receipt.get("logs", []):
        if (lg.get("address", "") or "").lower() != cfg.asset:
            continue
        topics = lg.get("topics", [])
        if len(topics) < 3 or topics[0].lower() != config.TRANSFER_TOPIC:
            continue
        if topics[1].lower() != want_from_topic or topics[2].lower() != want_to_topic:
            continue
        matched_any = True
        d = chain.decode_transfer_log(lg)
        if min_block is not None and d["block_number"] < min_block:
            # A release can never precede the deposit it settles (the M9 block floor).
            raise EscrowError(400, "release transfer predates the deposit")
        total += d["value"]
        block = d["block_number"]
    if not matched_any:
        raise EscrowError(400, "no matching USDT0 transfer between those wallets")
    if total != amount:
        raise EscrowError(400, "transfer amount does not match the exact budget")
    return block  # type: ignore[return-value]


# ------------------------------------------------------------ response shape
def _job_shape(job: CommissionJob, store: Store | None) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "buyer": job.buyer_addr,
        "store_slug": store.slug if store else None,
        "provider": store.pay_to if store else None,
        "title": job.title,
        "brief": job.brief,
        "budget_micro": job.budget_micro,
        "budget_usdt": (
            agentic._usdt_str(job.budget_micro)
            if job.budget_micro is not None
            else None
        ),
        "escrow_addr": job.escrow_addr,
        "evaluator": job.evaluator_addr,
        "funded_tx": job.funded_tx,
        "released_tx": job.released_tx,
        "deliverable": job.deliverable,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "funded_at": job.funded_at.isoformat() if job.funded_at else None,
        "submitted_at": job.submitted_at.isoformat() if job.submitted_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


# ------------------------------------------------------------ cores (off-loop)
def _create_core(buyer, slug, title, brief, evaluator_addr):
    with SessionLocal() as session:
        store = agentic._require_live_store(session, slug)
        _screen_or_raise(f"{title}\n\n{brief}")
        job = CommissionJob(
            id=uuid.uuid4().hex[:16],
            buyer_addr=buyer,
            store_id=store.id,
            title=title,
            brief=brief,
            evaluator_addr=evaluator_addr,
            status="open",
        )
        session.add(job)
        session.flush()
        log_event(
            session, "escrow", "job.created", store_id=store.id, data={"job": job.id}
        )
        session.commit()
        return _job_shape(job, store), 201


def _budget_core(buyer, job_id, budget_micro, escrow_addr):
    with SessionLocal() as session:
        job = _get_job(session, job_id)
        store = session.get(Store, job.store_id)
        _require_party(buyer, job.buyer_addr, "buyer")
        if job.status != "open":
            raise HTTPException(409, f"job is not open (status {job.status})")
        if not transition(
            session,
            job.id,
            ("open",),
            "budget_set",
            budget_micro=budget_micro,
            escrow_addr=escrow_addr,
        ):
            raise HTTPException(409, "job is no longer open")
        log_event(
            session, "escrow", "job.budget_set", store_id=store.id, data={"job": job.id}
        )
        session.commit()
        session.refresh(job)
        return _job_shape(job, store), 200


def _fund_core(buyer, job_id, tx_hash):
    with SessionLocal() as session:
        job = _get_job(session, job_id)
        store = session.get(Store, job.store_id)
        _require_party(buyer, job.buyer_addr, "buyer")
        if job.status != "budget_set":
            raise HTTPException(
                409, f"job is not awaiting funding (status {job.status})"
            )
        _reject_reused_tx(session, tx_hash, "funded")
        # VERIFY the buyer's deposit landed at escrow_addr with the exact budget — Tilla
        # records the hash, it never sent these funds.
        try:
            block = _verify_transfer(
                payment.CANONICAL_CHAIN,
                tx_hash,
                job.buyer_addr,
                job.escrow_addr,
                job.budget_micro,
            )
        except EscrowError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        except (httpx.HTTPError, chain.ChainError) as exc:
            raise HTTPException(502, "chain verification unavailable") from exc
        try:
            moved = transition(
                session,
                job.id,
                ("budget_set",),
                "funded",
                funded_tx=tx_hash,
                funded_block=block,
                funded_at=_now(),
            )
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(409, "deposit tx already recorded") from exc
        if not moved:
            raise HTTPException(409, "job is no longer awaiting funding")
        log_event(
            session,
            "escrow",
            "job.funded",
            store_id=store.id,
            data={"job": job.id, "funded_tx": tx_hash},
        )
        session.commit()
        session.refresh(job)
        return _job_shape(job, store), 200


def _submit_core(provider, job_id, deliverable):
    with SessionLocal() as session:
        job = _get_job(session, job_id)
        store = session.get(Store, job.store_id)
        _require_party(provider, store.pay_to, "provider")
        if job.status != "funded":
            raise HTTPException(409, f"job is not funded (status {job.status})")
        _screen_or_raise(deliverable)
        if not transition(
            session,
            job.id,
            ("funded",),
            "submitted",
            deliverable=deliverable,
            submitted_at=_now(),
        ):
            raise HTTPException(409, "job is no longer funded")
        log_event(
            session, "escrow", "job.submitted", store_id=store.id, data={"job": job.id}
        )
        session.commit()
        session.refresh(job)
        return _job_shape(job, store), 200


def _release_core(caller, job_id, tx_hash):
    with SessionLocal() as session:
        job = _get_job(session, job_id)
        store = session.get(Store, job.store_id)
        # Release is authorized ONLY by the evaluator when one gates the job, else the
        # buyer — fail closed on anyone else.
        if job.evaluator_addr:
            _require_party(caller, job.evaluator_addr, "evaluator")
        else:
            _require_party(caller, job.buyer_addr, "buyer")
        if job.status != "submitted":
            raise HTTPException(
                409, f"job is not awaiting release (status {job.status})"
            )
        _reject_reused_tx(session, tx_hash, "released")
        # VERIFY the release landed at the provider (store.pay_to) with the exact
        # budget, not predating the deposit — Tilla records the hash, never sends.
        try:
            _verify_transfer(
                payment.CANONICAL_CHAIN,
                tx_hash,
                job.escrow_addr,
                store.pay_to,
                job.budget_micro,
                min_block=job.funded_block,
            )
        except EscrowError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        except (httpx.HTTPError, chain.ChainError) as exc:
            raise HTTPException(502, "chain verification unavailable") from exc
        try:
            moved = transition(
                session,
                job.id,
                ("submitted",),
                "completed",
                released_tx=tx_hash,
                completed_at=_now(),
            )
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(409, "release tx already recorded") from exc
        if not moved:
            raise HTTPException(409, "job is no longer awaiting release")
        log_event(
            session,
            "escrow",
            "job.completed",
            store_id=store.id,
            data={"job": job.id, "released_tx": tx_hash},
        )
        session.commit()
        session.refresh(job)
        return _job_shape(job, store), 200


def _cancel_core(buyer, job_id):
    with SessionLocal() as session:
        job = _get_job(session, job_id)
        store = session.get(Store, job.store_id)
        _require_party(buyer, job.buyer_addr, "buyer")
        # Cancel only before funds are committed; a funded job must be disputed.
        if job.status not in ("open", "budget_set"):
            raise HTTPException(409, f"job cannot be cancelled in status {job.status}")
        if not transition(session, job.id, ("open", "budget_set"), "cancelled"):
            raise HTTPException(409, "job can no longer be cancelled")
        log_event(
            session, "escrow", "job.cancelled", store_id=store.id, data={"job": job.id}
        )
        session.commit()
        session.refresh(job)
        return _job_shape(job, store), 200


def _dispute_core(caller, job_id):
    with SessionLocal() as session:
        job = _get_job(session, job_id)
        store = session.get(Store, job.store_id)
        # Either party (buyer or provider) may raise a dispute on an in-flight job.
        if caller.lower() not in (job.buyer_addr.lower(), store.pay_to.lower()):
            raise HTTPException(403, "only a job party may dispute")
        if job.status not in ("funded", "submitted"):
            raise HTTPException(409, f"job cannot be disputed in status {job.status}")
        if not transition(session, job.id, ("funded", "submitted"), "disputed"):
            raise HTTPException(409, "job can no longer be disputed")
        log_event(
            session, "escrow", "job.disputed", store_id=store.id, data={"job": job.id}
        )
        session.commit()
        session.refresh(job)
        return _job_shape(job, store), 200


def _get_core(caller, job_id):
    with SessionLocal() as session:
        job = _get_job(session, job_id)
        store = session.get(Store, job.store_id)
        parties = {job.buyer_addr.lower(), (store.pay_to or "").lower()}
        if job.evaluator_addr:
            parties.add(job.evaluator_addr.lower())
        if caller.lower() not in parties:
            # Opaque 404 for a non-party — no job oracle (the reviews precedent).
            raise HTTPException(404, "job not found")
        return _job_shape(job, store), 200


# ------------------------------------------------------------------- routes
@router.post("/api/escrow/jobs")
@limiter.limit("20/minute")
async def escrow_create(request: Request):
    _require_enabled()
    caller = _session_wallet(request)
    body = _parse_json(await request.body())
    slug = _require_slug(body.get("store"))
    title = _require_text(body.get("title"), "title", 200)
    brief = _require_text(body.get("brief"), "brief", config.MAX_DESCRIPTION_LEN)
    raw_eval = body.get("evaluator_addr")
    evaluator = _require_addr(raw_eval, "evaluator_addr") if raw_eval else None
    shape, status = await asyncio.to_thread(
        _create_core, caller, slug, title, brief, evaluator
    )
    return JSONResponse(shape, status_code=status, headers=_ESCROW_HEADERS)


@router.get("/api/escrow/jobs/{job_id}")
@limiter.limit("60/minute")
async def escrow_get(
    request: Request,
    job_id: str = Path(..., min_length=1, max_length=32),
):
    _require_enabled()
    caller = _session_wallet(request)
    shape, status = await asyncio.to_thread(_get_core, caller, job_id)
    return JSONResponse(shape, status_code=status, headers=_ESCROW_HEADERS)


@router.post("/api/escrow/jobs/{job_id}/budget")
@limiter.limit("20/minute")
async def escrow_set_budget(
    request: Request,
    job_id: str = Path(..., min_length=1, max_length=32),
):
    _require_enabled()
    caller = _session_wallet(request)
    body = _parse_json(await request.body())
    budget = _require_budget(body.get("budget_micro"))
    escrow_addr = _require_addr(body.get("escrow_addr"), "escrow_addr")
    shape, status = await asyncio.to_thread(
        _budget_core, caller, job_id, budget, escrow_addr
    )
    return JSONResponse(shape, status_code=status, headers=_ESCROW_HEADERS)


@router.post("/api/escrow/jobs/{job_id}/fund")
@limiter.limit("20/minute")
async def escrow_fund(
    request: Request,
    job_id: str = Path(..., min_length=1, max_length=32),
):
    _require_enabled()
    caller = _session_wallet(request)
    tx_hash = _require_tx(_parse_json(await request.body()).get("tx_hash"))
    shape, status = await asyncio.to_thread(_fund_core, caller, job_id, tx_hash)
    return JSONResponse(shape, status_code=status, headers=_ESCROW_HEADERS)


@router.post("/api/escrow/jobs/{job_id}/submit")
@limiter.limit("20/minute")
async def escrow_submit(
    request: Request,
    job_id: str = Path(..., min_length=1, max_length=32),
):
    _require_enabled()
    caller = _session_wallet(request)
    body = _parse_json(await request.body())
    deliverable = _require_text(
        body.get("deliverable"), "deliverable", config.MAX_DESCRIPTION_LEN
    )
    shape, status = await asyncio.to_thread(_submit_core, caller, job_id, deliverable)
    return JSONResponse(shape, status_code=status, headers=_ESCROW_HEADERS)


@router.post("/api/escrow/jobs/{job_id}/release")
@limiter.limit("20/minute")
async def escrow_release(
    request: Request,
    job_id: str = Path(..., min_length=1, max_length=32),
):
    _require_enabled()
    caller = _session_wallet(request)
    tx_hash = _require_tx(_parse_json(await request.body()).get("tx_hash"))
    shape, status = await asyncio.to_thread(_release_core, caller, job_id, tx_hash)
    return JSONResponse(shape, status_code=status, headers=_ESCROW_HEADERS)


@router.post("/api/escrow/jobs/{job_id}/cancel")
@limiter.limit("20/minute")
async def escrow_cancel(
    request: Request,
    job_id: str = Path(..., min_length=1, max_length=32),
):
    _require_enabled()
    caller = _session_wallet(request)
    shape, status = await asyncio.to_thread(_cancel_core, caller, job_id)
    return JSONResponse(shape, status_code=status, headers=_ESCROW_HEADERS)


@router.post("/api/escrow/jobs/{job_id}/dispute")
@limiter.limit("20/minute")
async def escrow_dispute(
    request: Request,
    job_id: str = Path(..., min_length=1, max_length=32),
):
    _require_enabled()
    caller = _session_wallet(request)
    shape, status = await asyncio.to_thread(_dispute_core, caller, job_id)
    return JSONResponse(shape, status_code=status, headers=_ESCROW_HEADERS)
