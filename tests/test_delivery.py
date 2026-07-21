"""M4 gated-delivery tests: uploads (size/type caps, path traversal), signed
download links (expiry, count limit, forgery, cross-salt, IDOR), buyer
wallet-signature auth (fresh/replay/stale/wrong-wallet/malformed), the buyer
library, the email + magic-link fallback, and the license lifecycle.

No real network or SMTP: orders are confirmed by driving the state machine
directly, wallets are local eth-account keys, and SMTP stays unconfigured so
send_redelivery_email no-ops.
"""

import asyncio
import threading

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as main
from app import checkout, config, delivery
from app.db import SessionLocal
from app.models import (
    Deliverable,
    Entitlement,
    EventLog,
    Order,
    Store,
)

client = TestClient(main.app)

PDF = b"%PDF-1.4\n" + b"tilla-payload-bytes" * 1000  # ~19KB, distinctive


# --------------------------------------------------------------- helpers
def _give_key(slug: str) -> str:
    """Mint a manage key for an existing store, persist its hash, return plaintext."""
    plain, key_hash = delivery.mint_manage_key()
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        store.manage_key_hash = key_hash
        s.commit()
    return plain


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _order(slug: str) -> str:
    return client.post(f"/api/checkout/{slug}").json()["id"]


def _confirm(cid: str, from_addr: str, tx_hash: str):
    """Confirm + deliver an order via the real state machine (no network)."""
    with SessionLocal() as s:
        order = s.get(Order, cid)
        checkout.apply_transfer(
            s,
            order,
            order.expected_micro - (order.paid_micro or 0),
            tx_hash=tx_hash,
            log_index=0,
            block_number=1,
            from_addr=from_addr,
            head=10**9,
        )
        s.commit()


def _upload_file(
    slug, key, *, filename="guide.pdf", content=PDF, ctype="application/pdf", extra=None
):
    files = {"file": (filename, content, ctype)}
    return client.post(
        f"/api/stores/{slug}/deliverable",
        headers=_auth(key),
        files=files,
        data=extra or {},
    )


def _buy_file(
    make_store,
    slug,
    buyer,
    *,
    tx="0x" + "1" * 64,
    filename="guide.pdf",
    content=PDF,
    extra=None,
):
    """Store with a file deliverable, one confirmed order paid by `buyer`. Returns
    (cid, download_url)."""
    make_store(slug=slug, pay_to="0x" + "a" * 40, delivery="LEGACY")
    key = _give_key(slug)
    assert (
        _upload_file(
            slug, key, filename=filename, content=content, extra=extra
        ).status_code
        == 200
    )
    cid = _order(slug)
    _confirm(cid, buyer, tx)
    body = client.get(f"/api/checkout/{cid}").json()
    return cid, body.get("download_url")


# --------------------------------------------------------------- uploads
def test_upload_stores_at_sha_path_with_correct_hash(make_store):
    import hashlib

    make_store(slug="up", pay_to="0x" + "a" * 40)
    key = _give_key("up")
    r = _upload_file("up", key)
    assert r.status_code == 200 and r.json()["kind"] == "file"

    sha = hashlib.sha256(PDF).hexdigest()
    with SessionLocal() as s:
        dv = s.scalar(select(Deliverable).where(Deliverable.kind == "file"))
        assert dv.file_sha256 == sha and dv.file_size == len(PDF)
    path = config.FILES_DIR / sha[:2] / sha
    assert path.exists() and path.read_bytes() == PDF


def test_upload_requires_manage_key(make_store):
    make_store(slug="noauth", pay_to="0x" + "a" * 40)
    _give_key("noauth")
    # missing header
    assert _upload_file("noauth", "").status_code == 401
    # wrong key
    r = client.post(
        "/api/stores/noauth/deliverable",
        headers=_auth("totally-wrong-key"),
        files={"file": ("g.pdf", PDF, "application/pdf")},
    )
    assert r.status_code == 401


def test_upload_disallowed_extension_rejected(make_store):
    make_store(slug="ext", pay_to="0x" + "a" * 40)
    key = _give_key("ext")
    for bad in ("evil.svg", "evil.html", "evil.exe", "evil"):
        r = _upload_file("ext", key, filename=bad, ctype="application/octet-stream")
        assert r.status_code == 422, bad


