"""M9 merchant platform — auth, API keys, and buyer/merchant session separation.

The IDOR matrix, summary math, and CSV tests live alongside these as the read
surface / refund / export routes are exercised. All chain access is mocked; no
network, no funds.
"""

import hashlib

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app.db import SessionLocal
from app.models import Merchant

client = TestClient(main.app)


def _auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


# ---------------------------------------------------------------- sign-in helpers
def _merchant_nonce_message(address: str) -> str:
    r = client.post("/api/merchant/auth/nonce", json={"address": address})
    assert r.status_code == 200, r.text
    return r.json()["message"]


def _merchant_token(acct) -> str:
    message = _merchant_nonce_message(acct.address)
    sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
    rv = client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    )
    assert rv.status_code == 200, rv.text
    return rv.json()["session_token"]


def _buyer_nonce_message(address: str) -> str:
    r = client.post("/api/auth/nonce", json={"address": address})
    assert r.status_code == 200, r.text
    return r.json()["message"]


# ------------------------------------------------------------------------- tests
def test_merchant_signin_mints_token_and_creates_merchant():
    acct = Account.create()
    token = _merchant_token(acct)
    assert token
    # merchants row created lazily at first sign-in
    with SessionLocal() as s:
        m = s.scalar(
            select(Merchant).where(Merchant.wallet_address == acct.address.lower())
        )
        assert m is not None


def test_merchant_verify_wrong_wallet_401():
    acct_a = Account.create()
    acct_b = Account.create()
    message = _merchant_nonce_message(acct_a.address)
    # B signs A's nonce message -> recovered signer != claimed A -> 401
    sig = acct_b.sign_message(encode_defunct(text=message)).signature.hex()
    rv = client.post(
        "/api/merchant/auth/verify",
        json={"address": acct_a.address, "signature": sig},
    )
    assert rv.status_code == 401


def test_merchant_verify_replayed_signature_rejected():
    acct = Account.create()
    message = _merchant_nonce_message(acct.address)
    sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
    first = client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    )
    assert first.status_code == 200
    # nonce consumed -> replay is worthless
    again = client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    )
    assert again.status_code == 401


def test_buyer_purpose_signature_fails_on_merchant_verify():
    """A signature captured over the BUYER message can't upgrade to a merchant
    session: the merchant verify rebuilds the merchant-purpose message, so the
    recovered signer mismatches and it 401s."""
    acct = Account.create()
    # sign the buyer-purpose message, then submit it to the merchant verify
    buyer_msg = _buyer_nonce_message(acct.address)
    sig = acct.sign_message(encode_defunct(text=buyer_msg)).signature.hex()
    rv = client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    )
    assert rv.status_code == 401


def test_merchant_purpose_signature_fails_on_buyer_verify():
    """And the reverse — a merchant-purpose signature can't grant a buyer session."""
    acct = Account.create()
    merchant_msg = _merchant_nonce_message(acct.address)
    sig = acct.sign_message(encode_defunct(text=merchant_msg)).signature.hex()
    rv = client.post(
        "/api/auth/verify",
        json={"address": acct.address, "signature": sig},
    )
    assert rv.status_code == 401


def test_buyer_session_token_rejected_on_merchant_route():
    """A valid BUYER session token (different salt) never authenticates a merchant
    route."""
    acct = Account.create()
    buyer_msg = _buyer_nonce_message(acct.address)
    sig = acct.sign_message(encode_defunct(text=buyer_msg)).signature.hex()
    rv = client.post(
        "/api/auth/verify", json={"address": acct.address, "signature": sig}
    )
    buyer_token = rv.json()["session_token"]
    r = client.post("/api/merchant/api-key", headers=_auth(buyer_token))
    assert r.status_code == 401


def test_merchant_token_rejected_on_buyer_library():
    """A merchant token never validates on the buyer /api/library (buyer salt)."""
    acct = Account.create()
    merchant_token = _merchant_token(acct)
    r = client.get("/api/library", headers=_auth(merchant_token))
    assert r.status_code == 401


def test_api_key_mint_stores_only_hash_and_authenticates():
    acct = Account.create()
    token = _merchant_token(acct)
    r = client.post("/api/merchant/api-key", headers=_auth(token))
    assert r.status_code == 200
    key = r.json()["api_key"]
    assert key.startswith("tilla_sk_")
    with SessionLocal() as s:
        m = s.scalar(
            select(Merchant).where(Merchant.wallet_address == acct.address.lower())
        )
        # only the sha256 hex is persisted, never the plaintext
        assert m.api_key_hash == hashlib.sha256(key.encode()).hexdigest()
        assert m.api_key_hash != key
    # the raw key authenticates a merchant route (rotates, returns a new key)
    r2 = client.post("/api/merchant/api-key", headers=_auth(key))
    assert r2.status_code == 200


def test_api_key_rotation_kills_old_key():
    acct = Account.create()
    token = _merchant_token(acct)
    old = client.post("/api/merchant/api-key", headers=_auth(token)).json()["api_key"]
    new = client.post("/api/merchant/api-key", headers=_auth(old)).json()["api_key"]
    assert new != old
    # the rotated-away key no longer authenticates
    dead = client.post("/api/merchant/api-key", headers=_auth(old))
    assert dead.status_code == 401
    live = client.post("/api/merchant/api-key", headers=_auth(new))
    assert live.status_code == 200
