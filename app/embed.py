"""M13 embed button: serves the self-contained ``assets/embed.js`` at GET /embed.js.

A merchant drops one script tag on their own site and gets a shadow-DOM "Buy with
USDT — Tilla" button that opens the proven hosted checkout in a popup, wiring the
optional data-ref straight into the affiliate attribution ledger. The asset is a
committed repo file (reviewed, no build step); this route just serves it with a long
cache + nosniff so a browser re-uses it across the merchant's page loads.
"""

from __future__ import annotations

import pathlib

from fastapi import APIRouter, Request
from starlette.responses import FileResponse

router = APIRouter()

_EMBED_JS = pathlib.Path(__file__).resolve().parent.parent / "assets" / "embed.js"


@router.get("/embed.js")
def embed_js(request: Request):
    # FileResponse adds a stat-based ETag + Last-Modified and answers conditional
    # requests (304) on its own; we pin the long cache + nosniff. media_type is the
    # JS content type so a strict host page loads it as a script.
    return FileResponse(
        _EMBED_JS,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )
