"""Phase 4 custom domains — hostname validation, the DNS TXT ownership challenge,
fail-closed host-based store resolution, and the no-hijack uniqueness invariant.

DNS is never touched: the verify flow monkeypatches app.domains._lookup_txt so the
real match logic runs against a controlled TXT answer. No network, no funds.
"""

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app import config, domains
from app.db import SessionLocal
from app.models import Store

client = TestClient(main.app)

DOMAIN = "shop.example.com"


def _auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def _merchant_token(acct) -> str:
    r = client.post("/api/merchant/auth/nonce", json={"address": acct.address})
    assert r.status_code == 200, r.text
    message = r.json()["message"]
    sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
    rv = client.post(
        "/api/merchant/auth/verify",
        json={"address": acct.address, "signature": sig},
    )
    assert rv.status_code == 200, rv.text
    return rv.json()["session_token"]


def _seed_store(acct, slug: str, make_store, content: dict | None = None) -> None:
    make_store(slug=slug, pay_to=acct.address.lower())
    if content is not None:
        with SessionLocal() as s:
            row = s.scalar(select(Store).where(Store.slug == slug))
            row.content = content
            s.commit()


# ------------------------------------------------------------- hostname validation
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Shop.Example.COM", "shop.example.com"),
        ("shop.example.com.", "shop.example.com"),
        ("  store.acme.io  ", "store.acme.io"),
    ],
)
def test_normalize_domain_ok(raw, expected):
    assert domains.normalize_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "localhost",
        "example",  # single label
        "10.0.0.1",  # IP literal
        "::1",  # IPv6 literal
        "https://shop.example.com",  # scheme
        "shop.example.com/path",  # path
        "shop.example.com:8080",  # port
        "shop..example.com",  # empty label
        "-bad.example.com",  # leading hyphen
        "a b.example.com",  # whitespace
    ],
)
def test_normalize_domain_rejected(raw):
    with pytest.raises(domains.DomainError):
        domains.normalize_domain(raw)


def test_normalize_domain_rejects_platform_host(monkeypatch):
    monkeypatch.setattr(domains.config, "PUBLIC_BASE_URL", "https://tilla.gudman.xyz")
    with pytest.raises(domains.DomainError):
        domains.normalize_domain("tilla.gudman.xyz")


# --------------------------------------------------------------------- claim + auth
def test_claim_requires_auth(make_store):
    acct = Account.create()
    _seed_store(acct, "s1", make_store)
    r = client.post("/api/merchant/stores/s1/custom-domain", json={"domain": DOMAIN})
    assert r.status_code == 401


def test_claim_non_owner_404(make_store):
    owner, stranger = Account.create(), Account.create()
    _seed_store(owner, "s1", make_store)
    tok = _merchant_token(stranger)
    r = client.post(
        "/api/merchant/stores/s1/custom-domain",
        json={"domain": DOMAIN},
        headers=_auth(tok),
    )
    assert r.status_code == 404


