"""M4 gated delivery: signed tokens, content-addressed file storage,
entitlement/license issuance, buyer wallet sign-in, and the email re-delivery
stub.

Every capability here fails CLOSED when ``TILLA_SIGNING_KEY`` is unset: the token
serializers raise :class:`SigningUnavailable`, the gated endpoints 503, and the
legacy ``store.delivery`` text path is untouched. No module-level import of
``app.checkout`` — the dependency runs the other way (checkout imports this for
license minting), so terminal-status checks live in the route layer.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import logging
import os
import re
import secrets
import smtplib
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from eth_account import Account
from eth_account.messages import encode_defunct
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.models import AuthNonce, Entitlement, LicenseActivation, log_event

logger = logging.getLogger("tilla")

FILE_READY_MESSAGE = "Your download is ready."

_DOWNLOAD_SALT = "tilla.download.v1"
_SESSION_SALT = "tilla.session.v1"
_REDELIVER_SALT = "tilla.redeliver.v1"
# M9 merchant sessions ride the SAME signing key but a distinct salt, so a buyer
# session token can never validate on a merchant route and vice versa (the
# per-purpose-salt rule that already separates download/session/redeliver).
_MERCHANT_SALT = "tilla.merchant.v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SigningUnavailable(RuntimeError):
    """TILLA_SIGNING_KEY is not configured — gated endpoints fail closed (503)."""


class UploadTooLarge(RuntimeError):
    """A streamed upload exceeded config.MAX_UPLOAD_BYTES mid-stream (413)."""


def _now() -> datetime:
    """Naive UTC, matching app.checkout — SQLite reads DateTimes back naive, so
    every comparison in this module stays naive-to-naive."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------ signed tokens
def _serializer(salt: str) -> URLSafeTimedSerializer:
    if not config.SIGNING_KEY:
        raise SigningUnavailable("TILLA_SIGNING_KEY is not set")
    return URLSafeTimedSerializer(
        config.SIGNING_KEY,
        salt=salt,
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def mint_download_token(entitlement_id: int, deliverable_id: int) -> str:
    return _serializer(_DOWNLOAD_SALT).dumps({"e": entitlement_id, "d": deliverable_id})


def peek_download_token(token: str) -> dict:
    """Signature-only load (no expiry check) to recover {e, d} before the
    deliverable's per-link TTL is known. Raises BadSignature on a forged,
    tampered, or cross-salt token."""
    return _serializer(_DOWNLOAD_SALT).loads(token)


def load_download_token(token: str, max_age: int) -> dict:
    """Raises itsdangerous BadSignature (→403) / SignatureExpired (→410); the
    per-purpose salt means a session/redeliver token can never validate here."""
    return _serializer(_DOWNLOAD_SALT).loads(token, max_age=max_age)


def download_url(token: str) -> str:
    return f"{config.PUBLIC_BASE_URL}/api/download/{token}"


def mint_session_token(address: str) -> str:
    return _serializer(_SESSION_SALT).dumps({"a": address.lower()})


def load_session_token(token: str) -> str:
    return _serializer(_SESSION_SALT).loads(token, max_age=config.SESSION_TTL)["a"]


def mint_redeliver_token(order_id: str) -> str:
    return _serializer(_REDELIVER_SALT).dumps({"o": order_id})


def load_redeliver_token(token: str) -> str:
    return _serializer(_REDELIVER_SALT).loads(token, max_age=config.REDELIVER_TTL)["o"]


def mint_merchant_token(address: str) -> str:
    return _serializer(_MERCHANT_SALT).dumps({"a": address.lower()})


def load_merchant_token(token: str) -> str:
    """Raises BadSignature (→401 invalid) / SignatureExpired (→401 expired). The
    _MERCHANT_SALT means a buyer session token can never validate here, and a
    merchant token can never validate on /api/library (buyer salt)."""
    return _serializer(_MERCHANT_SALT).loads(
        token, max_age=config.MERCHANT_SESSION_TTL
    )["a"]


# ------------------------------------------------------------ manage key
def hash_manage_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def mint_manage_key() -> tuple[str, str]:
    """Return (plaintext, sha256 hex). The plaintext is shown once in the paid
    create-store response; only the hash is persisted."""
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_manage_key(plaintext)


def verify_manage_key(plaintext: str, stored_hash: str | None) -> bool:
    if not stored_hash or not plaintext:
        return False
    return hmac.compare_digest(hash_manage_key(plaintext), stored_hash)


# ------------------------------------------------------------ license key
def mint_license_key() -> str:
    """~100-bit key: TILLA- plus 4 groups of 5 base32 chars from 13 random bytes."""
    raw = base64.b32encode(secrets.token_bytes(13)).decode().rstrip("=")[:20]
    return "TILLA-" + "-".join(raw[i : i + 5] for i in range(0, 20, 5))


# ------------------------------------------------------------ file storage
def file_extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def sanitize_filename(name: str | None) -> str:
    """Content-Disposition value only — never a path component. Strips any
    directory parts, control chars, quotes, CR/LF, and non-ASCII, so a hostile
    filename cannot inject headers or escape a directory."""
    if not name:
        return "download"
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r'[\r\n"\x00-\x1f]', "", name).strip()
    return (name or "download")[:255]


