"""Phase 4 link-in-bio: the PUBLIC /m/{address} merchant profile.

Server-rendered, autoescaped; lists ONLY a merchant's LIVE, PUBLIC stores (the
Phase 1.7 visibility filter, same as discovery + the sitemap). No network, no
funds — stores are seeded straight into the DB via the make_store fixture.
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app.db import SessionLocal
from app.models import Store

client = TestClient(main.app)

ADDR = "0x" + "b" * 40


def _set_content(slug: str, content: dict) -> None:
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        store.content = content
        s.commit()


def _set_visibility(slug: str, visibility: str) -> None:
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        store.visibility = visibility
        s.commit()


def test_profile_lists_only_live_public_stores(make_store):
    make_store(slug="pub-one", pay_to=ADDR)
    make_store(slug="pub-two", pay_to=ADDR)
    make_store(slug="hidden-one", pay_to=ADDR)
    make_store(slug="pending-one", pay_to=ADDR, status="pending_screening")
    _set_content("pub-one", {"store_name": "Coffee Bar", "hero_subcopy": "Fresh daily"})
    _set_visibility("hidden-one", "hidden")

    r = client.get(f"/m/{ADDR}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    # both live+public stores are present, with the store_name from content
    assert "/s/pub-one/" in body and "/s/pub-two/" in body
    assert "Coffee Bar" in body and "Fresh daily" in body
    # hidden and not-yet-live stores are excluded
    assert "hidden-one" not in body
    assert "pending-one" not in body


def test_profile_unknown_address_is_404():
    assert client.get("/m/0x" + "c" * 40).status_code == 404


def test_profile_with_no_public_stores_is_404(make_store):
    make_store(slug="all-hidden", pay_to=ADDR)
    _set_visibility("all-hidden", "hidden")
    # a merchant whose every store is hidden looks identical to an unknown address
    assert client.get(f"/m/{ADDR}").status_code == 404


def test_profile_malformed_address_is_404():
    assert client.get("/m/not-an-address").status_code == 404


def test_profile_escapes_store_name(make_store):
    make_store(slug="xss-store", pay_to=ADDR)
    _set_content("xss-store", {"store_name": "<script>alert(1)</script>"})
    r = client.get(f"/m/{ADDR}")
    assert r.status_code == 200
    # the store name is screened LLM copy — rendered autoescaped, never live markup
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text