def test_claim_generates_token_and_record(make_store):
    acct = Account.create()
    _seed_store(acct, "s1", make_store)
    tok = _merchant_token(acct)
    r = client.post(
        "/api/merchant/stores/s1/custom-domain",
        json={"domain": DOMAIN},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["custom_domain"] == DOMAIN
    assert body["verified"] is False
    rec = body["dns_record"]
    assert rec["type"] == "TXT"
    assert rec["name"] == "_tilla-challenge.shop.example.com"
    assert rec["value"].startswith("tilla-domain-verification=")


def test_claim_invalid_domain_422(make_store):
    acct = Account.create()
    _seed_store(acct, "s1", make_store)
    tok = _merchant_token(acct)
    r = client.post(
        "/api/merchant/stores/s1/custom-domain",
        json={"domain": "localhost"},
        headers=_auth(tok),
    )
    assert r.status_code == 422


def test_domain_cannot_be_hijacked(make_store):
    owner_a, owner_b = Account.create(), Account.create()
    _seed_store(owner_a, "sa", make_store)
    _seed_store(owner_b, "sb", make_store)
    tok_a = _merchant_token(owner_a)
    tok_b = _merchant_token(owner_b)
    ok = client.post(
        "/api/merchant/stores/sa/custom-domain",
        json={"domain": DOMAIN},
        headers=_auth(tok_a),
    )
    assert ok.status_code == 200
    clash = client.post(
        "/api/merchant/stores/sb/custom-domain",
        json={"domain": DOMAIN},
        headers=_auth(tok_b),
    )
    assert clash.status_code == 409


# --------------------------------------------------------------- verify + fail-closed
def _content() -> dict:
    return {
        "store_name": "Bean Co",
        "tagline": "Fresh beans",
        "hero_subcopy": "Single-origin {{7*7}}",
        "product_name": "Beans",
        "product_blurb": "Great beans",
        "price_usdt": 9,
    }


def _claim(acct, slug: str) -> None:
    tok = _merchant_token(acct)
    r = client.post(
        f"/api/merchant/stores/{slug}/custom-domain",
        json={"domain": DOMAIN},
        headers=_auth(tok),
    )
    assert r.status_code == 200, r.text


def test_unverified_domain_serves_nothing(make_store):
    acct = Account.create()
    _seed_store(acct, "s1", make_store, content=_content())
    _claim(acct, "s1")
    # host resolution is fail-closed: unverified domain -> 404
    r = client.get("/", headers={"host": DOMAIN})
    assert r.status_code == 404


def test_verify_no_record_stays_unverified(make_store, monkeypatch):
    acct = Account.create()
    _seed_store(acct, "s1", make_store, content=_content())
    _claim(acct, "s1")
    monkeypatch.setattr(domains, "_lookup_txt", lambda name: [])
    tok = _merchant_token(acct)
    r = client.post("/api/merchant/stores/s1/custom-domain/verify", headers=_auth(tok))
    assert r.status_code == 422
    r2 = client.get("/", headers={"host": DOMAIN})
    assert r2.status_code == 404


def test_verify_then_serves_store(make_store, monkeypatch):
    acct = Account.create()
    _seed_store(acct, "s1", make_store, content=_content())
    _claim(acct, "s1")
    tok = _merchant_token(acct)
    # publish the exact challenge token as the TXT record
    with SessionLocal() as s:
        token = s.scalar(select(Store).where(Store.slug == "s1")).custom_domain_token
    monkeypatch.setattr(
        domains, "_lookup_txt", lambda name: [domains.txt_record_value(token)]
    )
    v = client.post("/api/merchant/stores/s1/custom-domain/verify", headers=_auth(tok))
    assert v.status_code == 200, v.text
    assert v.json()["verified"] is True

    r = client.get("/", headers={"host": DOMAIN})
    assert r.status_code == 200
    html = r.text
    # canonical + OG resolve to the custom domain root
    assert f'href="https://{DOMAIN}/"' in html
    assert f"https://{DOMAIN}/og.png" in html
    # SSTI canary: merchant copy is data — {{7*7}} renders literally at the injection
    # point, never evaluated to 49. (Scope the negative to the injected copy — a bare
    # "49 not in html" is flaky: random hex ids/tx hashes on the page can contain "49".)
    assert "Single-origin {{7*7}}" in html
    assert "Single-origin 49" not in html


def test_release_stops_serving(make_store, monkeypatch):
    acct = Account.create()
    _seed_store(acct, "s1", make_store, content=_content())
    _claim(acct, "s1")
    tok = _merchant_token(acct)
    with SessionLocal() as s:
        token = s.scalar(select(Store).where(Store.slug == "s1")).custom_domain_token
    monkeypatch.setattr(
        domains, "_lookup_txt", lambda name: [domains.txt_record_value(token)]
    )
    client.post("/api/merchant/stores/s1/custom-domain/verify", headers=_auth(tok))
    assert client.get("/", headers={"host": DOMAIN}).status_code == 200

    d = client.delete("/api/merchant/stores/s1/custom-domain", headers=_auth(tok))
    assert d.status_code == 200
    assert client.get("/", headers={"host": DOMAIN}).status_code == 404


def test_verified_domain_serves_feed_and_llms_at_root(make_store, monkeypatch):
    """The theme advertises the feed relative to the canonical URL, which on a custom
    domain is the root — so /feed.json and /llms.txt must resolve there, or the page
    links a 404 at exactly the crawlers and agents most likely to follow it."""
    acct = Account.create()
    _seed_store(acct, "s1", make_store, content=_content())
    _claim(acct, "s1")
    tok = _merchant_token(acct)
    with SessionLocal() as s:
        token = s.scalar(select(Store).where(Store.slug == "s1")).custom_domain_token
    monkeypatch.setattr(
        domains, "_lookup_txt", lambda name: [domains.txt_record_value(token)]
    )
    client.post("/api/merchant/stores/s1/custom-domain/verify", headers=_auth(tok))

    page = client.get("/", headers={"host": DOMAIN}).text
    assert f'href="https://{DOMAIN}/feed.json"' in page  # what the page advertises

    feed = client.get("/feed.json", headers={"host": DOMAIN})
    assert feed.status_code == 200
    assert feed.json()["products"]

    llms = client.get("/llms.txt", headers={"host": DOMAIN})
    assert llms.status_code == 200
    assert "s1" in llms.text or "Single-origin" in llms.text


def test_unknown_host_root_is_404():
    # no verified custom domain matches an arbitrary host -> fail-closed
    assert client.get("/", headers={"host": "random.example.org"}).status_code == 404
    # and the root feed surfaces are fail-closed on the same host check
    assert (
        client.get("/feed.json", headers={"host": "random.example.org"}).status_code
        == 404
    )
    assert (
        client.get("/llms.txt", headers={"host": "random.example.org"}).status_code
        == 404
    )


# ------------------------------------------- platform subdomains: <slug>.DOMAIN
def _live_store(make_store, slug="sub-shop"):
    acct = Account.create()
    _seed_store(acct, slug, make_store, content=_content())
    return slug


def test_platform_subdomain_serves_a_live_store(make_store):
    slug = _live_store(make_store)
    host = f"{slug}.{config.DOMAIN}"
    r = client.get("/", headers={"host": host})
    assert r.status_code == 200
    # canonical/OG must name the subdomain, not the literal "None" that reading
    # store.custom_domain would produce for a store that has no custom domain
    assert f'href="https://{host}/"' in r.text
    assert "https://None" not in r.text


def test_platform_subdomain_serves_the_root_feed_and_llms(make_store):
    slug = _live_store(make_store, "sub-feed")
    host = f"{slug}.{config.DOMAIN}"
    feed = client.get("/feed.json", headers={"host": host})
    assert feed.status_code == 200
    assert feed.json()["products"]
    assert client.get("/llms.txt", headers={"host": host}).status_code == 200


def test_platform_host_itself_is_not_a_store(make_store):
    """The bare platform domain must keep serving the landing page, not a store."""
    _live_store(make_store, "sub-bare")
    assert client.get("/", headers={"host": config.DOMAIN}).status_code == 404


@pytest.mark.parametrize("reserved", ["api", "admin", "health", "s", "static"])
def test_reserved_labels_can_never_serve_a_store(make_store, reserved):
    """Host is attacker-controlled. api.tilla.gudman.xyz must not be able to serve a
    storefront even if someone contrives a store row named for a reserved route."""
    assert (
        client.get("/", headers={"host": f"{reserved}.{config.DOMAIN}"}).status_code
        == 404
    )


@pytest.mark.parametrize(
    "host",
    [
        "deep.nested.{d}",  # more than one label
        "UPPER.{d}",  # pattern is lowercase-only
        "bad_underscore.{d}",
        "-leading.{d}",
        "sub-shop.evil.com",  # right label, wrong domain
        "sub-shop.{d}.evil.com",  # our domain as a prefix of theirs
        "{d}",
        "",
    ],
)
def test_malformed_or_foreign_hosts_are_404(make_store, host):
    _live_store(make_store, "sub-shop")
    assert (
        client.get("/", headers={"host": host.format(d=config.DOMAIN)}).status_code
        == 404
    )


def test_subdomain_of_a_non_live_store_is_404(make_store):
    """A blocked or pending store must not become reachable via its subdomain."""
    acct = Account.create()
    _seed_store(acct, "sub-dead", make_store, content=_content())
    with SessionLocal() as s:
        s.scalar(select(Store).where(Store.slug == "sub-dead")).status = "blocked"
        s.commit()
    assert (
        client.get("/", headers={"host": f"sub-dead.{config.DOMAIN}"}).status_code
        == 404
    )


def test_a_verified_custom_domain_still_wins(make_store, monkeypatch):
    """Custom domains are checked first; adding subdomains must not shadow them."""
    acct = Account.create()
    _seed_store(acct, "s1", make_store, content=_content())
    _claim(acct, "s1")
    tok = _merchant_token(acct)
    with SessionLocal() as s:
        token = s.scalar(select(Store).where(Store.slug == "s1")).custom_domain_token
    monkeypatch.setattr(
        domains, "_lookup_txt", lambda name: [domains.txt_record_value(token)]
    )
    client.post("/api/merchant/stores/s1/custom-domain/verify", headers=_auth(tok))
    assert client.get("/", headers={"host": DOMAIN}).status_code == 200
    # and the same store is reachable on its platform subdomain too
    assert client.get("/", headers={"host": f"s1.{config.DOMAIN}"}).status_code == 200
