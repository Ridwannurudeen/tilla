"""M9 merchant platform: wallet-session + API-key auth, the multi-store read
surface, order detail, non-custodial refunds, CSV export, and webhook config.

Merchant identity is the wallet address, reusing the M4 nonce/session machinery
verbatim with a purpose-separated message (delivery.build_merchant_signin_message)
and a distinct token salt (delivery._MERCHANT_SALT). Every merchant route resolves
the caller through the single :func:`resolve_merchant` helper (session token or raw
API key), and every store/order/export query is filtered on ``merchant_id`` — the
one canonical IDOR gate. Non-owned or unknown resources uniformly 404.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, delivery
from app.db import get_session
from app.limiter import limiter
from app.models import Merchant, get_or_create_merchant, log_event

router = APIRouter()

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

API_KEY_PREFIX = "tilla_sk_"


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


class _MerchantAddressBody(BaseModel):
    address: str

    @field_validator("address")
    @classmethod
    def _v_addr(cls, v):
        if not _EVM_ADDRESS.fullmatch(v or ""):
            raise ValueError("address must be a 0x-prefixed 20-byte EVM address")
        return v.lower()


class MerchantNonceBody(_MerchantAddressBody):
    pass


class MerchantVerifyBody(_MerchantAddressBody):
    signature: str = Field(min_length=1, max_length=300)


# --------------------------------------------------------------- auth resolver
def resolve_merchant(session: Session, bearer: str) -> Merchant | None:
    """Resolve the merchant behind a bearer credential, or None. Two interchangeable
    surfaces: a merchant session token (itsdangerous, merchant salt) → wallet →
    merchant row; else a raw API key looked up by sha256-hex equality against
    merchants.api_key_hash (high-entropy key, hash-equality is the M4 pattern). The
    ONE place credentials are turned into an identity — the IDOR gate builds on it.
    """
    if not bearer:
        return None
    if config.SIGNING_KEY:
        with contextlib.suppress(BadSignature, SignatureExpired):
            wallet = delivery.load_merchant_token(bearer)
            merchant = session.scalar(
                select(Merchant).where(Merchant.wallet_address == wallet)
            )
            if merchant is not None:
                return merchant
    key_hash = hashlib.sha256(bearer.encode()).hexdigest()
    return session.scalar(select(Merchant).where(Merchant.api_key_hash == key_hash))


def _require_merchant(request: Request, session: Session) -> Merchant:
    merchant = resolve_merchant(session, _bearer(request))
    if merchant is None:
        raise HTTPException(401, "merchant authentication required")
    return merchant


# ------------------------------------------------------------------ auth routes
@router.post("/api/merchant/auth/nonce")
@limiter.limit("10/minute")
def merchant_nonce(
    request: Request,
    body: MerchantNonceBody,
    session: Session = Depends(get_session),
):
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    row = delivery.issue_nonce(session, body.address)
    message = delivery.build_merchant_signin_message(
        row.address, row.nonce, row.issued_at.isoformat(), row.expires_at.isoformat()
    )
    session.commit()
    return {
        "nonce": row.nonce,
        "message": message,
        "expires_at": row.expires_at.isoformat(),
    }


@router.post("/api/merchant/auth/verify")
@limiter.limit("10/minute")
def merchant_verify(
    request: Request,
    body: MerchantVerifyBody,
    session: Session = Depends(get_session),
):
    if not config.SIGNING_KEY:
        raise HTTPException(503, "sign-in not configured")
    token = delivery.verify_signin(
        session, body.address, body.signature, purpose="merchant"
    )
    if token is not None:
        # Lazily create the merchants row at first sign-in if create-store did not
        # already make it, so the receive wallet owns any stores it created.
        get_or_create_merchant(session, body.address)
    session.commit()
    if token is None:
        raise HTTPException(401, "signature verification failed")
    return {"session_token": token, "expires_in": config.MERCHANT_SESSION_TTL}


@router.post("/api/merchant/api-key")
@limiter.limit("10/minute")
def merchant_api_key(request: Request, session: Session = Depends(get_session)):
    """Mint (or rotate) the caller's API key. Returns the plaintext exactly once;
    only its sha256 hex is persisted (mirrors hash_manage_key). Re-calling
    overwrites the hash — the previous key is dead."""
    merchant = _require_merchant(request, session)
    plaintext = API_KEY_PREFIX + secrets.token_urlsafe(32)
    merchant.api_key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    log_event(session, "merchant", "api_key.minted", data={"merchant_id": merchant.id})
    session.commit()
    return {"api_key": plaintext}
