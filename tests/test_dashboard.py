"""M9 merchant platform — auth, API keys, and buyer/merchant session separation.

The IDOR matrix, summary math, and CSV tests live alongside these as the read
surface / refund / export routes are exercised. All chain access is mocked; no
network, no funds.
"""

import csv
import hashlib
import io
import secrets

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import checkout
from app.db import SessionLocal
from app.models import Merchant, Order, Product

client = TestClient(main.app)


def _auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


# ------------------------------------------------------------ sale seed helpers
def _order_id(slug: str) -> str:
    return client.post("/api/checkout/" + slug).json()["id"]


def _apply(cid: str, value: int, from_addr: str = "0x" + "2" * 40) -> None:
    """Drive an order through the real state machine with a given inbound value —
    no network. value == expected → delivered; < expected → underpaid; > → overpaid."""
    with SessionLocal() as s:
        o = s.get(Order, cid)
        checkout.apply_transfer(
            s,
            o,
            value,
            tx_hash="0x" + secrets.token_hex(32),
            log_index=0,
            block_number=1,
            from_addr=from_addr,
            head=10**9,
        )
        s.commit()


def _expected(cid: str) -> int:
    with SessionLocal() as s:
        return s.get(Order, cid).expected_micro


def _seed_sale(slug: str, from_addr: str = "0x" + "2" * 40) -> tuple[str, int]:
    cid = _order_id(slug)
    exp = _expected(cid)
    _apply(cid, exp, from_addr)
    return cid, exp


def _owned_store(acct, slug: str, make_store, price_micro: int = 9_000_000):
    """A live store whose receive wallet is `acct` — so signing in as `acct` owns
    it (merchant_id derives from pay_to at create time)."""
    make_store(slug=slug, pay_to=acct.address.lower(), price_micro=price_micro)


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


# ---------------------------------------------------- multi-store + summary math
def test_multi_store_lists_both_with_revenue(make_store):
    """BUILD.md M9 acceptance: one merchant → two stores (same receive wallet) →
    a sale on each → GET /stores lists both with correct net revenue."""
    acct = Account.create()
    _owned_store(acct, "storeone", make_store)
    _owned_store(acct, "storetwo", make_store)
    _, exp1 = _seed_sale("storeone")
    _, exp2 = _seed_sale("storetwo")
    token = _merchant_token(acct)

    data = client.get("/api/merchant/stores", headers=_auth(token)).json()
    stores = {s["slug"]: s for s in data["stores"]}
    assert set(stores) == {"storeone", "storetwo"}
    assert stores["storeone"]["revenue_micro"] == exp1
    assert stores["storetwo"]["revenue_micro"] == exp2
    assert stores["storeone"]["order_counts"].get("delivered") == 1


def test_summary_revenue_counts_and_underpaid(make_store):
    acct = Account.create()
    _owned_store(acct, "sumstore", make_store)
    _, exp = _seed_sale("sumstore")
    # an underpaid order that M9 refund resolution must surface
    under_cid = _order_id("sumstore")
    under_exp = _expected(under_cid)
    _apply(under_cid, under_exp - 1_000_000)
    token = _merchant_token(acct)

    s = client.get("/api/merchant/summary", headers=_auth(token)).json()
    assert s["store_count"] == 1
    assert s["revenue_micro"] == exp  # only the delivered sale counts
    assert s["counts"].get("delivered") == 1
    assert s["counts"].get("underpaid") == 1
    under_ids = {u["order_id"] for u in s["underpaid"]}
    assert under_cid in under_ids
    # per-product breakdown present with the delivered revenue
    assert s["products"] and s["products"][0]["revenue_micro"] == exp


def test_summary_overpaid_surfaced_as_outstanding(make_store):
    acct = Account.create()
    _owned_store(acct, "overstore", make_store)
    cid = _order_id("overstore")
    exp = _expected(cid)
    _apply(cid, exp + 2_000_000)  # overpaid → delivered, surplus 2 USDT0
    token = _merchant_token(acct)
    s = client.get("/api/merchant/summary", headers=_auth(token)).json()
    assert s["outstanding_overpaid_micro"] == 2_000_000


# ----------------------------------------------------------------- IDOR matrix
def test_idor_store_list_scoped_to_caller(make_store):
    a, b = Account.create(), Account.create()
    _owned_store(a, "a-store", make_store)
    _owned_store(b, "b-store", make_store)
    _seed_sale("a-store")
    tok_b = _merchant_token(b)
    stores = client.get("/api/merchant/stores", headers=_auth(tok_b)).json()["stores"]
    slugs = {s["slug"] for s in stores}
    assert slugs == {"b-store"}  # B never sees A's store


def test_idor_store_orders_and_order_detail_404_for_non_owner(make_store):
    a, b = Account.create(), Account.create()
    _owned_store(a, "aa-store", make_store)
    _owned_store(b, "bb-store", make_store)
    cid, _ = _seed_sale("aa-store")
    tok_b = _merchant_token(b)
    # B cannot list A's store orders, nor read A's order — uniform 404
    assert (
        client.get(
            "/api/merchant/stores/aa-store/orders", headers=_auth(tok_b)
        ).status_code
        == 404
    )
    assert (
        client.get("/api/merchant/orders/" + cid, headers=_auth(tok_b)).status_code
        == 404
    )
    # A (the owner) can
    tok_a = _merchant_token(a)
    assert (
        client.get("/api/merchant/orders/" + cid, headers=_auth(tok_a)).status_code
        == 200
    )