def test_upload_oversized_by_content_length_413(make_store, monkeypatch):
    make_store(slug="big", pay_to="0x" + "a" * 40)
    key = _give_key("big")
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1000)
    r = _upload_file(
        "big", key, content=b"x" * 5000, filename="big.zip", ctype="application/zip"
    )
    assert r.status_code == 413


def test_store_upload_aborts_midstream_on_lying_length(monkeypatch, tmp_path):
    # The running byte budget aborts regardless of any declared Content-Length,
    # and the temp file is cleaned up.
    monkeypatch.setattr(config, "FILES_DIR", tmp_path)
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 100)

    class _Stub:
        filename = "x.zip"
        content_type = "application/zip"

        def __init__(self):
            self._chunks = [b"a" * 64, b"b" * 64, b"c" * 64]

        async def read(self, size=-1):
            return self._chunks.pop(0) if self._chunks else b""

    import pytest

    with pytest.raises(delivery.UploadTooLarge):
        asyncio.run(delivery.store_upload(_Stub()))
    tmp_dir = tmp_path / "tmp"
    assert not tmp_dir.exists() or not any(tmp_dir.iterdir())  # temp cleaned


def test_upload_over_64kb_is_allowed_not_blocked_by_body_cap(make_store):
    # The upload route is exempt from the 64KB middleware; a 100KB file succeeds.
    make_store(slug="exempt", pay_to="0x" + "a" * 40)
    key = _give_key("exempt")
    big = b"z" * (100 * 1024)
    r = _upload_file(
        "exempt", key, content=big, filename="big.zip", ctype="application/zip"
    )
    assert r.status_code == 200 and r.json()["file_size"] == len(big)


def test_upload_filename_path_traversal_is_sanitized(make_store):
    make_store(slug="trav", pay_to="0x" + "a" * 40)
    key = _give_key("trav")
    r = _upload_file("trav", key, filename="../../../../etc/passwd.pdf")
    assert r.status_code == 200
    assert r.json()["file_name"] == "passwd.pdf"  # no path components survive
    with SessionLocal() as s:
        dv = s.scalar(select(Deliverable).where(Deliverable.kind == "file"))
        assert "/" not in dv.file_name and "\\" not in dv.file_name


def test_replace_deliverable_flips_old_inactive(make_store):
    make_store(slug="rep", pay_to="0x" + "a" * 40)
    key = _give_key("rep")
    assert _upload_file("rep", key).status_code == 200
    r2 = client.post(
        "/api/stores/rep/deliverable",
        headers=_auth(key),
        json={"kind": "text", "payload": "SECRET"},
    )
    assert r2.status_code == 200
    with SessionLocal() as s:
        rows = s.scalars(select(Deliverable).order_by(Deliverable.id)).all()
        assert len(rows) == 2
        assert rows[0].active is False and rows[1].active is True


# --------------------------------------------------------------- downloads
def test_download_returns_identical_bytes(make_store):
    _cid, url = _buy_file(make_store, "dl", "0x" + "b" * 40)
    assert url
    token = url.rsplit("/", 1)[-1]
    r = client.get(f"/api/download/{token}")
    assert r.status_code == 200
    assert r.content == PDF
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_download_count_limit_enforced(make_store):
    _cid, url = _buy_file(
        make_store, "lim", "0x" + "b" * 40, extra={"max_downloads": "2"}
    )
    token = url.rsplit("/", 1)[-1]
    assert client.get(f"/api/download/{token}").status_code == 200
    assert client.get(f"/api/download/{token}").status_code == 200
    assert client.get(f"/api/download/{token}").status_code == 410  # third over cap


def test_download_count_limit_is_race_proof(make_store):
    # N concurrent claims never exceed max_downloads (conditional-UPDATE proof).
    _cid, url = _buy_file(
        make_store, "race", "0x" + "b" * 40, extra={"max_downloads": "3"}
    )
    with SessionLocal() as s:
        ent = s.scalar(select(Entitlement))
        ent_id, dv = ent.id, s.get(Deliverable, ent.deliverable_id)
        max_dl = dv.max_downloads
    granted = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        with SessionLocal() as s:
            ok = delivery.claim_download(s, ent_id, max_dl)
            s.commit()
            granted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert granted.count(True) == max_dl  # exactly the cap, never more
    with SessionLocal() as s:
        assert s.get(Entitlement, ent_id).download_count == max_dl


