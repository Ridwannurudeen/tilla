"""M15.2 theme-plugin install-path tests.

A theme is the ONE third-party plugin kind INV-1 permits in-process. These tests
prove the five install gates hold and that an operator-approved theme becomes
selectable and renders a live store, while a pending one is refused at create-store.
"""

import json
import pathlib

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import app.engine as engine
import app.main as main
from app import config, providers, render
from app.config import WARDEN_SCREEN_URL
from app.db import SessionLocal
from scripts import install_theme

client = TestClient(main.app)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "themes" / "midnight"
MANIFEST = FIXTURE / "manifest.json"
MIDNIGHT_SOURCE = (FIXTURE / "midnight.html").read_text(encoding="utf-8")

ADDR = "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"


@pytest.fixture
def clean_theme_file():
    """Remove any themes/midnight.html the install writes into the real themes dir,
    so a test artifact never lingers in the repo."""
    yield
    dest = config.THEMES_DIR / "midnight.html"
    if dest.exists():
        dest.unlink()


def _allow_screen():
    respx.post(WARDEN_SCREEN_URL).mock(
        return_value=httpx.Response(200, json={"verdict": "ALLOW"})
    )


# ----------------------------------------------------- XSS corpus through the plugin
@respx.mock
def test_plugin_theme_xss_corpus(clean_theme_file):
    """Every M1 XSS payload is inert when rendered through the plugin theme —
    run_xss_corpus raises if any survives, so a clean install proves the corpus."""
    _allow_screen()
    install_theme.run_xss_corpus(MIDNIGHT_SOURCE)  # no raise == corpus inert
    # and the same holds end-to-end through a real install
    row = install_theme.install(MANIFEST)
    assert row.status == "pending_review"
    assert (config.THEMES_DIR / "midnight.html").is_file()
    # a hostile render leaves the injected script escaped, never live
    html = render.render_source(
        MIDNIGHT_SOURCE,
        {"store_name": "<script>alert(1)</script>", "price_usdt": 9},
        ADDR,
        "probe",
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ------------------------------------------------------------------ hash pin
@respx.mock
def test_plugin_hash_mismatch_refused(clean_theme_file, tmp_path):
    _allow_screen()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["sha256"] = "0" * 64  # wrong hash
    tampered = tmp_path / "manifest.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "midnight.html").write_text(MIDNIGHT_SOURCE, encoding="utf-8")
    with pytest.raises(install_theme.ThemeInstallError, match="sha256 mismatch"):
        install_theme.install(tampered)
    # nothing written, no DB row
    assert not (config.THEMES_DIR / "midnight.html").exists()
    with SessionLocal() as s:
        assert providers.is_active(s, "theme", "midnight") is False


# ------------------------------------------------------------------ lint gate
def test_theme_lint_rejects_safe_filter():
    with pytest.raises(install_theme.ThemeInstallError, match="safe"):
        install_theme.lint_template("<h1>{{ STORE_NAME | safe }}</h1>")
    with pytest.raises(install_theme.ThemeInstallError, match="raw"):
        install_theme.lint_template("{% raw %}<b>{{x}}</b>{% endraw %}")
    with pytest.raises(install_theme.ThemeInstallError, match="on"):
        install_theme.lint_template('<img src="x" onerror="alert(1)">')
    with pytest.raises(install_theme.ThemeInstallError, match="include"):
        install_theme.lint_template('{% include "../secrets.html" %}')
    # the fixture theme is clean
    install_theme.lint_template(MIDNIGHT_SOURCE)


# --------------------------------------------- unapproved not selectable → approved
@respx.mock
def test_unapproved_theme_not_selectable(clean_theme_file):
    _allow_screen()
    install_theme.install(MANIFEST)  # lands pending_review
    with SessionLocal() as s:
        assert "midnight" not in providers.allowed_theme_names(s)
    # create-store with a pending theme is refused at the request boundary (422)
    r = client.post(
        "/create-store",
        json={"description": "i sell a thing", "theme": "midnight"},
    )
    assert r.status_code == 422
    # operator approval flips it active → now in the selectable set
    install_theme.approve("midnight")
    with SessionLocal() as s:
        assert "midnight" in providers.allowed_theme_names(s)


# ----------------------------------------- a live store rendered on the plugin theme
@respx.mock
def test_live_store_renders_on_plugin_theme(clean_theme_file, tmp_path, monkeypatch):
    _allow_screen()
    install_theme.install(MANIFEST)
    install_theme.approve("midnight")

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)
    from tests.test_engine import _fake_llm_response

    _fake_llm_response(
        monkeypatch,
        {"store_name": "Nocturne", "product_name": "Night Kit", "price_usdt": 9},
    )
    result = engine.create_store("i sell a night kit", theme="midnight")
    meta = json.loads((tmp_path / result["slug"] / "store.json").read_text("utf-8"))
    assert meta["status"] == "live"
    assert meta["theme"] == "midnight.html"
    index = (tmp_path / result["slug"] / "index.html").read_text("utf-8")
    assert "Midnight theme" in index  # the plugin theme's footer marker
    assert "Nocturne" in index
    print(f"live store on plugin theme: /s/{result['slug']}/ (theme=midnight.html)")
