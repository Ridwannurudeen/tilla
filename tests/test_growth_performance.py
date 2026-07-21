"""M17.2 performance-readback tests (no network, no funds, no on-chain).

Covers GET /api/stores/{slug}/growth/performance: exact pure-DB aggregates from
seeded fixtures (delivered orders/revenue by day, affiliate-attributed sales + top
referrers, waitlist growth, kit/draft history), the owner/IDOR gate, and the no-PII
guarantee (no buyer email or full referrer wallet ever appears in the response).
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app.main as main
from app.db import SessionLocal
from app.delivery import mint_manage_key
from app.models import (
    AffiliateAccrual,
    EmailSubscriber,
    GrowthDraft,
    Order,
    Store,
    log_event,
)

client = TestClient(main.app)

REFERRER = "0x" + "1" * 40
BUYER_EMAIL = "buyer-secret@example.com"


def _auth(key: str) -> dict:
    return {"Authorization": "Bearer " + key}


def _store_with_key(make_store, slug="perf1", status="live"):
    sid = make_store(slug=slug, status=status)
    key, key_hash = mint_manage_key()
    with SessionLocal() as s:
        st = s.get(Store, sid)
        st.manage_key_hash = key_hash
        s.commit()
        mid = st.merchant_id
    return sid, mid, key


def _order(session, sid, *, status, expected_micro, created_at, from_addr=None):
    oid = uuid.uuid4().hex[:16]
    session.add(
        Order(
            id=oid,
            store_id=sid,
            pay_to="0x" + "a" * 40,
            amount_micro=expected_micro,
            expected_micro=expected_micro,
            status=status,
            from_addr=from_addr,
            created_at=created_at,
        )
    )
    session.flush()
    return oid


def test_performance_shapes(make_store):
    sid, mid, key = _store_with_key(make_store, slug="perf-shape")
    now = datetime.now(timezone.utc)
    d1 = now - timedelta(days=1)
    d2 = now - timedelta(days=2)
    with SessionLocal() as s:
        # Two delivered orders on distinct days + one on the same day as d1.
        oid1 = _order(
            s, sid, status="delivered", expected_micro=9_000_000, created_at=d1
        )
        oid2 = _order(s, sid, status="paid", expected_micro=1_000_000, created_at=d1)
        _order(s, sid, status="delivered", expected_micro=5_000_000, created_at=d2)
        # Excluded: a pending order (not terminal) and an out-of-window delivered one.
        _order(s, sid, status="pending", expected_micro=7_000_000, created_at=now)
        _order(
            s,
            sid,
            status="delivered",
            expected_micro=8_000_000,
            created_at=now - timedelta(days=40),
        )
        # Affiliate: one accrued (counts) + one void (excluded).
        s.add(
            AffiliateAccrual(
                order_id=oid1,
                store_id=sid,
                merchant_id=mid,
                referrer_addr=REFERRER,
                basis_micro=9_000_000,
                rate_bps=200,
                accrued_micro=180_000,
                status="accrued",
                created_at=d1,
            )
        )
        s.add(
            AffiliateAccrual(
                order_id=oid2,
                store_id=sid,
                merchant_id=mid,
                referrer_addr=REFERRER,
                basis_micro=1_000_000,
                rate_bps=200,
                accrued_micro=20_000,
                status="void",
                created_at=d1,
            )
        )
        # Waitlist: one out-of-window (45d), one new; a checkout subscriber excluded.
        s.add(
            EmailSubscriber(
                store_id=sid,
                email=BUYER_EMAIL,
                source="waitlist",
                created_at=now - timedelta(days=45),
            )
        )
        s.add(
            EmailSubscriber(
                store_id=sid,
                email="fresh@example.com",
                source="waitlist",
                created_at=now,
            )
        )
        s.add(
            EmailSubscriber(
                store_id=sid,
                email="cust@example.com",
                source="checkout",
                created_at=now,
            )
        )
        # Growth activity: a kit event + a blocked draft.
        log_event(s, "growth", "growth.kit_generated", store_id=sid, data={})
        s.add(
            GrowthDraft(
                store_id=sid,
                channel="social",
                body="hidden",
                source="scheduled",
                status="blocked",
                content_sha256="x" * 64,
                screening_status="blocked",
            )
        )
        s.commit()

    r = client.get("/api/stores/perf-shape/growth/performance", headers=_auth(key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 30
    assert body["orders"]["total"] == 3
    assert body["orders"]["revenue_micro"] == 15_000_000
    by_day = body["orders"]["by_day"]
    assert len(by_day) == 2
    # Newest ordering is by date ascending; the d1 bucket holds two orders.
    day_totals = {row["date"]: (row["orders"], row["revenue_micro"]) for row in by_day}
    assert (2, 10_000_000) in day_totals.values()
    assert (1, 5_000_000) in day_totals.values()
    assert body["affiliates"]["attributed_sales"] == 1
    assert body["affiliates"]["accrued_micro"] == 180_000
    assert body["affiliates"]["top_referrers"][0]["accrued_micro"] == 180_000
    assert body["waitlist"] == {"total": 2, "new_in_window": 1}
    assert body["growth_activity"]["events"]["growth.kit_generated"] == 1
    assert body["growth_activity"]["drafts_by_status"]["blocked"] == 1
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_performance_days_clamped(make_store):
    _, _, key = _store_with_key(make_store, slug="perf-days")
    # days above the 90 cap is a 422 (Query le=90), days below 1 too.
    assert (
        client.get(
            "/api/stores/perf-days/growth/performance?days=91", headers=_auth(key)
        ).status_code
        == 422
    )
    r = client.get(
        "/api/stores/perf-days/growth/performance?days=7", headers=_auth(key)
    )
    assert r.status_code == 200
    assert r.json()["window_days"] == 7


def test_performance_idor_blocked(make_store):
    _store_with_key(make_store, slug="perf-a")
    _, _, key_b = _store_with_key(make_store, slug="perf-b")
    # Wrong key -> 401.
    assert (
        client.get(
            "/api/stores/perf-a/growth/performance", headers=_auth("nope")
        ).status_code
        == 401
    )
    # Store B's owner sees only store B's (empty) aggregates, never store A's.
    r = client.get("/api/stores/perf-b/growth/performance", headers=_auth(key_b))
    assert r.status_code == 200
    assert r.json()["orders"]["total"] == 0


def test_performance_no_pii(make_store):
    sid, mid, key = _store_with_key(make_store, slug="perf-pii")
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        oid = _order(
            s,
            sid,
            status="delivered",
            expected_micro=9_000_000,
            created_at=now,
            from_addr="0x" + "b" * 40,
        )
        s.add(
            AffiliateAccrual(
                order_id=oid,
                store_id=sid,
                merchant_id=mid,
                referrer_addr=REFERRER,
                basis_micro=9_000_000,
                rate_bps=200,
                accrued_micro=180_000,
                status="accrued",
                created_at=now,
            )
        )
        s.add(
            EmailSubscriber(
                store_id=sid, email=BUYER_EMAIL, source="waitlist", created_at=now
            )
        )
        s.commit()

    r = client.get("/api/stores/perf-pii/growth/performance", headers=_auth(key))
    assert r.status_code == 200
    # No buyer email and no FULL referrer/buyer wallet ever appear in the response.
    assert BUYER_EMAIL not in r.text
    assert REFERRER not in r.text
    assert ("0x" + "b" * 40) not in r.text
    # The referrer is present only in truncated display form.
    assert r.json()["affiliates"]["top_referrers"][0]["referrer"] == "0x1111…1111"