def test_download_token_expired_returns_410(make_store):
    _cid, url = _buy_file(make_store, "exp", "0x" + "b" * 40)
    token = url.rsplit("/", 1)[-1]
    # Force expiry by making the deliverable's link TTL negative (any token stale).
    with SessionLocal() as s:
        s.scalar(
            select(Deliverable).where(Deliverable.kind == "file")
        ).link_ttl_seconds = -1
        s.commit()
    assert client.get(f"/api/download/{token}").status_code == 410


def test_download_forged_or_tampered_token_403(make_store):
    _cid, url = _buy_file(make_store, "forge", "0x" + "b" * 40)
    token = url.rsplit("/", 1)[-1]
    assert client.get("/api/download/garbage.not.a.token").status_code == 403
    tampered = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    assert client.get(f"/api/download/{tampered}").status_code == 403


def test_download_cross_salt_session_token_rejected(make_store):
    # A token minted for the session purpose must never validate as a download.
    make_store(slug="salt", pay_to="0x" + "a" * 40)
    session_token = delivery.mint_session_token("0x" + "b" * 40)
    assert client.get(f"/api/download/{session_token}").status_code == 403


def test_download_blocked_after_revocation(make_store):
    cid, url = _buy_file(make_store, "rev", "0x" + "b" * 40)
    token = url.rsplit("/", 1)[-1]
    with SessionLocal() as s:
        delivery.revoke_entitlement(s, s.get(Order, cid))
        s.commit()
    assert client.get(f"/api/download/{token}").status_code == 403


def test_download_blocked_when_order_refunded(make_store):
    cid, url = _buy_file(make_store, "refund", "0x" + "b" * 40)
    token = url.rsplit("/", 1)[-1]
    with SessionLocal() as s:
        s.get(Order, cid).status = "refunded"
        s.commit()
    assert client.get(f"/api/download/{token}").status_code == 403


def test_download_missing_file_404_does_not_consume_slot(make_store):
    # The file-existence check runs before the download slot is claimed, so a
    # missing file 404s without burning a non-refundable download credit.
    _cid, url = _buy_file(make_store, "gone", "0x" + "b" * 40)
    token = url.rsplit("/", 1)[-1]
    with SessionLocal() as s:
        dv = s.scalar(select(Deliverable).where(Deliverable.kind == "file"))
        delivery.file_path(dv.file_sha256).unlink()
    assert client.get(f"/api/download/{token}").status_code == 404
    with SessionLocal() as s:
        assert s.scalar(select(Entitlement)).download_count == 0


def test_idor_buyer_cannot_see_or_download_another_buyers_purchase(make_store):
    # Two buyers, one store, distinct wallets. Each library shows only its own
    # purchase, and buyer A never receives a token for buyer B's entitlement.
    make_store(slug="idor", pay_to="0x" + "a" * 40, delivery="LEGACY")
    key = _give_key("idor")
    assert _upload_file("idor", key).status_code == 200

    acct_a, acct_b = Account.create(), Account.create()
    cid_a = _order("idor")
    _confirm(cid_a, acct_a.address.lower(), "0x" + "1" * 64)
    cid_b = _order("idor")
    _confirm(cid_b, acct_b.address.lower(), "0x" + "2" * 64)

    tok_a = _session_token(acct_a)
    lib_a = client.get("/api/library", headers=_auth(tok_a)).json()
    order_ids = {p["order_id"] for p in lib_a["purchases"]}
    assert order_ids == {cid_a}  # A sees only A's purchase, never B's


# --------------------------------------------------------- wallet sign-in
def _session_token(acct) -> str:
    r = client.post("/api/auth/nonce", json={"address": acct.address})
    message = r.json()["message"]
    sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
    rv = client.post(
        "/api/auth/verify", json={"address": acct.address, "signature": sig}
    )
    assert rv.status_code == 200, rv.text
    return rv.json()["session_token"]