def file_path(sha256: str):
    """Server-derived path: config.FILES_DIR/<sha[:2]>/<sha>. The hash is validated
    [0-9a-f]{64} so no user-controlled component ever reaches the filesystem."""
    if not _SHA256_RE.match(sha256):
        raise ValueError("invalid content hash")
    return config.FILES_DIR / sha256[:2] / sha256


async def store_upload(upload) -> dict:
    """Stream an UploadFile to disk in 64KB chunks, hashing as we go and aborting
    the moment the running byte total exceeds config.MAX_UPLOAD_BYTES (defeats a
    lying/absent Content-Length). On success os.replace the temp file into its
    content-addressed path (atomic, same filesystem); if the hash already exists,
    dedup by discarding the temp. Returns {sha256, size}."""
    tmp_dir = config.FILES_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / uuid.uuid4().hex
    hasher = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb") as fh:
            while True:
                chunk = await upload.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise UploadTooLarge("upload exceeds the size cap")
                hasher.update(chunk)
                fh.write(chunk)
        sha = hasher.hexdigest()
        dest = file_path(sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            os.replace(tmp_path, dest)
        return {"sha256": sha, "size": size}
    finally:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                os.remove(tmp_path)


# ------------------------------------------------------------ buyer sign-in
def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) and len(email) <= 255


def build_signin_message(
    address: str, nonce: str, issued_iso: str, expires_iso: str
) -> str:
    """Deterministic, domain+purpose-bound message. The server always rebuilds it
    from the stored nonce row, never trusts a client-supplied message."""
    return (
        "Tilla sign-in\n"
        f"domain: {config.DOMAIN}\n"
        f"address: {address}\n"
        f"nonce: {nonce}\n"
        f"issued: {issued_iso}\n"
        f"expires: {expires_iso}"
    )


def build_merchant_signin_message(
    address: str, nonce: str, issued_iso: str, expires_iso: str
) -> str:
    """Same nonce row, different purpose line ("Tilla merchant sign-in"). Because
    verify recovers the signer over the server-rebuilt message, a buyer-flow
    signature recovers to a garbage address on the merchant verify and 401s (and
    vice versa) — a captured buyer sign-in can never be upgraded to a merchant
    session, with no purpose column needed."""
    return (
        "Tilla merchant sign-in\n"
        f"domain: {config.DOMAIN}\n"
        f"address: {address}\n"
        f"nonce: {nonce}\n"
        f"issued: {issued_iso}\n"
        f"expires: {expires_iso}"
    )


def issue_nonce(session: Session, address: str) -> AuthNonce:
    """Create a fresh single-use nonce for `address`, opportunistically pruning
    this address's already-expired rows first."""
    addr = address.lower()
    now = _now()
    session.execute(
        delete(AuthNonce).where(AuthNonce.address == addr, AuthNonce.expires_at <= now)
    )
    row = AuthNonce(
        address=addr,
        nonce=secrets.token_urlsafe(16),
        issued_at=now,
        expires_at=now + timedelta(seconds=config.NONCE_TTL),
    )
    session.add(row)
    session.flush()
    return row


