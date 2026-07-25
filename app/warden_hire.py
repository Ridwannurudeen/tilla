"""M10 Warden PAID hire — a one-shot x402 payer client (agents-hiring-agents).

Dormant by default. :func:`paid_scan` upgrades a single content screen from the
FREE demo endpoint to a PAID hire of Warden #3808's listed scan service: it decodes
the 402 PAYMENT-REQUIRED challenge (which carries the full EIP-712 spec), signs an
EIP-3009 authorization LOCALLY with eth-account, replays ONCE with the signed
PAYMENT-SIGNATURE header, and reads the PAYMENT-RESPONSE settle tx hash back.

Funds-safety invariants (funds move only when Tilla means to spend):
  (a) it REFUSES to sign unless the challenge's scheme/asset/network/payTo/amount
      match pinned expectations (payTo pinned to Warden's wallet, amount capped at
      ``config.TILLA_WARDEN_MAX_MICRO``) — a changed/hostile 402 can never
      over-commit or redirect the fee;
  (b) it signs AT MOST ONCE and NEVER retries a signed authorization — a transport
      failure after signing returns None (the caller falls back to the free demo
      scan) rather than re-firing; funds settle only on a served 200;
  (c) ANY problem (missing key, undecodable challenge, pin mismatch, non-200,
      timeout) returns None so the caller degrades to the free demo endpoint and
      screening semantics stay unchanged.

The verdict body is returned verbatim; ALLOW/BLOCK interpretation stays in
``app.screening`` (one place enforces the verdict contract).

Rating write-back (:func:`record_rating`): once a hire has SETTLED, Tilla rates the
agent it just paid. The local half — an append-only ``hire.rating`` event_log row —
is real and always written. The submission half is a DRY RUN ONLY: no rating /
feedback / reputation endpoint or ``onchainos`` subcommand is documented anywhere in
this repo, so :func:`rating_payload` builds Tilla's own shape and nothing ever sends
it. See that function's UNVERIFIED note before wiring any transport.
"""

from __future__ import annotations

import logging
import os
import shlex
import time
from dataclasses import dataclass

import httpx
from x402.http.constants import PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER
from x402.http.utils import (
    PAYMENT_REQUIRED_HEADER,
    decode_payment_required_header,
    decode_payment_response_header,
    encode_payment_signature_header,
)
from x402.schemas import PaymentPayload, PaymentRequirements

from app import config
from app.db import SessionLocal
from app.models import log_event
from app.payment import (
    PAYMENT_ASSET,
    PAYMENT_EIP712_NAME,
    PAYMENT_EIP712_VERSION,
    PAYMENT_NETWORK,
    PAYMENT_SCHEME,
    PAYMENT_TIMEOUT_SECONDS,
)

logger = logging.getLogger("tilla")

# The agent Tilla hires: Warden #3808, whose paid scan service is the endpoint pinned
# by WARDEN_PAID_PAYTO (docs/runbooks/M10-onchain.md step D).
WARDEN_AGENT_ID = 3808
# The rating scale the payload uses. 1..5 mirrors Tilla's own buyer-review scale
# (models.Review's 1..5 CHECK), so both directions of reputation read the same way.
RATING_MAX = 5

# The EIP-3009 TransferWithAuthorization struct, signed locally with eth-account so
# the paid path needs NO web3 (the x402 SDK's EVM signer does). Field order/types
# must match the facilitator's expectation exactly (see x402 evm eip712.py).
_TRANSFER_WITH_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}
# Small clock-skew buffer for validAfter (mirrors the SDK's validity window).
_VALIDITY_BUFFER_SECONDS = 60


@dataclass
class PaidScan:
    """The result of a served paid scan: the raw verdict body plus the on-chain
    settle receipt. ``tx_hash`` is None only when the PAYMENT-RESPONSE header was
    undecodable (the scan still settled and was served). ``latency_ms`` is the whole
    hire measured wall-clock — unpaid probe, local signing, served paid replay — i.e.
    what Tilla actually waited for, and the only timing fact it can honestly rate."""

    verdict: dict
    amount_micro: int
    tx_hash: str | None
    latency_ms: int


def _select_exact(req) -> PaymentRequirements | None:
    """The exact-scheme requirement on Tilla's network, or None."""
    for a in getattr(req, "accepts", None) or []:
        if (
            getattr(a, "scheme", None) == PAYMENT_SCHEME
            and str(getattr(a, "network", "")) == PAYMENT_NETWORK
        ):
            return a
    return None