def test_wallet_signin_valid_signature_grants_library_access(make_store):
    make_store(slug="signin", pay_to="0x" + "a" * 40, delivery="LEGACY")
    key = _give_key("signin")
    _upload_file("signin", key)
    acct = Account.create()
    cid = _order("signin")
    _confirm(cid, acct.address.lower(), "0x" + "3" * 64)

    token = _session_token(acct)
    lib = client.get("/api/library", headers=_auth(token))
    assert lib.status_code == 200
    assert lib.json()["wallet"] == acct.address.lower()
    assert lib.json()["purchases"][0]["order_id"] == cid
    assert lib.json()["purchases"][0]["download_url"]  # fresh link re-issued


def test_wallet_signin_replayed_signature_rejected():
    acct = Account.create()
    r = client.post("/api/auth/nonce", json={"address": acct.address})
    message = r.json()["message"]
    sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
    first = client.post(
        "/api/auth/verify", json={"address": acct.address, "signature": sig}
    )
    assert first.status_code == 200
    # same signature again: the nonce is consumed -> replay is worthless
    replay = client.post(
        "/api/auth/verify", json={"address": acct.address, "signature": sig}
    )
    assert replay.status_code == 401


def test_wallet_signin_stale_nonce_rejected():
    from app.models import AuthNonce

    acct = Account.create()
    r = client.post("/api/auth/nonce", json={"address": acct.address})
    message = r.json()["message"]
    sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
    with SessionLocal() as s:
        row = s.scalar(
            select(AuthNonce).where(AuthNonce.address == acct.address.lower())
        )
        row.expires_at = checkout._now().replace(year=2000)
        s.commit()
    r = client.post(
        "/api/auth/verify", json={"address": acct.address, "signature": sig}
    )
    assert r.status_code == 401


def test_wallet_signin_wrong_wallet_rejected():
    acct_a, acct_b = Account.create(), Account.create()
    r = client.post("/api/auth/nonce", json={"address": acct_a.address})
    message = r.json()["message"]
    # B signs A's message; claimed address is A -> recovered signer != A
    sig = acct_b.sign_message(encode_defunct(text=message)).signature.hex()
    r = client.post(
        "/api/auth/verify", json={"address": acct_a.address, "signature": sig}
    )
    assert r.status_code == 401


def test_wallet_signin_malformed_signature_is_401_not_500():
    acct = Account.create()
    client.post("/api/auth/nonce", json={"address": acct.address})
    r = client.post(
        "/api/auth/verify",
        json={"address": acct.address, "signature": "0xdeadbeef"},
    )
    assert r.status_code == 401


def test_library_requires_valid_session():
    assert client.get("/api/library", headers=_auth("not-a-token")).status_code == 401
    assert client.get("/api/library").status_code == 401


# --------------------------------------------------- email + magic link
def test_email_capture_validates_and_stores(make_store):
    make_store(slug="mail", pay_to="0x" + "a" * 40)
    key = _give_key("mail")
    cid = _order("mail")
    assert (
        client.post(
            f"/api/checkout/{cid}/email",
            headers=_auth(key),
            json={"email": "not-an-email"},
        ).status_code
        == 422
    )
    # header-injection attempt is stripped and then fails validation
    bad = client.post(
        f"/api/checkout/{cid}/email",
        headers=_auth(key),
        json={"email": "a@b.com\r\nBcc: x@y.com"},
    )
    assert bad.status_code == 422
    ok = client.post(
        f"/api/checkout/{cid}/email",
        headers=_auth(key),
        json={"email": "buyer@example.com"},
    )
    assert ok.status_code == 200
    with SessionLocal() as s:
        assert s.get(Order, cid).buyer_email == "buyer@example.com"


def test_email_requires_order_proof(make_store):
    # An attacker who only knows the cid cannot overwrite buyer_email or trigger a
    # redelivery: proof is the store manage key or the payer's wallet session.
    make_store(slug="mailauth", pay_to="0x" + "a" * 40, delivery="LEGACY")
    key = _give_key("mailauth")
    acct = Account.create()
    cid = _order("mailauth")
    _confirm(cid, acct.address.lower(), "0x" + "7" * 64)

    body = {"email": "buyer@example.com"}
    assert client.post(f"/api/checkout/{cid}/email", json=body).status_code == 401
    assert (
        client.post(
            f"/api/checkout/{cid}/email", headers=_auth("wrong-key"), json=body
        ).status_code
        == 401
    )
    # a different wallet's session is not proof for this order
    other = _session_token(Account.create())
    assert (
        client.post(
            f"/api/checkout/{cid}/email", headers=_auth(other), json=body
        ).status_code
        == 401
    )
    # the payer's own wallet session is accepted
    assert (
        client.post(
            f"/api/checkout/{cid}/email",
            headers=_auth(_session_token(acct)),
            json=body,
        ).status_code
        == 200
    )
    # so is the store manage key
    assert (
        client.post(
            f"/api/checkout/{cid}/email", headers=_auth(key), json=body
        ).status_code
        == 200
    )
    with SessionLocal() as s:
        assert s.get(Order, cid).buyer_email == "buyer@example.com"


