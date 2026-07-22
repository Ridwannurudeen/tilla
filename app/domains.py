"""Phase 4 custom domains — hostname validation + a DNS TXT ownership challenge.

The merchant-supplied domain is UNTRUSTED input: it is validated as a strict public
hostname before it is ever stored, echoed, or looked up, and it is always rendered
through the autoescaped Jinja env (never innerHTML) on any surface. Ownership is proven
out-of-band by a DNS TXT record — Tilla generates a per-claim token, the owner publishes
``tilla-domain-verification=<token>`` at ``_tilla-challenge.<domain>``, and
:func:`verify_domain` confirms it with a READ-ONLY DNS lookup.

SSRF: a TXT lookup only asks a DNS resolver for a record — it never opens a connection
to the claimed host or any host/port the merchant controls, so there is no request-
forgery surface. We still reject IP literals / localhost / the platform's own host up
front so a claim can only ever name a real, external, public DNS name.
"""

from __future__ import annotations

import ipaddress
import re
import secrets
import urllib.parse

import dns.exception
import dns.resolver

from app import config

# One DNS label: 1–63 chars, alnum with internal hyphens, no leading/trailing hyphen.
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# The subdomain the owner publishes the TXT challenge at, and the record prefix.
_CHALLENGE_PREFIX = "_tilla-challenge"
_TXT_PREFIX = "tilla-domain-verification="

# DNS lookup budget — a slow/hostile resolver must never hang the request thread.
_DNS_TIMEOUT = 5.0


class DomainError(ValueError):
    """A merchant-supplied domain failed hostname validation (→422)."""


def _platform_host() -> str:
    return (urllib.parse.urlparse(config.PUBLIC_BASE_URL).hostname or "").lower()


def normalize_domain(raw: str) -> str:
    """Validate + canonicalize a merchant-claimed hostname, or raise DomainError.

    Lowercased, trailing dot stripped. Rejects: empty, over-length (>253), a scheme or
    path (a bare hostname only), IP literals, ``localhost``, any single-label name, a
    label that isn't a strict DNS label, and the platform's own host (which must never
    be re-pointed to a merchant store)."""
    host = (raw or "").strip().lower().rstrip(".")
    if not host:
        raise DomainError("domain is required")
    if len(host) > 253:
        raise DomainError("domain is too long")
    # A URL/scheme/path/port/whitespace means it isn't a bare hostname.
    if any(c in host for c in "/:@ \t\r\n") or "//" in host:
        raise DomainError("domain must be a bare hostname (no scheme, port, or path)")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # not an IP literal — good
    else:
        raise DomainError("domain must be a hostname, not an IP address")
    if host == "localhost":
        raise DomainError("domain must be a public hostname")
    labels = host.split(".")
    if len(labels) < 2:
        raise DomainError("domain must be a fully-qualified hostname")
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise DomainError("domain has an invalid label")
    if host == _platform_host():
        raise DomainError("domain must differ from the platform host")
    return host


def new_token() -> str:
    """A fresh per-claim challenge token (URL-safe, high-entropy)."""
    return secrets.token_urlsafe(24)


def challenge_name(domain: str) -> str:
    """The TXT record name the owner must publish the token at."""
    return f"{_CHALLENGE_PREFIX}.{domain}"


def txt_record_value(token: str) -> str:
    """The exact TXT record value the owner must publish."""
    return f"{_TXT_PREFIX}{token}"


def _lookup_txt(name: str) -> list[str]:
    """Read-only TXT lookup. Returns the joined string of every TXT record at ``name``,
    or [] when the name has no TXT record / does not exist / the resolver errors — a
    fail-closed absence, never an exception on the request path."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = _DNS_TIMEOUT
    resolver.timeout = _DNS_TIMEOUT
    try:
        answer = resolver.resolve(name, "TXT")
    except (dns.exception.DNSException, ValueError):
        return []
    values: list[str] = []
    for record in answer:
        # A TXT rdata is one or more character-strings; join them into one value.
        values.append(b"".join(record.strings).decode("utf-8", "replace"))
    return values


def verify_domain(domain: str, token: str) -> bool:
    """True IFF the challenge TXT record for ``domain`` carries ``token``. Read-only
    DNS only — no connection to the claimed host is ever made."""
    if not token:
        return False
    expected = txt_record_value(token)
    return expected in _lookup_txt(challenge_name(domain))