def _pins_ok(reqs: PaymentRequirements) -> bool:
    """True iff the challenge matches every pinned expectation — checked BEFORE any
    signing so a hostile/changed 402 can never move funds."""
    pinned_pay_to = (config.WARDEN_PAID_PAYTO or "").lower()
    if not pinned_pay_to:
        logger.warning("warden_hire: no pinned payTo configured; refusing to sign")
        return False
    if (reqs.pay_to or "").lower() != pinned_pay_to:
        logger.warning("warden_hire: challenge payTo does not match the pinned wallet")
        return False
    if (reqs.asset or "").lower() != PAYMENT_ASSET.lower():
        logger.warning("warden_hire: challenge asset does not match USDT0")
        return False
    if str(reqs.network) != PAYMENT_NETWORK:
        logger.warning("warden_hire: challenge network is not eip155:196")
        return False
    try:
        amount = int(reqs.amount)
    except (TypeError, ValueError):
        logger.warning("warden_hire: challenge amount is not an integer")
        return False
    if amount <= 0 or amount > config.TILLA_WARDEN_MAX_MICRO:
        logger.warning(
            "warden_hire: challenge amount %s over cap %s",
            amount,
            config.TILLA_WARDEN_MAX_MICRO,
        )
        return False
    return True


def _sign_payload(reqs: PaymentRequirements) -> str | None:
    """Build + sign the EIP-3009 authorization for `reqs` LOCALLY with eth-account
    (no web3), returning the encoded PAYMENT-SIGNATURE header value, or None on any
    signing error. The wire shape mirrors x402's ExactEIP3009Payload.to_dict()."""
    try:
        from eth_account import Account

        account = Account.from_key(config.TILLA_WARDEN_PAYER_KEY)
        chain_id = int(str(reqs.network).split(":")[1])
        extra = reqs.extra or {}
        name = extra.get("name") or PAYMENT_EIP712_NAME
        version = extra.get("version") or PAYMENT_EIP712_VERSION
        now = int(time.time())
        valid_after = now - _VALIDITY_BUFFER_SECONDS
        valid_before = now + (reqs.max_timeout_seconds or PAYMENT_TIMEOUT_SECONDS)
        nonce_bytes = os.urandom(32)
        nonce_hex = "0x" + nonce_bytes.hex()
        value = int(reqs.amount)

        signed = Account.sign_typed_data(
            config.TILLA_WARDEN_PAYER_KEY,
            domain_data={
                "name": name,
                "version": version,
                "chainId": chain_id,
                "verifyingContract": reqs.asset,
            },
            message_types=_TRANSFER_WITH_AUTHORIZATION_TYPES,
            message_data={
                "from": account.address,
                "to": reqs.pay_to,
                "value": value,
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": nonce_bytes,
            },
        )
        inner = {
            "authorization": {
                "from": account.address,
                "to": reqs.pay_to,
                "value": str(value),
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": nonce_hex,
            },
            "signature": "0x" + bytes(signed.signature).hex(),
        }
        payload = PaymentPayload(x402_version=2, payload=inner, accepted=reqs)
        return encode_payment_signature_header(payload)
    except Exception:
        logger.exception("warden_hire: failed to build/sign payment authorization")
        return None