def test_redeliver_magic_link_returns_payload(make_store):
    make_store(slug="redeliv", pay_to="0x" + "a" * 40, delivery="LEGACY")
    key = _give_key("redeliv")
    _upload_file("redeliv", key)
    cid = _order("redeliv")
    _confirm(cid, "0x" + "b" * 40, "0x" + "4" * 64)

    token = delivery.mint_redeliver_token(cid)
    r = client.get(f"/api/redeliver/{token}")
    assert r.status_code == 200
    assert r.json()["status"] == "paid"
    assert r.json()["download_url"]


def test_redeliver_expired_link_410(make_store):
    make_store(slug="redx", pay_to="0x" + "a" * 40)
    cid = _order("redx")
    # a token whose max_age is exceeded: monkeypatch-free via a tiny REDELIVER_TTL
    token = delivery.mint_redeliver_token(cid)
    import app.config as cfg

    orig = cfg.REDELIVER_TTL
    cfg.REDELIVER_TTL = -1
    try:
        assert client.get(f"/api/redeliver/{token}").status_code == 410
    finally:
        cfg.REDELIVER_TTL = orig


def test_send_redelivery_email_noops_without_smtp(make_store):
    make_store(slug="smtp", pay_to="0x" + "a" * 40)
    cid = _order("smtp")
    with SessionLocal() as s:
        order = s.get(Order, cid)
        order.buyer_email = "x@y.com"
        sent = delivery.send_redelivery_email(s, order, "https://example/redeliver/tok")
        s.commit()
    assert sent is False
    with SessionLocal() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(EventLog)
                .where(EventLog.event == "email.skipped")
            )
            == 1
        )


# --------------------------------------------------------------- licenses
def _license_key(make_store, slug) -> str:
    make_store(slug=slug, pay_to="0x" + "a" * 40, delivery="LEGACY")
    key = _give_key(slug)
    assert (
        client.post(
            f"/api/stores/{slug}/deliverable",
            headers=_auth(key),
            json={"kind": "license", "max_activations": 2},
        ).status_code
        == 200
    )
    cid = _order(slug)
    _confirm(cid, "0x" + "b" * 40, "0x" + "5" * 64)
    body = client.get(f"/api/checkout/{cid}").json()
    assert body["license_key"].startswith("TILLA-")
    return body["license_key"]


def test_license_issued_on_sale_and_unique(make_store):
    lk1 = _license_key(make_store, "lic1")
    with SessionLocal() as s:
        n = s.scalar(
            select(func.count())
            .select_from(Entitlement)
            .where(Entitlement.license_key == lk1)
        )
        assert n == 1


def test_license_activate_up_to_limit_then_409(make_store):
    lk = _license_key(make_store, "liclim")
    assert (
        client.post(
            "/api/licenses/activate", json={"license_key": lk, "device_id": "dev-1"}
        ).json()["status"]
        == "activated"
    )
    assert (
        client.post(
            "/api/licenses/activate", json={"license_key": lk, "device_id": "dev-2"}
        ).json()["status"]
        == "activated"
    )
    r3 = client.post(
        "/api/licenses/activate", json={"license_key": lk, "device_id": "dev-3"}
    )
    assert r3.status_code == 409  # max_activations=2 exhausted


def test_license_reactivate_same_device_is_idempotent(make_store):
    lk = _license_key(make_store, "licidem")
    a = client.post(
        "/api/licenses/activate", json={"license_key": lk, "device_id": "d1"}
    )
    assert a.json()["status"] == "activated" and a.json()["activations"] == 1
    b = client.post(
        "/api/licenses/activate", json={"license_key": lk, "device_id": "d1"}
    )
    assert (
        b.json()["status"] == "already_active" and b.json()["activations"] == 1
    )  # no bump


