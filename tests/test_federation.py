"""M16.4 federation ingest tests — the mirror-of-mirrors up to the boundary.

A fixture peer (canned httpx responses via respx) stands in for a second Tilla
instance; the real federated network stays EXTERNALLY-BLOCKED on a non-self peer
existing. Covers: dormant-by-default (zero network), oversize + off-schema
rejection, labeled federated rows on ?include=federated, the migration
up/down/up, and the grep-assert that federation.py moves no funds.
"""

import json
import pathlib
import subprocess

import httpx
import respx

import app.main as main
from app import config, federation
from app.db import SessionLocal
from app.models import FederatedListing
from fastapi.testclient import TestClient

client = TestClient(main.app)

PEER = "https://peer.example.com"
REPO = pathlib.Path(__file__).resolve().parent.parent


def _valid_feed(slug: str = "peerstore") -> dict:
    return {
        "store": {
            "slug": slug,
            "name": "Peer Store",
            "description": "a store on the peer",
            "url": f"{PEER}/s/{slug}/",
        },
        "products": [
            {
                "id": "1",
                "title": "Peer Widget",
                "description": "sold by the peer",
                "link": f"{PEER}/s/{slug}/",
                "price": {"amount": "9.0", "currency": "USDT"},
                "availability": "in_stock",
                "pricing": {"model": "one_time", "params": {}},
                "x402": {
                    "endpoint": f"/s/{slug}/buy",
                    "network": "eip155:196",
                    "asset": "0x" + "e" * 40,
                },
            }
        ],
    }


def _discovery(slug: str = "peerstore") -> dict:
    return {
        "service": "tilla",
        "resources": [
            {
                "slug": slug,
                "name": "Peer Store",
                "description": "a store on the peer",
                "url": f"{PEER}/s/{slug}/",
                "price_min_micro": 9_000_000,
                "price_max_micro": 9_000_000,
                "currency": "USDT",
                "network": "eip155:196",
            }
        ],
    }


def _clear_listings():
    with SessionLocal() as s:
        s.query(FederatedListing).delete()
        s.commit()


# --------------------------------------------------------------- dormant default
def test_dormant_by_default_zero_network(monkeypatch):
    """No peers configured => ingest_once does ZERO network and returns 0."""
    monkeypatch.setattr(config, "FEDERATION_PEERS", [])
    with respx.mock:
        route = respx.route().mock(return_value=httpx.Response(200, json={}))
        assert federation.ingest_once() == 0
        assert route.call_count == 0  # not a single request left the process


# --------------------------------------------------------------- happy ingest
@respx.mock
def test_ingest_fixture_peer_caches_rows(monkeypatch):
    monkeypatch.setattr(config, "FEDERATION_PEERS", [PEER])
    monkeypatch.setattr(federation, "validate_peer", lambda p: p)
    _clear_listings()
    respx.get(f"{PEER}/discovery/resources").mock(
        return_value=httpx.Response(200, json=_discovery())
    )
    respx.get(f"{PEER}/s/peerstore/feed.json").mock(
        return_value=httpx.Response(200, json=_valid_feed())
    )
    assert federation.ingest_once() == 1
    with SessionLocal() as s:
        rows = s.query(FederatedListing).all()
        assert len(rows) == 1
        assert rows[0].origin == PEER
        assert rows[0].slug == "peerstore"
        assert rows[0].url == f"{PEER}/s/peerstore/"  # links OUT to the peer
        assert rows[0].feed_url == f"{PEER}/s/peerstore/feed.json"


@respx.mock
def test_ingest_is_idempotent_upsert(monkeypatch):
    monkeypatch.setattr(config, "FEDERATION_PEERS", [PEER])
    monkeypatch.setattr(federation, "validate_peer", lambda p: p)
    _clear_listings()
    respx.get(f"{PEER}/discovery/resources").mock(
        return_value=httpx.Response(200, json=_discovery())
    )
    respx.get(f"{PEER}/s/peerstore/feed.json").mock(
        return_value=httpx.Response(200, json=_valid_feed())
    )
    federation.ingest_once()
    federation.ingest_once()  # a second tick replaces, never duplicates
    with SessionLocal() as s:
        assert s.query(FederatedListing).count() == 1