def test_unknown_store_and_order_are_404(make_store):
    acct = Account.create()
    _owned_store(acct, "known", make_store)
    token = _merchant_token(acct)
    assert (
        client.get("/api/merchant/stores/nope/orders", headers=_auth(token)).status_code
        == 404
    )
    assert (
        client.get("/api/merchant/orders/deadbeef", headers=_auth(token)).status_code
        == 404
    )


def test_order_detail_has_oklink_and_timeline(make_store):
    acct = Account.create()
    _owned_store(acct, "detailstore", make_store)
    cid, exp = _seed_sale("detailstore")
    token = _merchant_token(acct)
    d = client.get("/api/merchant/orders/" + cid, headers=_auth(token)).json()
    assert d["order_id"] == cid
    assert d["tx_url"].startswith("https://www.oklink.com/x-layer/tx/")
    events = {e["event"] for e in d["timeline"]}
    assert "order.confirmed" in events and "order.delivered" in events
    assert d["paid_usdt"] == f"{exp // 1_000_000}.{exp % 1_000_000:06d}"


# -------------------------------------------- _require_store_key merchant seam
def test_merchant_session_authorizes_store_key_seam(make_store):
    """A merchant session token authorizes the manage-key seam for a store it owns
    (pricing), and a non-owner merchant is rejected — manage keys still work."""
    a, b = Account.create(), Account.create()
    _owned_store(a, "seamstore", make_store)
    tok_a = _merchant_token(a)
    tok_b = _merchant_token(b)
    ok = client.post(
        "/api/stores/seamstore/pricing",
        headers=_auth(tok_a),
        json={"pricing_model": "one_time"},
    )
    assert ok.status_code == 200, ok.text
    denied = client.post(
        "/api/stores/seamstore/pricing",
        headers=_auth(tok_b),
        json={"pricing_model": "one_time"},
    )
    assert denied.status_code == 401


def test_dashboard_shell_renders_without_data():
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "personal_sign" in r.text
    assert "{{" not in r.text  # no Jinja leak; shell carries no server data


# --------------------------------------------------------------------- CSV export
def _rename_product(store_id: int, name: str) -> None:
    with SessionLocal() as s:
        p = s.scalar(select(Product).where(Product.store_id == store_id))
        p.name = name
        s.commit()


def test_orders_csv_header_and_row_scoped(make_store):
    acct = Account.create()
    _owned_store(acct, "csvstore", make_store)
    cid, exp = _seed_sale("csvstore")
    token = _merchant_token(acct)
    r = client.get("/api/merchant/export/orders.csv", headers=_auth(token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][0] == "order_id" and "paid_usdt" in rows[0]
    body = [row for row in rows[1:] if row]
    assert len(body) == 1 and body[0][0] == cid


def test_orders_csv_formula_injection_escaped(make_store):
    acct = Account.create()
    sid = make_store(slug="evilstore", pay_to=acct.address.lower())
    _rename_product(sid, '=HYPERLINK("http://evil","x")')
    _seed_sale("evilstore")
    token = _merchant_token(acct)
    r = client.get("/api/merchant/export/orders.csv", headers=_auth(token))
    rows = list(csv.reader(io.StringIO(r.text)))
    idx = rows[0].index("product_name")
    cell = rows[1][idx]
    assert cell.startswith("'=")  # neutralised for spreadsheets


def test_orders_csv_date_filter(make_store):
    acct = Account.create()
    _owned_store(acct, "datestore", make_store)
    _seed_sale("datestore")
    token = _merchant_token(acct)
    # a future `from` excludes the sale; a past `from` includes it
    future = client.get(
        "/api/merchant/export/orders.csv?from=2099-01-01", headers=_auth(token)
    )
    assert len([row for row in csv.reader(io.StringIO(future.text))][1:]) == 0
    past = client.get(
        "/api/merchant/export/orders.csv?from=2020-01-01", headers=_auth(token)
    )
    assert len([row for row in csv.reader(io.StringIO(past.text)) if row][1:]) == 1
    # a malformed date is a 422
    bad = client.get(
        "/api/merchant/export/orders.csv?from=notadate", headers=_auth(token)
    )
    assert bad.status_code == 422


def test_customers_csv_groups_by_buyer(make_store):
    acct = Account.create()
    _owned_store(acct, "custstore", make_store)
    _seed_sale("custstore", from_addr="0x" + "5" * 40)
    _seed_sale("custstore", from_addr="0x" + "5" * 40)  # same buyer, 2 orders
    _seed_sale("custstore", from_addr="0x" + "6" * 40)
    token = _merchant_token(acct)
    r = client.get("/api/merchant/export/customers.csv", headers=_auth(token))
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][0] == "from_addr"
    body = {row[0]: row for row in rows[1:] if row}
    assert body["0x" + "5" * 40][1] == "2"  # orders_count
    assert body["0x" + "6" * 40][1] == "1"


def test_export_store_param_must_be_owned(make_store):
    a, b = Account.create(), Account.create()
    _owned_store(a, "exp-a", make_store)
    _owned_store(b, "exp-b", make_store)
    tok_b = _merchant_token(b)
    # B asking to export A's store by slug → 404
    r = client.get("/api/merchant/export/orders.csv?store=exp-a", headers=_auth(tok_b))
    assert r.status_code == 404
    # B's own store scopes fine
    ok = client.get("/api/merchant/export/orders.csv?store=exp-b", headers=_auth(tok_b))
    assert ok.status_code == 200


def test_export_requires_merchant_auth():
    assert client.get("/api/merchant/export/orders.csv").status_code == 401
    assert client.get("/api/merchant/export/customers.csv").status_code == 401