def test_license_reactivate_after_deactivate_reclaims_one_slot(make_store):
    # Reactivating a previously-deactivated device claims exactly one slot back
    # via the conditional-UPDATE reactivation path (never leaks a seat).
    lk = _license_key(make_store, "licreact")
    client.post("/api/licenses/activate", json={"license_key": lk, "device_id": "d1"})
    client.post("/api/licenses/deactivate", json={"license_key": lk, "device_id": "d1"})
    with SessionLocal() as s:
        assert s.scalar(select(Entitlement)).activations_used == 0
    r = client.post(
        "/api/licenses/activate", json={"license_key": lk, "device_id": "d1"}
    )
    assert r.json()["status"] == "activated" and r.json()["activations"] == 1
    with SessionLocal() as s:
        assert s.scalar(select(Entitlement)).activations_used == 1


def test_license_deactivate_frees_a_slot(make_store):
    lk = _license_key(make_store, "licdeact")
    client.post("/api/licenses/activate", json={"license_key": lk, "device_id": "d1"})
    client.post("/api/licenses/activate", json={"license_key": lk, "device_id": "d2"})
    # at limit
    assert (
        client.post(
            "/api/licenses/activate", json={"license_key": lk, "device_id": "d3"}
        ).status_code
        == 409
    )
    # free one, then a new device fits
    d = client.post(
        "/api/licenses/deactivate", json={"license_key": lk, "device_id": "d1"}
    )
    assert d.json()["deactivated"] is True and d.json()["activations"] == 1
    assert (
        client.post(
            "/api/licenses/activate", json={"license_key": lk, "device_id": "d3"}
        ).json()["status"]
        == "activated"
    )


def test_license_validate_true_false_across_states(make_store):
    lk = _license_key(make_store, "licval")
    client.post("/api/licenses/activate", json={"license_key": lk, "device_id": "d1"})
    # valid key, no device -> valid
    assert (
        client.post("/api/licenses/validate", json={"license_key": lk}).json()["valid"]
        is True
    )
    # valid key + active device -> valid
    assert (
        client.post(
            "/api/licenses/validate", json={"license_key": lk, "device_id": "d1"}
        ).json()["valid"]
        is True
    )
    # valid key + inactive device -> not valid for that device
    assert (
        client.post(
            "/api/licenses/validate", json={"license_key": lk, "device_id": "nope"}
        ).json()["valid"]
        is False
    )
    # unknown key -> uniform valid:false (no exists-oracle)
    assert client.post(
        "/api/licenses/validate", json={"license_key": "TILLA-XXXXX-XXXXX-XXXXX-XXXXX"}
    ).json() == {"valid": False}


def test_license_validate_false_after_revocation(make_store):
    lk = _license_key(make_store, "licrev")
    with SessionLocal() as s:
        ent = s.scalar(select(Entitlement).where(Entitlement.license_key == lk))
        delivery.revoke_entitlement(s, s.get(Order, ent.order_id))
        s.commit()
    assert client.post("/api/licenses/validate", json={"license_key": lk}).json() == {
        "valid": False
    }


# --------------------------------------------------------------- fail-closed
def test_gated_endpoints_503_when_signing_key_unset(make_store, monkeypatch):
    make_store(slug="closed", pay_to="0x" + "a" * 40, delivery="LEGACY-STILL-WORKS")
    key = _give_key("closed")
    monkeypatch.setattr(config, "SIGNING_KEY", "")
    assert _upload_file("closed", key).status_code == 503
    assert client.get("/api/download/anytoken").status_code == 503
    assert (
        client.post("/api/auth/nonce", json={"address": "0x" + "c" * 40}).status_code
        == 503
    )
    assert client.get("/api/library").status_code == 503
    # legacy text delivery is untouched: a plain checkout still confirms + delivers
    cid = _order("closed")
    _confirm(cid, "0x" + "b" * 40, "0x" + "6" * 64)
    body = client.get(f"/api/checkout/{cid}").json()
    assert body["status"] == "paid" and body["delivery"] == "LEGACY-STILL-WORKS"
