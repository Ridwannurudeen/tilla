"""M15.1 provider registry tests.

The golden gate for the milestone: the registry + plugins table is a REFACTOR,
so the 402 challenge and the delivery payload the built-ins produce are proven
byte-identical to the direct (pre-refactor) code path. Plus the review-state
invariants: built-ins seeded active, an external row never resolves while
pending_review, registry writes are operator-only, and manifest screening is
fail-closed.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import delivery, providers, screening
from app.db import SessionLocal
from app.models import Deliverable, Entitlement, Order, Plugin, Store
from app.payment import build_store_payment_options, load_payment_rail

client = TestClient(main.app)
SENTINEL = "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"


@pytest.fixture
def seeded():
    """Seed the built-in plugin rows the way the 0011 migration does in prod."""
    with SessionLocal() as s:
        providers.seed_builtins(s)
        s.commit()


def _add_order(store_id, status="delivered"):
    with SessionLocal() as s:
        oid = f"o-{store_id}-{status}"
        s.add(
            Order(
                id=oid,
                store_id=store_id,
                pay_to="0x" + "a" * 40,
                amount_micro=1_000_000,
                expected_micro=1_000_000,
                status=status,
            )
        )
        s.commit()
        return oid


def _serialize_accepts(options):
    """The static, byte-stable fields of an accepts list — the dynamic per-request
    pay_to/price resolvers are coroutine closures, so they serialize as '<hook>'."""
    out = []
    for opt in options:
        out.append(
            (
                opt.scheme,
                opt.network,
                opt.max_timeout_seconds,
                "<hook>" if callable(opt.pay_to) else opt.pay_to,
                "<hook>" if callable(opt.price) else getattr(opt.price, "amount", None),
            )
        )
    return out


# --------------------------------------------------------- built-ins registered
def test_builtin_providers_registered(seeded):
    assert set(providers.DELIVERY_PROVIDERS) == {"file", "text", "license"}
    assert set(providers.PAYMENT_RAIL_PROVIDERS) == {"exact"}
    assert set(providers.THEME_PROVIDERS) == {"original", "bold", "editorial"}
    # every theme built-in satisfies the 15-token contract (template on disk)
    for theme in providers.THEME_PROVIDERS.values():
        assert theme.contract_ok()
    assert len(providers.THEME_TOKENS) == 15
    # the seeded rows: all nine built-ins, active, source builtin
    with SessionLocal() as s:
        rows = providers.list_plugins(s)
        assert len(rows) == 9
        assert all(r.source == "builtin" and r.status == "active" for r in rows)
        assert providers.active_names(s, "theme") == {"original", "bold", "editorial"}
        assert providers.active_names(s, "delivery") == {"file", "text", "license"}
        assert providers.active_names(s, "payment_rail") == {"exact", "mpp", "period"}


def test_seed_builtins_idempotent(seeded):
    with SessionLocal() as s:
        assert providers.seed_builtins(s) == 0  # already seeded → no re-insert
        s.commit()
        assert len(providers.list_plugins(s)) == 9


# --------------------------------------------------------- 402 byte-identical
def test_402_challenge_byte_identical(make_store, seeded):
    make_store(slug="prov-402", pay_to="0x" + "Ab" * 20, price_micro=2_500_000)
    # (a) the live endpoint 402 challenge is unchanged (status + body bytes).
    r = client.post("/s/prov-402/buy")
    assert r.status_code == 402
    assert r.content == b'{"detail":"payment required"}'
    # (b) the accepts the exact-rail provider builds are byte-identical to the
    # direct build_store_payment_options output — the refactor is a pure pass-through.
    rail = load_payment_rail({"PAY_TO_ADDRESS": SENTINEL})
    provider = providers.PAYMENT_RAIL_PROVIDERS["exact"]
    direct = _serialize_accepts(build_store_payment_options(rail))
    via_provider = _serialize_accepts(provider.build_accepts(rail))
    assert via_provider == direct
    # golden: exact scheme, X Layer, 300s timeout, dynamic pay_to + price hooks.
    assert direct == [("exact", "eip155:196", 300, "<hook>", "<hook>")]
    # a built-in rail may only veto (>=400), never originate: exact never vetoes.
    assert provider.pre_settle_gate() is None


# ------------------------------------------------ delivery via provider == direct
def test_delivery_via_provider_matches_direct(make_store, seeded):
    sid = make_store(slug="prov-del", delivery="secret-download-link")
    oid = _add_order(sid)
    with SessionLocal() as s:
        order = s.get(Order, oid)
        text_p = providers.DELIVERY_PROVIDERS["text"]
        file_p = providers.DELIVERY_PROVIDERS["file"]
        lic_p = providers.DELIVERY_PROVIDERS["license"]
        # text: the provider reproduces exactly checkout.deliver's text payload
        # (the legacy store.delivery string, since there is no text deliverable).
        store = s.get(Store, sid)
        assert text_p.mint(s, order) == store.delivery == "secret-download-link"
        # file: the ready-message payload, byte-identical to the direct constant.
        assert file_p.mint(s, order) == delivery.FILE_READY_MESSAGE
        # license: a valid key in the direct mint_license_key format.
        key = lic_p.mint(s, order)
        assert key.startswith("TILLA-") and len(key.split("-")) == 5


def test_provider_revoke_matches_direct(make_store, seeded):
    sid = make_store(slug="prov-rev")
    oid = _add_order(sid)
    with SessionLocal() as s:
        d = Deliverable(store_id=sid, kind="text", payload="x", active=True)
        s.add(d)
        s.flush()
        s.add(Entitlement(order_id=oid, deliverable_id=d.id))
        s.commit()
    with SessionLocal() as s:
        order = s.get(Order, oid)
        provider = providers.DELIVERY_PROVIDERS["text"]
        # first revoke succeeds and delegates to delivery.revoke_entitlement; a
        # second is idempotent-False — identical semantics to the direct call.
        assert provider.revoke(s, order) is True
        assert provider.revoke(s, order) is False
        s.commit()


# --------------------------------------------------- pending-until-approved
def test_plugin_pending_until_approved():
    """An external-source theme row never resolves (never appears active) while
    pending_review; only an explicit operator approval flips it."""
    with SessionLocal() as s:
        providers.seed_builtins(s)
        row = Plugin(
            kind="theme",
            name="midnight",
            version="1.0.0",
            source="external",
            artifact_sha256="a" * 64,
            manifest={"template": "midnight.html"},
            status="pending_review",
        )
        s.add(row)
        s.commit()
        pid = row.id
        # not resolvable while pending
        assert "midnight" not in providers.active_names(s, "theme")
        assert providers.is_active(s, "theme", "midnight") is False
        # operator approval flips it active → now resolvable
        providers.set_status(s, pid, "active", operator=True)
        s.commit()
        assert "midnight" in providers.active_names(s, "theme")


# --------------------------------------------------- operator-only writes (IDOR)
def test_registry_writes_operator_only():
    with SessionLocal() as s:
        providers.seed_builtins(s)
        s.commit()
        with pytest.raises(providers.OperatorRequired):
            providers.register_external(
                s,
                kind="theme",
                name="x",
                version="1",
                artifact_sha256="b" * 64,
                manifest={},
                manifest_text="clean theme",
                operator=False,
            )
        with pytest.raises(providers.OperatorRequired):
            providers.set_status(s, 1, "disabled", operator=False)


def test_external_non_theme_kind_forbidden(monkeypatch):
    monkeypatch.setattr(screening, "screen", lambda *_a, **_k: None)  # never reached
    with SessionLocal() as s:
        with pytest.raises(providers.SandboxBoundaryError):
            providers.register_external(
                s,
                kind="delivery",
                name="evil",
                version="1",
                artifact_sha256="c" * 64,
                manifest={},
                manifest_text="x",
                operator=True,
            )


def test_register_external_screens_fail_closed(monkeypatch):
    # BLOCK propagates (fail-closed) — no row is written.
    def _block(_payload, attempts=2):
        raise screening.ScreeningBlocked({"verdict": "BLOCK", "risk_level": "high"})

    monkeypatch.setattr(screening, "screen", _block)
    with SessionLocal() as s:
        with pytest.raises(screening.ScreeningBlocked):
            providers.register_external(
                s,
                kind="theme",
                name="bad",
                version="1",
                artifact_sha256="d" * 64,
                manifest={},
                manifest_text="hostile",
                operator=True,
            )
        s.rollback()
        assert s.query(Plugin).filter_by(name="bad").first() is None


def test_register_external_unavailable_fail_closed(monkeypatch):
    # an unavailable ('pending') screen holds the plugin out — nothing written.
    monkeypatch.setattr(
        screening, "screen", lambda *_a, **_k: screening.ScanOutcome("pending", None)
    )
    with SessionLocal() as s:
        with pytest.raises(screening.ScreeningUnavailable):
            providers.register_external(
                s,
                kind="theme",
                name="held",
                version="1",
                artifact_sha256="e" * 64,
                manifest={},
                manifest_text="ambiguous",
                operator=True,
            )
        s.rollback()
        assert s.query(Plugin).filter_by(name="held").first() is None


def test_register_external_allow_writes_pending(monkeypatch):
    monkeypatch.setattr(
        screening,
        "screen",
        lambda *_a, **_k: screening.ScanOutcome(
            "allow", screening.ScanReceipt(mode="demo", endpoint="x", verdict="ALLOW")
        ),
    )
    with SessionLocal() as s:
        row = providers.register_external(
            s,
            kind="theme",
            name="ok-theme",
            version="2.0.0",
            artifact_sha256="f" * 64,
            manifest={"template": "ok-theme.html"},
            manifest_text="wholesome theme manifest",
            operator=True,
        )
        s.commit()
        assert row.status == "pending_review"
        assert row.source == "external"
        assert providers.is_active(s, "theme", "ok-theme") is False