def _recover_signer(message: str, signature: str) -> str | None:
    """Recover the signer of `message`. Signatures are adversarial input from an
    unauthenticated caller and eth-account raises many exception types on a
    malformed one; any failure means "no valid signer" → the caller 401s, never
    500s. This is boundary validation, not error swallowing."""
    try:
        return Account.recover_message(
            encode_defunct(text=message), signature=signature
        )
    except Exception:  # noqa: BLE001 — untrusted signature, map every failure to 401
        return None


def verify_signin(
    session: Session, address: str, signature: str, purpose: str = "buyer"
) -> str | None:
    """Verify a wallet signature against this address's live nonces and, on the
    first that matches, consume it atomically (used_at IS NULL → now; a replay
    loses the conditional UPDATE) and mint a short-lived session token. Returns
    the token, or None on no match / replay / expiry / bad signature.

    ``purpose`` selects the signed-message text and the token salt: 'buyer' (the
    M4 default) or 'merchant' (M9). The replay-proof consume path is identical for
    both; the differing message purpose line means a signature minted for one
    purpose can never validate for the other."""
    if purpose == "merchant":
        build_message, mint_token = build_merchant_signin_message, mint_merchant_token
    else:
        build_message, mint_token = build_signin_message, mint_session_token
    addr = address.lower()
    now = _now()
    rows = session.scalars(
        select(AuthNonce)
        .where(
            AuthNonce.address == addr,
            AuthNonce.used_at.is_(None),
            AuthNonce.expires_at > now,
        )
        .order_by(AuthNonce.id.desc())
    ).all()
    for row in rows:
        message = build_message(
            addr, row.nonce, row.issued_at.isoformat(), row.expires_at.isoformat()
        )
        recovered = _recover_signer(message, signature)
        if recovered is None or recovered.lower() != addr:
            continue
        consumed = session.execute(
            update(AuthNonce)
            .where(AuthNonce.id == row.id, AuthNonce.used_at.is_(None))
            .values(used_at=now)
        ).rowcount
        if consumed == 1:
            return mint_token(addr)
    return None


# ------------------------------------------------------------ entitlements
def claim_download(session: Session, entitlement_id: int, max_downloads: int) -> bool:
    """Atomically consume one download slot. The conditional UPDATE (count < max
    AND not revoked) mirrors checkout.transition: N concurrent requests can never
    push download_count past the cap, since SQLite serializes writes."""
    rowcount = session.execute(
        update(Entitlement)
        .where(
            Entitlement.id == entitlement_id,
            Entitlement.download_count < max_downloads,
            Entitlement.revoked_at.is_(None),
        )
        .values(download_count=Entitlement.download_count + 1)
    ).rowcount
    return rowcount == 1


def revoke_entitlement(session: Session, order) -> bool:
    """Revoke the entitlement bound to `order` (M9's refunded transition calls
    this). Idempotent; download/validate already block a revoked entitlement."""
    ent = session.scalar(select(Entitlement).where(Entitlement.order_id == order.id))
    if ent is None or ent.revoked_at is not None:
        return False
    ent.revoked_at = _now()
    log_event(
        session,
        "delivery",
        "entitlement.revoked",
        store_id=order.store_id,
        order_id=order.id,
    )
    return True


