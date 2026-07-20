import app.main
from fastapi.testclient import TestClient

client = TestClient(app.main.app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "service": "tilla", "chain": "X Layer (196)"}


def test_create_store_503_without_llm_key():
    r = client.post("/create-store", json={"description": "I sell handmade socks"})
    assert r.status_code == 503


def test_checkout_404_unknown_store(tmp_path, monkeypatch):
    monkeypatch.setattr(app.main, "STORES", tmp_path)
    r = client.post("/api/checkout/does-not-exist")
    assert r.status_code == 404