def paid_scan(payload: str) -> PaidScan | None:
    """Hire Warden's paid scan for one screen, or None if the paid path is not
    cleanly usable (caller then falls back to the free demo endpoint). Signs at most
    once and never retries a signed authorization."""
    if not config.TILLA_WARDEN_PAYER_KEY:
        return None
    timeout = config.WARDEN_SCREEN_TIMEOUT
    url = config.WARDEN_PAID_SCAN_URL
    body = {"payload": payload}
    started = time.monotonic()

    # 1) Unpaid probe: expect a 402 carrying the EIP-712 challenge.
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            challenge = client.post(url, json=body)
            if challenge.status_code != 402:
                logger.warning(
                    "warden_hire: expected 402, got %s; using demo",
                    challenge.status_code,
                )
                return None
            header = challenge.headers.get(PAYMENT_REQUIRED_HEADER)
            if not header:
                logger.warning("warden_hire: 402 without a PAYMENT-REQUIRED header")
                return None
    except httpx.HTTPError:
        logger.warning("warden_hire: challenge request failed; using demo")
        return None

    # 2) Decode + PIN. No signing happens unless every pin matches.
    try:
        required = decode_payment_required_header(header)
    except Exception:
        logger.exception("warden_hire: undecodable PAYMENT-REQUIRED challenge")
        return None
    reqs = _select_exact(required)
    if reqs is None or not _pins_ok(reqs):
        return None

    # 3) Sign LOCALLY (one authorization).
    signature = _sign_payload(reqs)
    if signature is None:
        return None
    amount_micro = int(reqs.amount)

    # 4) Replay EXACTLY ONCE with the signed header. A transport failure here does
    #    NOT re-fire (one-clean-tx): funds settle only on a served 200.
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            paid = client.post(
                url, json=body, headers={PAYMENT_SIGNATURE_HEADER: signature}
            )
    except httpx.HTTPError:
        logger.warning("warden_hire: paid replay failed after signing; using demo")
        return None
    if paid.status_code != 200:
        logger.warning(
            "warden_hire: paid replay returned %s (no settle); using demo",
            paid.status_code,
        )
        return None
    try:
        verdict = paid.json()
    except ValueError:
        logger.warning("warden_hire: paid scan returned a non-JSON body; using demo")
        return None
    if not isinstance(verdict, dict):
        return None

    tx_hash = None
    settle_header = paid.headers.get(PAYMENT_RESPONSE_HEADER)
    if settle_header:
        try:
            tx_hash = decode_payment_response_header(settle_header).transaction or None
        except Exception:
            logger.exception("warden_hire: undecodable PAYMENT-RESPONSE on served scan")
    return PaidScan(
        verdict=verdict,
        amount_micro=amount_micro,
        tx_hash=tx_hash,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


# ---------- rating write-back: Tilla rates the agent it just paid ----------
def _rate_hires_enabled() -> bool:
    """``TILLA_RATE_HIRES=1`` turns on the DRY-RUN submitter log. Read at call time
    rather than at config import so a test/operator can flip it without a restart —
    it is safe to, because there is nothing to submit TO (see rating_payload)."""
    return os.environ.get("TILLA_RATE_HIRES", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _rating(actionable: bool, latency_ms: int) -> int:
    """The score Tilla can honestly give one settled hire, out of RATING_MAX.

    Derived ONLY from facts this client observed: 1 when the hire settled but came
    back with a verdict Tilla could not act on (money spent, nothing usable), 3 when
    an actionable verdict took more than half the screen budget, else 5. Whether the
    verdict was CORRECT is deliberately not scored — Tilla cannot check that, and a
    rating it cannot justify would be noise in someone else's reputation graph.
    """
    if not actionable:
        return 1
    if latency_ms > config.WARDEN_SCREEN_TIMEOUT * 1000 / 2:
        return 3
    return RATING_MAX


class SelfReviewRefused(Exception):
    """Raised when the agent to be rated shares Tilla's owner wallet.

    A public star rating carries the implicit claim that an independent party
    assessed the service. When both agents belong to one owner that claim is false,
    and the rating would inflate a reputation graph other people rely on. Splitting
    the wallets would only make the relationship harder to see, not make the review
    independent — so the refusal keys on the OWNER, and there is deliberately no
    override flag.
    """


def _owner_of(agent_id: int) -> str | None:
    """The on-chain owner wallet of `agent_id`, lowercased, or None if unreadable.

    Delegates to the ERC-8004 registry read b2b already owns and tests (a read-only
    ``ownerOf`` eth_call, ``fresh=True`` so an ownership transfer inside the cache
    TTL cannot hide a self-review). Imported lazily: b2b pulls in the agentic module
    graph, and this seam is only reached on a settled paid hire.
    """
    from app import b2b

    return b2b.verify_agent_owner(agent_id, fresh=True)


def assert_not_self_review(target_agent_id: int) -> None:
    """Fail CLOSED unless `target_agent_id` provably belongs to a different owner.

    Raises SelfReviewRefused when the owners match OR when either is unreadable —
    an unverifiable counterparty is not evidence of independence.

    Production reality on 2026-07-25, both facts measured, not assumed:
      * OKX's agent API (``onchainos agent get-agents``) reports Warden #3808 and
        Tilla #6961 under the SAME ownerAddress 0xf4c9…fa51 — so a rating of Warden
        by Tilla is a review of one owner's agent by the same owner's agent.
      * The ERC-8004 registry this function reads returns None for BOTH ids, i.e.
        OKX marketplace agent ids do not resolve through it.
    So today every hire is refused on the unreadable branch rather than the
    same-owner branch. Both roads lead to "not provably independent", which is the
    intended outcome: the loop is built, wired and tested, and declines to publish
    a rating it cannot justify. Making it fire for a genuine third party needs an
    ownership source that covers OKX agent ids — do NOT swap in an HTTP endpoint
    whose contract has not been read off the wire first.
    """
    from app.agentic import AGENT_ID

    mine = _owner_of(AGENT_ID)
    theirs = _owner_of(target_agent_id)
    if mine is None or theirs is None:
        raise SelfReviewRefused(
            f"cannot verify independent ownership (self={mine}, target={theirs})"
        )
    if mine == theirs:
        raise SelfReviewRefused(
            f"agent {target_agent_id} shares Tilla's owner {mine} — a review from"
            " one wallet's agent about another is not an independent assessment"
        )


def rating_payload(scan: PaidScan, *, actionable: bool) -> dict:
    """The rating Tilla submits for one settled hire of Warden #3808.

    Transport VERIFIED 2026-07-25 against the installed onchainos CLI:
    ``agent feedback-submit --agent-id <target> --creator-id <mine> --score
    <0.00-5.00> --task-id <id> [--description ...]``. Note --task-id is REQUIRED and
    belongs to OKX's task lifecycle (create-task -> asp-match -> confirm-accept ->
    complete); a direct x402 endpoint hire like Tilla's screening has no task id, so
    submission stays gated on one being supplied by the caller.

    Earlier revisions of this docstring said no such surface existed. That was true
    of this repo and false of the platform — the CLI has had it all along.
    """
    return {
        "agent_id": WARDEN_AGENT_ID,
        "service": "paid_scan",
        "rating": _rating(actionable, scan.latency_ms),
        "max_rating": RATING_MAX,
        "actionable_verdict": actionable,
        "latency_ms": scan.latency_ms,
        "amount_micro": scan.amount_micro,
        "tx_hash": scan.tx_hash,
        "network": PAYMENT_NETWORK,
    }


def record_rating(scan: PaidScan, *, actionable: bool) -> None:
    """Rate `scan`'s hire: always an append-only ``hire.rating`` event_log row, plus
    the dry-run payload log when ``TILLA_RATE_HIRES`` is on.

    Fire-and-forget and FAIL-OPEN: the hire has already settled and the verdict is
    already decided, so nothing here may raise into — or change — a screening
    decision. This is the ONLY fail-open path in the screening seam.
    """
    try:
        payload = rating_payload(scan, actionable=actionable)
        try:
            assert_not_self_review(WARDEN_AGENT_ID)
            payload["independent"] = True
        except SelfReviewRefused as exc:
            # Recorded, never submitted: the local row is honest bookkeeping of a
            # hire Tilla made; publishing it would be self-review.
            payload["independent"] = False
            payload["withheld_reason"] = str(exc)
        with SessionLocal() as session:
            log_event(session, "warden_hire", "hire.rating", data=payload)
            session.commit()
        if not payload["independent"]:
            logger.info(
                "warden_hire: rating WITHHELD (not independent): %s",
                payload["withheld_reason"],
            )
            return
        if _rate_hires_enabled():
            logger.info(
                "warden_hire: rating ready to submit (%s); submission requires an OKX"
                " task id — see rating_payload for the verified CLI contract: %s",
                _submit_command(payload),
                payload,
            )
    except Exception:
        logger.exception("warden_hire: rating write-back failed; screening unaffected")


def _submit_command(payload: dict) -> str:
    """The exact, verified `onchainos agent feedback-submit` line for `payload`.

    Built rather than executed: --task-id is required by the CLI and belongs to
    OKX's task lifecycle, which a direct x402 hire has none of, so the operator
    supplies it. Emitting the precise command keeps the last mile a human decision
    without inventing a request shape.
    """
    from app.agentic import AGENT_ID

    return (
        "onchainos agent feedback-submit"
        f" --agent-id {payload['agent_id']}"
        f" --creator-id {AGENT_ID}"
        f" --score {payload['rating']:.2f}"
        " --task-id <OKX task id>"
        f" --description {shlex.quote(_review_text(payload))}"
    )


def _review_text(payload: dict) -> str:
    """Buyer-vocabulary review text derived only from what this client observed."""
    verdict = "actionable" if payload["actionable_verdict"] else "unusable"
    return (
        f"Paid scan settled in {payload['latency_ms']}ms and returned an"
        f" {verdict} verdict."
    )