# --------------------------------------------------------------- rejections
@respx.mock
def test_federation_rejects_oversize_and_offschema(monkeypatch):
    monkeypatch.setattr(config, "FEDERATION_PEERS", [PEER])
    monkeypatch.setattr(federation, "validate_peer", lambda p: p)
    monkeypatch.setattr(config, "FEDERATION_MAX_BYTES", 200)  # tiny cap
    _clear_listings()
    # OVERSIZE discovery index => the whole peer yields nothing
    big = _discovery()
    big["resources"][0]["description"] = "x" * 500  # blow past the 200-byte cap
    respx.get(f"{PEER}/discovery/resources").mock(
        return_value=httpx.Response(200, content=json.dumps(big).encode())
    )
    assert federation.ingest_once() == 0
    with SessionLocal() as s:
        assert s.query(FederatedListing).count() == 0

    # OFF-SCHEMA feed => that store is dropped (index ok, feed invalid)
    monkeypatch.setattr(config, "FEDERATION_MAX_BYTES", 262_144)
    respx.get(f"{PEER}/discovery/resources").mock(
        return_value=httpx.Response(200, json=_discovery())
    )
    bad_feed = {"store": {"slug": "peerstore"}}  # missing required products/fields
    respx.get(f"{PEER}/s/peerstore/feed.json").mock(
        return_value=httpx.Response(200, json=bad_feed)
    )
    assert federation.ingest_once() == 0
    with SessionLocal() as s:
        assert s.query(FederatedListing).count() == 0


@respx.mock
def test_peer_url_gate_rejects_internal(monkeypatch):
    """A non-https / internal peer origin is skipped by the SSRF gate (no fetch)."""
    monkeypatch.setattr(config, "FEDERATION_PEERS", ["http://169.254.169.254"])
    route = respx.route().mock(return_value=httpx.Response(200, json={}))
    assert federation.ingest_once() == 0
    assert route.call_count == 0


# --------------------------------------------------------------- labeled rows
@respx.mock
def test_federated_rows_labeled(monkeypatch):
    monkeypatch.setattr(config, "FEDERATION_PEERS", [PEER])
    monkeypatch.setattr(federation, "validate_peer", lambda p: p)
    _clear_listings()
    respx.get(f"{PEER}/discovery/resources").mock(
        return_value=httpx.Response(200, json=_discovery())
    )
    respx.get(f"{PEER}/s/peerstore/feed.json").mock(
        return_value=httpx.Response(200, json=_valid_feed())
    )
    federation.ingest_once()
    # default discovery does NOT include federated rows
    base = client.get("/discovery/resources").json()
    assert "federated" not in base
    # opt-in: labeled, linking OUT to the peer
    fed = client.get("/discovery/resources?include=federated").json()
    assert len(fed["federated"]) == 1
    row = fed["federated"][0]
    assert row["federated"] is True
    assert row["origin"] == PEER
    assert row["url"] == f"{PEER}/s/peerstore/"
    # no Tilla buy/mcp route is exposed for a federated row (out-links only)
    assert "buy" not in row and "mcp" not in row


def test_federated_absent_when_empty():
    _clear_listings()
    fed = client.get("/discovery/resources?include=federated").json()
    assert fed["federated"] == []


# --------------------------------------------------------------- no fund movement
def test_no_peer_checkout_proxy():
    """Grep-assert: federation.py contains ZERO fund-moving code — it never
    imports the checkout/payment/chain rails and never calls a settle/verify/
    deliver path. Discovery links OUT only; Tilla never settles a peer's sale."""
    src = (REPO / "app" / "federation.py").read_text(encoding="utf-8")
    forbidden = [
        "record_settlement",
        "verify_txhash",
        "checkout.",
        "payment.",
        "chain.",
        ".settle",
        "fulfill_agent_order",
        "create_order",
    ]
    for token in forbidden:
        assert token not in src, f"federation.py must not reference {token!r}"


# --------------------------------------------------------------- migration
def test_migration_up_down_up(tmp_path):
    """0016_federation applies, reverses, and re-applies cleanly against a scratch
    SQLite DB (additive table, no data loss on downgrade). env.py derives the URL
    from TILLA_DB_PATH, so the subprocess is fully isolated from the test DB."""
    import os

    from sqlalchemy import create_engine, inspect

    db = tmp_path / "fed_mig.db"
    url = f"sqlite:///{db.as_posix()}"
    env = {**os.environ, "TILLA_DB_PATH": str(db)}

    def _alembic(*args):
        return subprocess.run(
            ["python", "-m", "alembic", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            env=env,
        )

    up = _alembic("upgrade", "0016_federation")
    assert up.returncode == 0, up.stderr
    eng = create_engine(url)
    assert "federated_listings" in inspect(eng).get_table_names()
    eng.dispose()
    down = _alembic("downgrade", "0014_crosschain")
    assert down.returncode == 0, down.stderr
    eng = create_engine(url)
    assert "federated_listings" not in inspect(eng).get_table_names()
    eng.dispose()
    reup = _alembic("upgrade", "0016_federation")
    assert reup.returncode == 0, reup.stderr
