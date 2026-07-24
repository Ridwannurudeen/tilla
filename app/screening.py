"""Client for Warden's content-screening endpoint — the pre-deploy safety gate.

Contract (verified live): POST TILLA_SCREEN_URL {"payload": "<text>"} ->
{"verdict": "ALLOW"|"BLOCK"|..., "risk_level": ..., "threat_classes": [...], ...}

``scan`` / ``scan_with_retry`` hit the FREE demo endpoint and are unchanged. The
M10 entry point ``screen`` dispatches to a PAID Warden hire when enabled (dormant
by default) and returns the verdict + a queryable receipt; on any paid-path problem
it degrades to the free demo scan, so the ALLOW/BLOCK/pending semantics never move.
A settled paid hire is also rated back (``warden_hire.record_rating``) — fail-open,
after the verdict is in hand, so the rating can never influence the decision.
"""

from dataclasses import dataclass

import httpx

from app import config
from app.config import WARDEN_SCREEN_TIMEOUT, WARDEN_SCREEN_URL


class ScreeningUnavailable(Exception):
    """Raised when the screening endpoint times out or fails with a server error."""


class ScreeningBlocked(Exception):
    """Raised when the screening endpoint returns a BLOCK verdict."""

    def __init__(self, verdict: dict):
        self.verdict = verdict
        super().__init__(f"content blocked: {verdict.get('risk_level')!r}")


def scan(payload: str) -> dict:
    """Screen `payload` with Warden, fail-closed. Returns the verdict dict only
    on an explicit ALLOW verdict. Raises ScreeningBlocked on BLOCK, and
    ScreeningUnavailable on timeout / connection error / any HTTP error status
    (4xx or 5xx) / a non-JSON body / a missing or unrecognized verdict — so an
    ambiguous screening result holds the store instead of deploying it."""
    try:
        r = httpx.post(
            WARDEN_SCREEN_URL,
            json={"payload": payload},
            timeout=WARDEN_SCREEN_TIMEOUT,
        )
        r.raise_for_status()
    except httpx.TimeoutException as exc:
        raise ScreeningUnavailable(f"warden screening timed out: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise ScreeningUnavailable(
            f"warden screening returned {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise ScreeningUnavailable(f"warden screening unreachable: {exc}") from exc

    try:
        verdict = r.json()
    except ValueError as exc:
        raise ScreeningUnavailable("warden screening returned a non-JSON body") from exc
    if not isinstance(verdict, dict):
        raise ScreeningUnavailable("warden screening returned an unexpected body")

    result = verdict.get("verdict")
    if result == "BLOCK":
        raise ScreeningBlocked(verdict)
    if result == "ALLOW":
        return verdict
    raise ScreeningUnavailable(f"warden screening returned verdict {result!r}")


def scan_with_retry(payload: str, attempts: int = 2) -> str:
    """Run `scan` with a short retry on transient unavailability.

    Returns "allow" only if an explicit ALLOW verdict was obtained, or
    "pending" if the endpoint stayed unreachable/erroring/ambiguous for every
    attempt. Never swallows a BLOCK verdict — ScreeningBlocked always
    propagates immediately.
    """
    last_error: ScreeningUnavailable | None = None
    for _ in range(max(1, attempts)):
        try:
            scan(payload)
            return "allow"
        except ScreeningUnavailable as exc:
            last_error = exc
            continue
    assert last_error is not None
    return "pending"


@dataclass
class ScanReceipt:
    """A queryable record of one screening event, persisted as a screening_receipts
    row. ``mode='demo'`` carries no amount/tx; ``mode='paid'`` records the x402 hire
    amount and the on-chain settle tx hash."""

    mode: str  # 'demo' | 'paid'
    endpoint: str
    verdict: str
    risk_level: str | None = None
    amount_micro: int | None = None
    tx_hash: str | None = None


@dataclass
class ScanOutcome:
    """The result of a :func:`screen` call: the go-live decision plus the receipt
    (present only on an ALLOW; a still-unavailable screen has no verdict)."""

    status: str  # 'allow' | 'pending'
    receipt: ScanReceipt | None = None


def _paid_enabled() -> bool:
    """The paid Warden hire runs ONLY when the flag is on AND a payer key is present.
    Either missing => the free demo endpoint is used and the signing path is never
    constructed, so no funds can move (dormant-by-default)."""
    return bool(config.WARDEN_PAID_ENABLED and config.TILLA_WARDEN_PAYER_KEY)


def screen(payload: str, attempts: int = 2) -> ScanOutcome:
    """Screen `payload`, fail-closed, returning the go-live decision + a receipt.

    When the paid hire is enabled a single x402 hire of Warden's scan service is
    attempted first; a served ALLOW yields a mode='paid' receipt with the settle tx.
    On BLOCK the paid verdict is honored (fail-closed, ScreeningBlocked propagates).
    Any paid-path problem degrades to the free demo scan — byte-identical to today's
    behaviour — so screening semantics (allow / block / pending) never change.
    """
    if _paid_enabled():
        # Imported lazily so the signing path is never even loaded while dormant.
        from app import warden_hire

        result = warden_hire.paid_scan(payload)
        if result is not None:
            verdict = result.verdict.get("verdict")
            # The hire settled, so rate the agent Tilla just paid (loop 5). Only
            # ALLOW/BLOCK are verdicts Tilla can act on, and that contract lives
            # here, so actionability is decided here and passed in. Fail-open and
            # fire-and-forget — it can never move the decision made below.
            warden_hire.record_rating(result, actionable=verdict in ("ALLOW", "BLOCK"))
            if verdict == "BLOCK":
                raise ScreeningBlocked(result.verdict)
            if verdict == "ALLOW":
                return ScanOutcome(
                    "allow",
                    ScanReceipt(
                        mode="paid",
                        endpoint=config.WARDEN_PAID_SCAN_URL,
                        verdict="ALLOW",
                        risk_level=result.verdict.get("risk_level"),
                        amount_micro=result.amount_micro,
                        tx_hash=result.tx_hash,
                    ),
                )
            # An unrecognized paid verdict is ambiguous -> fall back to the demo scan
            # rather than trusting it (fail-safe).
    status = scan_with_retry(payload, attempts)
    if status == "allow":
        return ScanOutcome(
            "allow",
            ScanReceipt(mode="demo", endpoint=WARDEN_SCREEN_URL, verdict="ALLOW"),
        )
    return ScanOutcome("pending", None)
