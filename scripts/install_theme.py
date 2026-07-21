#!/usr/bin/env python3
"""M15.2 — operator-only theme-plugin install CLI (offline, NOT an HTTP endpoint).

A theme is the ONE third-party plugin kind INV-1 permits to run in-process: it is
declarative Jinja2 rendered under the loader-owned autoescape env, never code.
Even so, an installed theme sits on the store-render path, so a candidate passes
five gates before it can be selected:

  1. sha256 pin       — the template file's hash must equal the manifest's, or the
                        install refuses (supply-chain: tampered artifact).
  2. template lint     — no ``|safe``, no ``{% raw %}``, no ``{% include %}`` that
                        escapes ``themes/``, no inline ``on*=`` event handlers.
  3. M1 XSS corpus     — the candidate is rendered with hostile store content and
                        every payload must come back inert.
  4. Warden screening  — the manifest text is screened fail-closed
                        (``providers.register_external``); only an ALLOW proceeds.
  5. operator approval — the row lands ``pending_review``; a SEPARATE ``approve``
                        invocation (an operator act) flips it ``active``, which is
                        what makes the theme selectable (``allowed_theme_names``).

Usage:
    python -m scripts.install_theme install <manifest.json>
    python -m scripts.install_theme approve <name>

The manifest is JSON: ``{"name","version","sha256","template"}`` where ``template``
is a path (relative to the manifest) to the theme's ``.html`` source.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys

from sqlalchemy import select

from app import config, providers, render
from app.db import SessionLocal
from app.models import Plugin

# The M1 XSS corpus (mirrors tests/test_render.py) plus the inert-render assertions
# — a candidate theme must neutralize every one of these exactly as the built-ins do.
XSS_PAYLOADS = (
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "javascript:alert(1)",
    '"><script>alert(1)</script>',
    "</style><script>alert(1)</script>",
    "'; alert(1); //",
    "{{7*7}}",  # SSTI probe: must render as literal text, never evaluate to 49
)

# Lint patterns. ``|safe`` and ``{% raw %}`` disable the loader's autoescape for a
# span; an inline ``on*=`` attribute is the classic markup-injection sink. All are
# banned outright in third-party themes (the audited built-ins are exempt — they are
# never installed through this path).
_SAFE_FILTER = re.compile(r"\|\s*safe\b")
_RAW_BLOCK = re.compile(r"{%-?\s*raw\b")
_INCLUDE = re.compile(r"""{%-?\s*include\s+['"]([^'"]+)['"]""")
_INLINE_HANDLER = re.compile(r"""<[^>]*?\son[a-z]+\s*=\s*['"]""", re.IGNORECASE)
# An include target that stays inside themes/: a bare ``name.html`` with no path
# separator and no parent traversal.
_BARE_TEMPLATE = re.compile(r"^[A-Za-z0-9_.-]+\.html$")

RENDER_ADDR = "0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51"
RENDER_SLUG = "install-probe"


class ThemeInstallError(ValueError):
    """A candidate theme failed one of the install gates (hash / lint / corpus)."""


def load_manifest(manifest_path: pathlib.Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in ("name", "version", "sha256", "template"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise ThemeInstallError(f"manifest missing required string field {field!r}")
    if not _BARE_TEMPLATE.fullmatch(f"{manifest['name']}.html"):
        raise ThemeInstallError(f"unsafe theme name {manifest['name']!r}")
    return manifest


def verify_hash(source_bytes: bytes, expected_sha256: str) -> None:
    actual = hashlib.sha256(source_bytes).hexdigest()
    if actual != expected_sha256.lower():
        raise ThemeInstallError(
            f"artifact sha256 mismatch: manifest {expected_sha256.lower()!r} "
            f"!= file {actual!r}"
        )


def lint_template(source: str) -> None:
    if _SAFE_FILTER.search(source):
        raise ThemeInstallError("theme uses the |safe filter (autoescape bypass)")
    if _RAW_BLOCK.search(source):
        raise ThemeInstallError("theme uses a {% raw %} block (autoescape bypass)")
    if _INLINE_HANDLER.search(source):
        raise ThemeInstallError("theme uses an inline on*= event-handler attribute")
    for target in _INCLUDE.findall(source):
        if ".." in target or not _BARE_TEMPLATE.fullmatch(target):
            raise ThemeInstallError(
                f"theme {{% include %}} escapes themes/: {target!r}"
            )


def _hostile_content(payload: str) -> dict:
    return {
        "store_name": payload,
        "tagline": payload,
        "hero_headline": payload,
        "hero_subcopy": payload,
        "product_name": payload,
        "product_blurb": payload,
        "cta_text": payload,
        "price_usdt": 9,
        "emoji": payload,
        "palette": {k: payload for k in ("primary", "accent", "bg", "text")},
    }


def run_xss_corpus(source: str) -> None:
    """Render the candidate with each hostile payload and assert it stays inert —
    the exact assertions the built-in themes pass in ``test_xss_corpus_renders_inert``."""
    for payload in XSS_PAYLOADS:
        html = render.render_source(
            source, _hostile_content(payload), RENDER_ADDR, RENDER_SLUG
        )
        for live in (
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
            "</style><script>alert(1)</script>",
        ):
            if live in html:
                raise ThemeInstallError(f"XSS corpus survived render: {live!r}")
        if 'href="javascript:alert(1)"' in html:
            raise ThemeInstallError("XSS corpus produced a live javascript: href")
        if payload == "{{7*7}}" and "49" in html:
            raise ThemeInstallError("theme evaluated an SSTI probe ({{7*7}} -> 49)")
        if render.DEFAULT_PALETTE["primary"] not in html:
            raise ThemeInstallError("theme let an unvalidated palette value through")


def install(
    manifest_path: pathlib.Path, *, operator: bool = True, session=None
) -> Plugin:
    """Run every install gate, then register the theme ``pending_review`` and write
    its template into ``themes/``. Registration (operator gate + INV-1 + fail-closed
    screening) runs BEFORE the file is written, so a blocked/unavailable screen or a
    non-operator caller leaves no artifact on disk."""
    manifest_path = pathlib.Path(manifest_path)
    manifest = load_manifest(manifest_path)
    source_bytes = (manifest_path.parent / manifest["template"]).read_bytes()
    verify_hash(source_bytes, manifest["sha256"])
    source = source_bytes.decode("utf-8")
    lint_template(source)
    run_xss_corpus(source)

    name = manifest["name"]
    own_session = session is None
    s = session or SessionLocal()
    try:
        row = providers.register_external(
            s,
            kind="theme",
            name=name,
            version=manifest["version"],
            artifact_sha256=manifest["sha256"].lower(),
            manifest={"template": f"{name}.html"},
            manifest_text=json.dumps(manifest, sort_keys=True),
            operator=operator,
        )
        (config.THEMES_DIR / f"{name}.html").write_text(source, encoding="utf-8")
        if own_session:
            s.commit()
        return row
    finally:
        if own_session:
            s.close()


def approve(name: str, *, session=None) -> Plugin:
    """Flip an installed theme ``pending_review`` -> ``active`` (an operator act).
    Activation is what adds the name to the selectable set."""
    own_session = session is None
    s = session or SessionLocal()
    try:
        row = s.scalar(
            select(Plugin).where(
                Plugin.kind == "theme",
                Plugin.name == name,
                Plugin.source == "external",
            )
        )
        if row is None:
            raise ThemeInstallError(f"no installed external theme named {name!r}")
        providers.set_status(s, row.id, "active", operator=True)
        if own_session:
            s.commit()
        return row
    finally:
        if own_session:
            s.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    command, arg = argv
    if command == "install":
        row = install(pathlib.Path(arg))
        print(
            f"installed theme {row.name!r} v{row.version} as pending_review "
            f"(id={row.id}); approve with: python -m scripts.install_theme approve {row.name}"
        )
        return 0
    if command == "approve":
        row = approve(arg)
        print(f"approved theme {row.name!r} (id={row.id}) — now active and selectable")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
