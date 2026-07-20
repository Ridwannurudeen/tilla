"""Client for Warden's content-screening endpoint — the pre-deploy safety gate.

Contract (verified live): POST TILLA_SCREEN_URL {"payload": "<text>"} ->
{"verdict": "ALLOW"|"BLOCK"|..., "risk_level": ..., "threat_classes": [...], ...}
"""

import httpx

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
