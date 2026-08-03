"""Real fulfilment at create time: caller-supplied delivery text, a Deliverable
attached at birth, screening of both, and the honest default that replaced a
fabricated download link. No network — the LLM and Warden are stubbed."""

import json

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.main as main
from app.config import WARDEN_SCREEN_URL
from app.db import SessionLocal
from app.models import Deliverable, Store

client = TestClient(main.app)


def _fake_llm(monkeypatch, raw):
    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": json.dumps(raw)}]}

    monkeypatch.setattr("app.engine.requests.post", lambda *a, **k: R())


def _allow():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )


def _content(name="Kit"):
    return {
        "store_name": name,
        "products": [{"name": "Guide", "price_usdt": 5, "cta_text": "Buy"}],
    }


# ------------------------------------------------- the honest default (phase 2)
@respx.mock
def test_default_delivery_no_longer_fabricates_a_download_link(tmp_path, monkeypatch):
    # The old default handed every buyer https://tilla.gudman.xyz/files/<slug>,
    # a path that is not a route and returns 404.
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm(monkeypatch, _content("Honest"))
    _allow()
    r = engine.create_store("i sell a guide")
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == r["slug"]))
    assert "/files/" not in store.delivery
    assert "demo delivery link" not in store.delivery
    # it must say what is true, and name the endpoint that fixes it
    assert "deliverable" in store.delivery


def test_default_delivery_does_not_assume_the_goods_are_digital():
    # The first honest wording said "no downloadable file". Right for a report,
    # wrong for the watch, jersey and candle stores that also carried it — a
    # merchant who ships a physical object had not failed to configure anything.
    import app.engine as engine

    text = engine.default_delivery_text("timber-time", "Classic Walnut Watch")
    assert "no downloadable file" not in text
    assert "physical item or a service" in text
    # and it must still tell a digital seller what to do
    assert "licence key" in text and "/api/stores/timber-time/deliverable" in text


def test_superseded_delivery_texts_are_kept_verbatim_for_the_repair():
    # The repair recognises text WE wrote by exact render. Editing an entry here
    # (rather than appending) strands every store still holding that exact string:
    # it stops matching, so it is never brought forward again.
    import app.engine as engine

    old = engine.superseded_delivery_texts("dossier", "Token Due Diligence Report")
    assert len(old) >= 1
    v1 = old[0]
    assert v1.startswith("Payment received — thank you. Token Due Diligence Report")
    assert "no downloadable file attached yet" in v1
    assert "/api/stores/dossier/deliverable" in v1
    # a superseded text must never equal the current one, or the repair would
    # treat an up-to-date store as stale and rewrite it on every run
    assert (
        engine.default_delivery_text("dossier", "Token Due Diligence Report") not in old
    )


# ------------------------------------------- caller-supplied delivery (phase 1)
@respx.mock
def test_caller_supplied_delivery_is_persisted(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm(monkeypatch, _content("Real"))
    _allow()
    r = engine.create_store("guides", delivery="Your guide: https://example.com/g.pdf")
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == r["slug"]))
    assert store.delivery == "Your guide: https://example.com/g.pdf"


@respx.mock
def test_supplied_delivery_and_payload_are_screened(tmp_path, monkeypatch):
    # Buyer-facing text the merchant supplies must ride the SAME single Warden
    # call as the generated copy — not go unscreened, and not cost a second hire.
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm(monkeypatch, _content("Screened"))
    seen = {}

    def _capture(text):
        seen["text"] = text
        from app.screening import ScanOutcome

        return ScanOutcome(status="allow", receipt=None)

    monkeypatch.setattr(engine.screening, "screen", _capture)
    engine.create_store(
        "guides",
        delivery="DELIVERY-MARKER",
        deliverable={"kind": "text", "payload": "PAYLOAD-MARKER"},
    )
    assert "DELIVERY-MARKER" in seen["text"]
    assert "PAYLOAD-MARKER" in seen["text"]