# ------------------------------------------------------------ licenses
def activate_license(
    session: Session, ent: Entitlement, device_id: str, max_activations: int
) -> str:
    """Activate `device_id` against `ent`. Returns 'already_active' (idempotent,
    no count change), 'activated' (a slot was claimed), or 'at_limit' (409). A
    slot is claimed by a conditional counter UPDATE (SQLite serializes writes, so
    it can never exceed the cap) BEFORE any row is written, so the at-limit path
    leaves no partial state; the new row's insert only follows a successful bump,
    with UNIQUE(entitlement_id, device_id) undoing a concurrent duplicate."""
    existing = session.scalar(
        select(LicenseActivation).where(
            LicenseActivation.entitlement_id == ent.id,
            LicenseActivation.device_id == device_id,
        )
    )
    if existing is not None and existing.deactivated_at is None:
        return "already_active"  # same device already holds a slot

    bumped = session.execute(
        update(Entitlement)
        .where(
            Entitlement.id == ent.id,
            Entitlement.activations_used < max_activations,
            Entitlement.revoked_at.is_(None),
        )
        .values(activations_used=Entitlement.activations_used + 1)
    ).rowcount
    if bumped != 1:
        return "at_limit"

    if existing is not None:
        reactivated = session.execute(
            update(LicenseActivation)
            .where(
                LicenseActivation.id == existing.id,
                LicenseActivation.deactivated_at.is_not(None),
            )
            .values(deactivated_at=None, activated_at=_now())
        ).rowcount
        if reactivated != 1:
            # A concurrent activate() already reactivated this device; undo our
            # over-bump, mirroring the IntegrityError branch below.
            session.execute(
                update(Entitlement)
                .where(Entitlement.id == ent.id, Entitlement.activations_used > 0)
                .values(activations_used=Entitlement.activations_used - 1)
            )
            return "already_active"
        return "activated"
    try:
        with session.begin_nested():
            session.add(LicenseActivation(entitlement_id=ent.id, device_id=device_id))
            session.flush()
    except IntegrityError:
        # A concurrent request already inserted this device; undo our over-bump.
        session.execute(
            update(Entitlement)
            .where(Entitlement.id == ent.id, Entitlement.activations_used > 0)
            .values(activations_used=Entitlement.activations_used - 1)
        )
        return "already_active"
    return "activated"


def deactivate_license(session: Session, ent: Entitlement, device_id: str) -> bool:
    """Free the slot held by `device_id`, decrementing the counter (guarded
    activations_used > 0). Returns False if the device held no active slot."""
    row = session.scalar(
        select(LicenseActivation).where(
            LicenseActivation.entitlement_id == ent.id,
            LicenseActivation.device_id == device_id,
            LicenseActivation.deactivated_at.is_(None),
        )
    )
    if row is None:
        return False
    row.deactivated_at = _now()
    session.execute(
        update(Entitlement)
        .where(Entitlement.id == ent.id, Entitlement.activations_used > 0)
        .values(activations_used=Entitlement.activations_used - 1)
    )
    return True


def device_active(session: Session, ent: Entitlement, device_id: str) -> bool:
    return (
        session.scalar(
            select(LicenseActivation.id).where(
                LicenseActivation.entitlement_id == ent.id,
                LicenseActivation.device_id == device_id,
                LicenseActivation.deactivated_at.is_(None),
            )
        )
        is not None
    )


# ------------------------------------------------------------ email stub
def send_redelivery_email(session: Session, order, link: str) -> bool:
    """Send the re-delivery magic link. When SMTP is unconfigured (the default),
    this logs + writes an event_log row and no-ops — link generation and every
    test are fully buildable without real creds. Returns True iff a mail was sent.
    The caller is responsible for having validated order.buyer_email."""
    if not (config.SMTP_HOST and config.SMTP_FROM and order.buyer_email):
        logger.info("redelivery email no-op for order %s (SMTP unset)", order.id)
        log_event(
            session,
            "delivery",
            "email.skipped",
            store_id=order.store_id,
            order_id=order.id,
            data={"reason": "smtp_unconfigured"},
        )
        return False
    msg = EmailMessage()
    msg["Subject"] = "Your Tilla delivery"
    msg["From"] = config.SMTP_FROM
    msg["To"] = order.buyer_email
    msg.set_content(f"Your purchase is ready:\n\n{link}\n")
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as smtp:
            smtp.starttls()
            if config.SMTP_USER:
                smtp.login(config.SMTP_USER, config.SMTP_PASS)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException):
        logger.exception("redelivery email send failed for order %s", order.id)
        log_event(
            session,
            "delivery",
            "email.failed",
            store_id=order.store_id,
            order_id=order.id,
        )
        return False
    log_event(
        session,
        "delivery",
        "email.sent",
        store_id=order.store_id,
        order_id=order.id,
    )
    return True
