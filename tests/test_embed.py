"""M13 embed tests: /embed.js serves the asset with the right content-type + cache
headers, and the committed source is statically XSS-safe (shadow DOM + textContent,
a hard-coded base constant, strict slug/ref regexes, and none of the dangerous
DOM-injection sinks).
"""

import pathlib

from fastapi.testclient import TestClient

import app.main as main

client = TestClient(main.app)

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = (REPO / "assets" / "embed.js").read_text(encoding="utf-8")


def test_embed_js_served_with_headers():
    r = client.get("/embed.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "max-age=3600" in r.headers["Cache-Control"]
    assert "etag" in {k.lower() for k in r.headers}


def test_embed_source_has_hardcoded_base():
    # the buy URL base is a fixed literal, never derived from the embedding page
    assert 'BASE = "https://tilla.gudman.xyz"' in SRC


def test_embed_source_no_dangerous_sinks():
    for tok in [
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ]:
        assert tok not in SRC, f"embed.js must not use {tok}"


def test_embed_source_uses_safe_dom_apis():
    assert "attachShadow" in SRC
    assert "textContent" in SRC
    assert "addEventListener" in SRC
    assert "Buy with USDT" in SRC


def test_embed_source_validates_slug_and_ref():
    assert "[a-z0-9][a-z0-9-]{0,39}" in SRC  # slug pattern
    assert "0x[0-9a-fA-F]{40}" in SRC  # ref pattern


def test_embed_source_opens_popup_not_iframe():
    assert "window.open" in SRC
    # deliberately a popup, never a framed checkout (clickjacking / wallet-injection)
    assert 'createElement("iframe")' not in SRC
    assert "<iframe" not in SRC