@respx.mock
def test_deliverable_is_attached_at_birth(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm(monkeypatch, _content("Goods"))
    _allow()
    r = engine.create_store(
        "guides", deliverable={"kind": "text", "payload": "the real goods"}
    )
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == r["slug"]))
        rows = s.scalars(
            select(Deliverable).where(Deliverable.store_id == store.id)
        ).all()
    assert len(rows) == 1
    d = rows[0]
    assert (d.kind, d.payload, d.version, d.active) == (
        "text",
        "the real goods",
        1,
        True,
    )
    assert d.product_id is None  # store-level default: covers every product


@respx.mock
def test_no_deliverable_means_no_row(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm(monkeypatch, _content("Bare"))
    _allow()
    r = engine.create_store("guides")
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == r["slug"]))
        assert (
            s.scalars(select(Deliverable).where(Deliverable.store_id == store.id)).all()
            == []
        )


# ------------------------------------------------------------- the API surface
@respx.mock
def test_bodyless_create_is_a_hidden_nonpayable_sandbox(tmp_path, monkeypatch):
    """The listing review's bodyless POST must never publish Tilla's demo wallet.

    The response remains useful to an unattended reviewer, but a checkout attempt
    creates no order and exposes no payable store until a merchant supplies their
    own receive address on a new create call.
    """
    import app.engine as engine
    from app.models import Order

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    monkeypatch.setenv("TILLA_LLM_KEY", "k")
    _fake_llm(monkeypatch, _content("Review sample"))
    _allow()

    created = client.post("/create-store")

    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "sandbox"
    assert body["visibility"] == "hidden"
    assert body["payable"] is False
    assert "receive_address" in body["note"]
    with SessionLocal() as session:
        store = session.scalar(select(Store).where(Store.slug == body["slug"]))
        assert store is not None
        assert store.status == "sandbox"
        assert store.visibility == "hidden"
        assert store.pay_to == engine.DEFAULT_ADDR

    checkout = client.post(f"/api/checkout/{body['slug']}")

    # 409 with the reason named, not a bare 404: this store's page is publicly
    # served, so "store not found" read as a glitch worth retrying — and no retry
    # can ever clear it. Refusing is still absolute; only the wording changed.
    assert checkout.status_code == 409
    assert "sample store" in checkout.json()["detail"]
    with SessionLocal() as session:
        assert session.scalar(select(Order).where(Order.store_id == store.id)) is None

    described = client.post("/create-store", json={"description": "I sell guides"})

    assert described.status_code == 200, described.text
    assert described.json()["status"] == "sandbox"
    assert described.json()["payable"] is False
    assert "receive_address" in described.json()["note"]


def test_browser_checkout_requires_and_persists_declared_buyer_inputs(make_store):
    from app.models import Order, Product

    store_id = make_store(slug="briefed", pay_to="0x" + "b" * 40)
    with SessionLocal() as session:
        product = session.scalar(select(Product).where(Product.store_id == store_id))
        product.buyer_inputs = [
            {"name": "token", "label": "Token address", "required": True},
            {"name": "context", "label": "Extra context", "required": False},
        ]
        session.commit()

    missing = client.post("/api/checkout/briefed")

    assert missing.status_code == 422
    with SessionLocal() as session:
        assert session.scalar(select(Order).where(Order.store_id == store_id)) is None

    created = client.post(
        "/api/checkout/briefed",
        json={
            "inputs": {
                "token": "0xabc",
                "context": "focus on governance",
                "ignored": "not merchant declared",
            }
        },
    )

    assert created.status_code == 200, created.text
    with SessionLocal() as session:
        order = session.get(Order, created.json()["id"])
        assert order.buyer_inputs == {
            "token": "0xabc",
            "context": "focus on governance",
        }


def test_create_store_schema_advertises_fulfilment_fields():
    # The 402 body and the agent card both derive from this model, so publishing
    # the fields is what makes them discoverable to a paying agent.
    props = main.CreateStoreBody.model_json_schema()["properties"]
    assert "delivery" in props
    assert "deliverable" in props


