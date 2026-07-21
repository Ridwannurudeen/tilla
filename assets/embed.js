/*
 * Tilla embed button — a self-contained, dependency-free buy button a merchant drops
 * on their own site:
 *
 *   <script src="https://tilla.gudman.xyz/embed.js"
 *           data-tilla-store="SLUG" data-ref="0x.." async></script>
 *
 * Security posture (see docs/acp-checkout.md):
 *  - locates its own <script> tag and reads data-tilla-store + optional data-ref;
 *  - validates BOTH against strict patterns and SILENTLY no-ops on failure;
 *  - renders inside an attached shadow DOM (style isolation from the host page);
 *  - every string is set via textContent — no HTML-injection sink, no dynamic code
 *    evaluation, and zero interpolation of host-page data into markup;
 *  - the checkout URL is built off a HARD-CODED literal base, never derived from the
 *    embedding page, so an attacker page cannot redirect the buy flow;
 *  - opens the proven hosted checkout in a POPUP (not an iframe) so wallet
 *    extensions inject reliably and the store page's X-Frame-Options is honoured;
 *  - listeners via addEventListener only (no inline handlers), CSP-friendly.
 */
(function () {
  "use strict";

  // Fixed constant — the buy flow can NEVER be pointed at another origin.
  var BASE = "https://tilla.gudman.xyz";
  var SLUG_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;
  var REF_RE = /^0x[0-9a-fA-F]{40}$/;

  function findSelf() {
    if (document.currentScript) {
      return document.currentScript;
    }
    var scripts = document.getElementsByTagName("script");
    for (var i = scripts.length - 1; i >= 0; i--) {
      var src = scripts[i].getAttribute("src") || "";
      if (src.indexOf("/embed.js") !== -1) {
        return scripts[i];
      }
    }
    return null;
  }

  var self = findSelf();
  if (!self) {
    return;
  }

  var slug = self.getAttribute("data-tilla-store") || "";
  var ref = self.getAttribute("data-ref") || "";
  if (!SLUG_RE.test(slug)) {
    return; // silent no-op on a bad/missing slug
  }

  var url = BASE + "/s/" + slug + "/";
  if (ref && REF_RE.test(ref)) {
    url += "?ref=" + ref;
  }

  var host = document.createElement("span");
  host.setAttribute("data-tilla-embed", slug);
  var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;

  var style = document.createElement("style");
  style.textContent = [
    ":host{all:initial}",
    ".tilla-btn{",
    "display:inline-flex;align-items:center;justify-content:center;gap:8px;",
    "font:600 15px/1.2 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;",
    "color:#fff;background:#111;border:0;border-radius:10px;",
    "padding:12px 18px;cursor:pointer;text-decoration:none;",
    "}",
    ".tilla-btn:hover{background:#000}",
    ".tilla-btn:focus-visible{outline:2px solid #6aa9ff;outline-offset:2px}",
  ].join("");
  root.appendChild(style);

  var btn = document.createElement("button");
  btn.type = "button";
  btn.className = "tilla-btn";
  btn.textContent = "Buy with USDT — Tilla";
  btn.addEventListener("click", function () {
    window.open(url, "_blank", "noopener,noreferrer");
  });
  root.appendChild(btn);

  if (self.parentNode) {
    self.parentNode.insertBefore(host, self.nextSibling);
  }
})();