def test_text_deliverable_requires_a_payload():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        main.DeliverableBody(kind="text", payload="   ")
    # a licence needs no payload — the key is minted per order
    assert main.DeliverableBody(kind="license").kind == "license"


def test_file_kind_is_rejected_on_the_json_create_call():
    import pytest
    from pydantic import ValidationError

    # multipart cannot ride a JSON create; files stay on the upload endpoint
    with pytest.raises(ValidationError):
        main.DeliverableBody(kind="file", payload="x")


# ------------------------------------------------- end to end: what a buyer gets
def _deliver_order(store_id, oid="ff-ord"):
    from app import checkout
    from app.models import Delivery, Entitlement, Order

    with SessionLocal() as s:
        order = Order(
            id=oid,
            store_id=store_id,
            pay_to="0x" + "a" * 40,
            amount_micro=5_000_000,
            expected_micro=5_000_000,
            status="confirmed",
            from_addr="0x" + "2" * 40,
        )
        s.add(order)
        s.commit()
        checkout.deliver(s, order)
        s.commit()
        row = s.scalar(select(Delivery).where(Delivery.order_id == oid))
        ent = s.scalar(select(Entitlement).where(Entitlement.order_id == oid))
        return row, ent


@respx.mock
def test_a_store_with_a_licence_deliverable_mints_a_real_key(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm(monkeypatch, _content("Licensed"))
    _allow()
    r = engine.create_store("software", deliverable={"kind": "license"})
    with SessionLocal() as s:
        sid = s.scalar(select(Store.id).where(Store.slug == r["slug"]))
    row, ent = _deliver_order(sid, "ff-lic")
    # a real entitlement, and the payload is a minted key -- not a message
    assert ent is not None and ent.license_key
    assert row.kind == "license"
    assert row.payload == ent.license_key


@respx.mock
def test_a_store_without_a_deliverable_still_sells(tmp_path, monkeypatch):
    # LOAD-BEARING: refusing to sell without a deliverable would break every
    # existing store, including the listed ones a marketplace reviewer buys from.
    # The sale must still settle -- only the message changes.
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm(monkeypatch, _content("Bareshop"))
    _allow()
    r = engine.create_store("guides")
    with SessionLocal() as s:
        sid = s.scalar(select(Store.id).where(Store.slug == r["slug"]))
    row, ent = _deliver_order(sid, "ff-bare")
    assert row is not None and row.kind == "text"
    assert ent is None  # nothing to be entitled to
    assert "/files/" not in row.payload  # and no fabricated link


@respx.mock
def test_unconfigured_response_names_the_create_time_field(tmp_path, monkeypatch):
    # A merchant reported never learning the create call can carry the goods
    # itself: naming only the follow-up endpoint left them to discover the field,
    # which is how a store ships empty twice.
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    monkeypatch.setenv("TILLA_LLM_KEY", "k")
    _fake_llm(monkeypatch, _content("Advertised"))
    _allow()
    r = client.post("/create-store", json={"description": "guides"})
    assert r.status_code == 200, r.text
    f = r.json()["fulfilment"]
    assert f["configured"] is False
    assert "deliverable" in f["next_time"]
    assert "/deliverable" in f["note"]  # the follow-up endpoint is still named


@respx.mock
def test_configured_response_says_so(tmp_path, monkeypatch):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    monkeypatch.setenv("TILLA_LLM_KEY", "k")
    _fake_llm(monkeypatch, _content("Configured"))
    _allow()
    r = client.post(
        "/create-store",
        json={"description": "guides", "deliverable": {"kind": "license"}},
    )
    assert r.status_code == 200, r.text
    f = r.json()["fulfilment"]
    assert f["configured"] is True and f["kind"] == "license"
    assert "next_time" not in f  # nothing to advise; they already did it


# ------------------------------------------ reading back what is attached (GET)
def _manage_key(monkeypatch, tmp_path, slug, **kw):
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm(monkeypatch, _content(slug))
    _allow()
    r = engine.create_store("guides", **kw)
    return r["slug"], r["manage_key"]


@respx.mock
def test_get_deliverable_reads_back_a_text_payload(tmp_path, monkeypatch):
    # A merchant who attached one had no way to confirm it: the POST response was
    # the only evidence, and checking meant buying from your own store.
    slug, key = _manage_key(
        monkeypatch,
        tmp_path,
        "Readback",
        deliverable={"kind": "text", "payload": "the real handoff"},
    )
    r = client.get(
        f"/api/stores/{slug}/deliverable", headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["configured"] is True and b["kind"] == "text"
    assert b["payload"] == "the real handoff"


@respx.mock
def test_get_deliverable_reports_unconfigured(tmp_path, monkeypatch):
    slug, key = _manage_key(monkeypatch, tmp_path, "Empty")
    b = client.get(
        f"/api/stores/{slug}/deliverable", headers={"Authorization": f"Bearer {key}"}
    ).json()
    assert b["configured"] is False
    assert "POST to this same path" in b["note"]


@respx.mock
def test_get_deliverable_requires_the_manage_key(tmp_path, monkeypatch):
    slug, _key = _manage_key(
        monkeypatch,
        tmp_path,
        "Guarded",
        deliverable={"kind": "text", "payload": "secret goods"},
    )
    assert client.get(f"/api/stores/{slug}/deliverable").status_code in (401, 403)
    r = client.get(
        f"/api/stores/{slug}/deliverable", headers={"Authorization": "Bearer wrong"}
    )
    assert r.status_code in (401, 403)


@respx.mock
def test_get_deliverable_never_returns_file_bytes(tmp_path, monkeypatch):
    # Bytes stay behind a signed, entitlement-bound token even for the owner.
    from app.db import SessionLocal as SL
    from app.models import Deliverable as D, Store as S

    slug, key = _manage_key(monkeypatch, tmp_path, "Filed")
    with SL() as s:
        sid = s.scalar(select(S.id).where(S.slug == slug))
        s.add(
            D(
                store_id=sid,
                kind="file",
                file_name="a.pdf",
                file_size=12,
                file_sha256="0" * 64,
                version=1,
                active=True,
            )
        )
        s.commit()
    b = client.get(
        f"/api/stores/{slug}/deliverable", headers={"Authorization": f"Bearer {key}"}
    ).json()
    assert b["kind"] == "file" and b["file_name"] == "a.pdf"
    assert "payload" not in b


# ------------------------------------------- discoverability of the manage key
@respx.mock
def test_manage_index_lists_what_the_key_opens(tmp_path, monkeypatch):
    # A merchant probed 21 candidate routes and the API index before giving up:
    # the create response says "use your manage key" without naming a path, and
    # there is no /openapi.json. A capability that cannot be discovered is one the
    # holder does not have.
    slug, key = _manage_key(
        monkeypatch,
        tmp_path,
        "Indexed",
        deliverable={"kind": "text", "payload": "goods"},
    )
    r = client.get(
        f"/api/stores/{slug}/manage", headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 200, r.text
    b = r.json()
    paths = {e["path"] for e in b["manage_key_endpoints"]}
    assert f"/api/stores/{slug}/deliverable" in paths
    assert "/add-product" in paths and "/upgrade-store" in paths
    assert b["deliverable"]["kind"] == "text"
    assert b["products"] and "price_usdt" in b["products"][0]


@respx.mock
def test_manage_index_answers_the_price_question(tmp_path, monkeypatch):
    # The blocker they hit: no price field on create-store, and no way to learn
    # that repricing is a MERCHANT action rather than a manage-key one.
    slug, key = _manage_key(monkeypatch, tmp_path, "Priced")
    b = client.get(
        f"/api/stores/{slug}/manage", headers={"Authorization": f"Bearer {key}"}
    ).json()
    cp = b["changing_a_price"]
    assert "no price field" in cp["note"]
    assert cp["recommended"]["method"] == "PATCH"
    assert "/api/merchant/stores/" in cp["recommended"]["path"]
    assert cp["recommended"]["cost"] == "free"
    assert any("add-product" in a for a in cp["alternatives"])
    # The example price must READ as a placeholder, like <product_id> beside it.
    # It was a bare 0.01 — which happened to be the reporting merchant's own
    # price — and they flagged that an agent parsing this field could take it for
    # the required value rather than an illustration.
    body_price = cp["recommended"]["body"]["price_usdt"]
    assert isinstance(body_price, str), "a literal number reads as the value to send"
    assert body_price.startswith("<") and body_price.endswith(">")
    # And the fee for an alternative must not read as a price to set.
    assert any("the call costs 0.01 USDT" in a for a in cp["alternatives"])


@respx.mock
def test_manage_index_names_the_other_merchant_actions(tmp_path, monkeypatch):
    # Repricing used to be the ONLY merchant-session action this index named, so
    # renaming a store, rewording its headline and replacing a product photo all
    # read as impossible — the same "capability we never named" failure the index
    # was built to end, with a 0.03 USDT regeneration as the apparent workaround.
    slug, key = _manage_key(monkeypatch, tmp_path, "Renamed")
    b = client.get(
        f"/api/stores/{slug}/manage", headers={"Authorization": f"Bearer {key}"}
    ).json()
    other = b["other_merchant_actions"]
    assert "merchant sign-in" in other["note"]
    paths = {e["path"] for e in other["endpoints"]}
    assert f"/api/merchant/stores/{slug}/description" in paths
    assert f"/api/merchant/stores/{slug}/products/<product_id>/image" in paths
    assert f"/api/merchant/stores/{slug}/visibility" in paths
    copy = next(e for e in other["endpoints"] if e["path"].endswith("/description"))
    # the headline is the field a merchant could not reach at all before
    assert "hero_headline" in copy["does"]


@respx.mock
def test_manage_index_names_the_consequence_of_having_no_deliverable(
    tmp_path, monkeypatch
):
    # `deliverable: null` is a fact the holder still has to interpret. 36 of 38 live
    # stores were in this state and their merchants had no way to learn it once the
    # create response had scrolled past.
    slug, key = _manage_key(monkeypatch, tmp_path, "Bare")
    b = client.get(
        f"/api/stores/{slug}/manage", headers={"Authorization": f"Bearer {key}"}
    ).json()
    assert b["deliverable"] is None
    f = b["fulfilment"]
    assert f["mode"] == "merchant"
    assert "you fulfil each sale yourself" in f["means"]
    # and it must name the endpoint that changes the answer
    assert f"/api/stores/{slug}/deliverable" in f["to_automate"]
    # products carry their own input demands, [] when they ask nothing
    assert b["products"] and b["products"][0]["buyer_inputs"] == []


@respx.mock
def test_manage_index_reports_product_specific_mixed_fulfilment(tmp_path, monkeypatch):
    """A product-specific good must not make manual products look automatic."""
    from app.models import Product

    slug, key = _manage_key(monkeypatch, tmp_path, "Mixed")
    with SessionLocal() as session:
        store = session.scalar(select(Store).where(Store.slug == slug))
        primary = session.scalar(select(Product).where(Product.store_id == store.id))
        secondary = Product(
            store_id=store.id,
            name="Manual consultation",
            price_micro=8_000_000,
            active=True,
        )
        session.add(secondary)
        session.flush()
        session.add(
            Deliverable(
                store_id=store.id,
                product_id=primary.id,
                kind="text",
                payload="automatic primary good",
                version=1,
                active=True,
            )
        )
        session.commit()

    response = client.get(
        f"/api/stores/{slug}/manage", headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    by_product = {product["id"]: product for product in body["products"]}
    assert body["fulfilment"]["mode"] == "mixed"
    assert by_product[primary.id]["fulfilment"] == "automatic"
    assert by_product[secondary.id]["fulfilment"] == "merchant"
    assert body["deliverable"] is None
    assert len(body["deliverables"]) == 1
    assert body["deliverables"][0]["product_id"] == primary.id
    assert body["deliverables"][0]["kind"] == "text"
    assert body["deliverables"][0]["version"] == 1


@respx.mock
def test_manage_index_requires_the_key(tmp_path, monkeypatch):
    slug, _key = _manage_key(monkeypatch, tmp_path, "Shut")
    assert client.get(f"/api/stores/{slug}/manage").status_code in (401, 403)
    assert client.get(
        f"/api/stores/{slug}/manage", headers={"Authorization": "Bearer nope"}
    ).status_code in (401, 403)


# --------------------------------------------- merchant contact (who to tell)
@respx.mock
def test_create_store_captures_a_notify_agent_id(tmp_path, monkeypatch):
    # Tilla captured no contact of any kind, so when 36 live stores were found
    # serving a 404 link, one of six external merchants could be reached.
    import app.engine as engine
    from app.models import Merchant

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm(monkeypatch, _content("Reachable"))
    _allow()
    addr = "0x" + "c1" * 20
    engine.create_store("i sell guides", addr, notify_agent_id=7012)
    with SessionLocal() as s:
        m = s.scalar(select(Merchant).where(Merchant.wallet_address == addr.lower()))
        assert m.contact_agent_id == 7012


@respx.mock
def test_a_later_create_never_clears_an_existing_contact(tmp_path, monkeypatch):
    # First write wins: a merchant with several stores sets this once, and a later
    # create that omits it must not silently drop the only channel we have.
    import app.engine as engine
    from app.models import Merchant

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _allow()
    addr = "0x" + "c2" * 20
    _fake_llm(monkeypatch, _content("First"))
    engine.create_store("first shop", addr, notify_agent_id=6961)
    _fake_llm(monkeypatch, _content("Second"))
    engine.create_store("second shop", addr)
    with SessionLocal() as s:
        m = s.scalar(select(Merchant).where(Merchant.wallet_address == addr.lower()))
        assert m.contact_agent_id == 6961


@respx.mock
def test_a_malformed_notify_agent_id_never_fails_the_paid_create(tmp_path, monkeypatch):
    # Refusing a PAID create over a notification preference trades a real sale for
    # a nicety. The create path drops it; the explicit endpoint 422s instead.
    import app.main as main_mod

    body = main_mod.CreateStoreBody(description="x", notify_agent_id="not-an-id")
    assert main_mod.b2b.parse_agent_id(body.notify_agent_id) is None


# ------------------------------------------------ detaching: the one-way door
# A merchant reported that attaching goods could never be undone: DELETE 405, and
# POST with active:false / an empty payload both 422 on the create schema. That
# left the store advertising fulfilment "automatic" — a claim a buying agent acts
# on without a human reading anything — with no way to withdraw it.
@respx.mock
def test_detach_flips_the_store_back_to_merchant_fulfilment(tmp_path, monkeypatch):
    slug, key = _manage_key(
        monkeypatch, tmp_path, "Detach", deliverable={"kind": "text", "payload": "G"}
    )
    auth = {"Authorization": f"Bearer {key}"}
    assert (
        client.get(f"/s/{slug}/feed.json").json()["store"]["fulfilment"] == "automatic"
    )

    r = client.delete(f"/api/stores/{slug}/deliverable", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is False and r.json()["detached"] == 1
    # the machine-readable claim must actually withdraw, in the surface an agent reads
    assert (
        client.get(f"/s/{slug}/feed.json").json()["store"]["fulfilment"] == "merchant"
    )
    assert (
        client.get(f"/api/stores/{slug}/deliverable", headers=auth).json()["configured"]
        is False
    )


@respx.mock
def test_detach_is_idempotent_and_key_gated(tmp_path, monkeypatch):
    slug, key = _manage_key(
        monkeypatch, tmp_path, "Twice", deliverable={"kind": "text", "payload": "G"}
    )
    auth = {"Authorization": f"Bearer {key}"}
    assert (
        client.delete(f"/api/stores/{slug}/deliverable", headers=auth).status_code
        == 200
    )
    # a retry after a timeout must be a no-op, not a 404
    second = client.delete(f"/api/stores/{slug}/deliverable", headers=auth)
    assert second.status_code == 200 and second.json()["detached"] == 0
    # and it is owner-only
    assert client.delete(f"/api/stores/{slug}/deliverable").status_code in (401, 403)


@respx.mock
def test_detach_does_not_revoke_what_a_buyer_already_received(tmp_path, monkeypatch):
    # DEACTIVATES, never deletes. Entitlement.deliverable_id is an FK and the claim
    # paths resolve the row by id, so a delivered buyer keeps their goods.
    from app.models import Entitlement

    slug, key = _manage_key(
        monkeypatch, tmp_path, "Kept", deliverable={"kind": "license", "payload": None}
    )
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        _deliver_order(store.id, oid="detach-keep")
        ent = s.scalar(select(Entitlement).where(Entitlement.order_id == "detach-keep"))
        assert ent is not None and ent.license_key
        key_before, deliverable_id = ent.license_key, ent.deliverable_id

    client.delete(
        f"/api/stores/{slug}/deliverable", headers={"Authorization": f"Bearer {key}"}
    )

    with SessionLocal() as s:
        row = s.get(Deliverable, deliverable_id)
        assert row is not None, "the row must survive — an FK points at it"
        assert row.active is False
        ent = s.scalar(select(Entitlement).where(Entitlement.order_id == "detach-keep"))
        assert ent.license_key == key_before and ent.revoked_at is None


@respx.mock
def test_detach_refuses_while_an_order_is_paid_but_undelivered(tmp_path, monkeypatch):
    # checkout.deliver resolves the deliverable at DELIVERY time, so detaching in
    # the gap between payment landing and delivery running would hand a buyer the
    # placeholder text after their money was irrevocable.
    from app.models import Order

    slug, key = _manage_key(
        monkeypatch, tmp_path, "Race", deliverable={"kind": "text", "payload": "G"}
    )
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        s.add(
            Order(
                id="race-ord",
                store_id=store.id,
                pay_to=store.pay_to,
                amount_micro=1_000_000,
                expected_micro=1_000_000,
                status="confirmed",  # funds in, delivery not yet run
            )
        )
        s.commit()
    r = client.delete(
        f"/api/stores/{slug}/deliverable", headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 409
    assert "not been delivered yet" in r.json()["detail"]
    # and the goods are still attached, so the pending delivery still mints them
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        assert (
            s.scalar(
                select(Deliverable).where(
                    Deliverable.store_id == store.id, Deliverable.active.is_(True)
                )
            )
            is not None
        )


@respx.mock
def test_detach_refuses_while_a_metered_channel_holds_prepaid_balance(
    tmp_path, monkeypatch
):
    # MPP has NO Order rows, so the order guard above is structurally blind to it:
    # a buyer funds a channel and draws units down later, each redemption resolving
    # the deliverable at that moment.
    from app.models import MppChannel, Product

    slug, key = _manage_key(
        monkeypatch, tmp_path, "Metered", deliverable={"kind": "text", "payload": "G"}
    )
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        product = s.scalar(select(Product).where(Product.store_id == store.id))
        s.add(
            MppChannel(
                store_id=store.id,
                product_id=product.id,
                channel_id="ch-prepaid",
                pay_to=store.pay_to,
                deposit_micro=2_000_000,
                spent_micro=100_000,  # 1.9 USDT still prepaid and unspent
                unit_price_micro=100_000,
                status="open",
            )
        )
        s.commit()
    r = client.delete(
        f"/api/stores/{slug}/deliverable", headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 409
    assert "prepaid" in r.json()["detail"]

    # fully drawn down => nothing left to strand => detach allowed
    with SessionLocal() as s:
        ch = s.scalar(select(MppChannel).where(MppChannel.channel_id == "ch-prepaid"))
        ch.spent_micro = ch.deposit_micro
        s.commit()
    assert (
        client.delete(
            f"/api/stores/{slug}/deliverable",
            headers={"Authorization": f"Bearer {key}"},
        ).status_code
        == 200
    )


@respx.mock
def test_detach_shows_what_buyers_will_now_get_and_warns_a_listed_store(
    tmp_path, monkeypatch
):
    # The message buyers now receive is echoed so the merchant SEES it rather than
    # learning it from a complaint; and a marketplace-LISTED store is warned that
    # its on-chain listing text ("delivered as soon as the payment clears") is now
    # an overclaim — listing edits are user-gated, so a silent detach would leave
    # a stale claim on the most public surface there is.
    slug, key = _manage_key(
        monkeypatch, tmp_path, "Listed", deliverable={"kind": "text", "payload": "G"}
    )
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        store.marketplace_status = "listed"
        s.commit()
    r = client.delete(
        f"/api/stores/{slug}/deliverable", headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["delivery_message"]  # never empty — falls back to the default
    assert any("LISTED" in w for w in body["warnings"])


@respx.mock
def test_detach_warns_when_subscription_products_keep_billing(tmp_path, monkeypatch):
    # Renewals settle at the facilitator BEFORE our delivery hook runs and there is
    # no local subscriber table to wait on — so this cannot be a 409 like the
    # order/channel guards. It must be said, not hidden.
    from app.models import Product

    slug, key = _manage_key(
        monkeypatch, tmp_path, "Subbed", deliverable={"kind": "text", "payload": "G"}
    )
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == slug))
        product = s.scalar(select(Product).where(Product.store_id == store.id))
        product.pricing_model = "subscription"
        s.commit()
    r = client.delete(
        f"/api/stores/{slug}/deliverable", headers={"Authorization": f"Bearer {key}"}
    )
    assert r.status_code == 200
    assert any("subscription" in w for w in r.json()["warnings"])


@respx.mock
def test_manage_index_advertises_the_detach(tmp_path, monkeypatch):
    # The index exists because a merchant probed 21 routes for a capability we
    # never named. The detach was built after that lesson; shipping it unlisted
    # would repeat the exact failure it answers.
    slug, key = _manage_key(monkeypatch, tmp_path, "Advertised")
    b = client.get(
        f"/api/stores/{slug}/manage", headers={"Authorization": f"Bearer {key}"}
    ).json()
    entry = next(
        e
        for e in b["manage_key_endpoints"]
        if e["path"] == f"/api/stores/{slug}/deliverable"
    )
    assert entry["method"] == "GET/POST/DELETE"
    assert "detach" in entry["does"]


# ------------------------------------------------ merchant taste: brand colour
@respx.mock
def test_stated_brand_color_beats_the_models_guess(tmp_path, monkeypatch):
    # Same contract as prices: what the merchant states is used exactly, auto
    # fills the silence. Only the HUE is taken — harmony, mood and the contrast
    # floors stay derived, so a stated colour cannot render the store illegible.
    import app.engine as engine

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    _fake_llm(monkeypatch, _content("Tinted"))
    _allow()
    r = engine.create_store("candles", brand_color="#FF0000")
    with SessionLocal() as s:
        store = s.scalar(select(Store).where(Store.slug == r["slug"]))
    assert store.content["brand"]["hue"] == 0.0  # red
    assert "palette" in store.content  # ...and the derived system still resolved


def test_brand_color_validators_normalize_and_refuse():
    import pytest
    from pydantic import ValidationError

    from app.dashboard import SelfServeCreateBody

    assert main.CreateStoreBody(brand_color="1a7f5c").brand_color == "#1A7F5C"
    assert (
        SelfServeCreateBody(description="x", brand_color="#00D1B2").brand_color
        == "#00D1B2"
    )
    assert main.CreateStoreBody(brand_color="").brand_color is None
    with pytest.raises(ValidationError):
        main.CreateStoreBody(brand_color="reddish")
    with pytest.raises(ValidationError):
        SelfServeCreateBody(description="x", brand_color="#12345")
