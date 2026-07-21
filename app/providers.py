"""M15.1 provider registry — the three hard-coded extension points formalized.

Delivery (:mod:`app.delivery`), payment accepts (:mod:`app.payment` +
:mod:`app.agentic`) and themes (:mod:`app.render` + ``themes/``) become explicit
provider Protocols with the built-ins as first-class, thin wrappers over the
EXISTING functions. This is a REFACTOR: nothing here is wired into the live
request path yet, so checkout / delivery / 402 bytes are unchanged (the goldens
in ``tests/test_providers.py`` assert it).

The ``plugins`` table is the metadata + review-state surface. Built-ins are
seeded ``source='builtin', status='active'``. Any external plugin defaults to
``pending_review`` and only an operator act (a fail-closed screening pass + an
explicit approval) can flip it ``active`` — INV-1 keeps that to the ``theme``
kind alone until an out-of-process runner exists (15.4).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, delivery
from app.models import Order, Plugin, Store

# The 15-token store-theme contract (M6): every theme renders exactly these names.
# The SEO/JSON-LD context is additive and not part of the contract.
THEME_TOKENS = frozenset(
    {
        "SLUG",
        "STORE_NAME",
        "TAGLINE",
        "HERO_HEADLINE",
        "HERO_SUBCOPY",
        "PRODUCT_NAME",
        "PRODUCT_BLURB",
        "CTA_TEXT",
        "PRICE",
        "EMOJI",
        "ADDR",
        "C_PRIMARY",
        "C_ACCENT",
        "C_BG",
        "C_TEXT",
    }
)

# INV-1 (sandbox boundary): the ONLY installable external plugin kind until the
# out-of-process runner (15.4) exists is 'theme'. delivery/payment_rail stay
# built-in-only.
EXTERNAL_KINDS = frozenset({"theme"})


class OperatorRequired(PermissionError):
    """A registry WRITE was attempted without the operator flag. Registry writes
    are operator-only (no merchant/public write) — the IDOR defense for 15.1."""


class SandboxBoundaryError(ValueError):
    """An external plugin of a non-'theme' kind was rejected by INV-1."""


class ThemeNameConflict(ValueError):
    """An external theme tried to claim a built-in theme name (would overwrite
    the audited built-in file on install)."""


# --------------------------------------------------------------- delivery providers
@runtime_checkable
class DeliveryProvider(Protocol):
    # ``mint`` is PAYLOAD-ONLY and side-effect-free: it computes the delivery
    # payload checkout.deliver would write, but state transitions, Entitlement/
    # Delivery rows, license persistence, webhooks, attest queueing and affiliate
    # accrual stay owned by checkout.deliver. A rewire may swap deliver's payload
    # branch for mint — never delegate more than the payload.
    kind: str

    def mint(self, session: Session, order: Order) -> str: ...

    def revoke(self, session: Session, order: Order) -> bool: ...


class _FileDelivery:
    """Wraps the file deliverable seam: the download-token mint/claim primitives
    and the shared entitlement revoke. ``mint`` returns the exact ready-message
    payload :func:`app.checkout.deliver` writes for a file order."""

    kind = "file"

    def mint(self, session: Session, order: Order) -> str:
        return delivery.FILE_READY_MESSAGE

    def revoke(self, session: Session, order: Order) -> bool:
        return delivery.revoke_entitlement(session, order)

    def download_token(self, entitlement_id: int, deliverable_id: int) -> str:
        return delivery.mint_download_token(entitlement_id, deliverable_id)

    def claim(self, session: Session, entitlement_id: int, max_downloads: int) -> bool:
        return delivery.claim_download(session, entitlement_id, max_downloads)


class _TextDelivery:
    """Wraps the text-secret seam. ``mint`` reproduces the payload
    :func:`app.checkout.deliver` writes for a text order (active text deliverable,
    else the legacy ``store.delivery`` string, else the default)."""

    kind = "text"

    def mint(self, session: Session, order: Order) -> str:
        from app import checkout

        deliverable = checkout._active_deliverable(session, order.store_id)
        if deliverable is not None and deliverable.kind == "text":
            return deliverable.payload or checkout.DEFAULT_DELIVERY
        store = session.get(Store, order.store_id)
        return store.delivery if store and store.delivery else checkout.DEFAULT_DELIVERY

    def revoke(self, session: Session, order: Order) -> bool:
        return delivery.revoke_entitlement(session, order)


class _LicenseDelivery:
    """Wraps the license seam: key mint + activate/deactivate + revoke."""

    kind = "license"

    def mint(self, session: Session, order: Order) -> str:
        return delivery.mint_license_key()

    def revoke(self, session: Session, order: Order) -> bool:
        return delivery.revoke_entitlement(session, order)

    def activate(self, session, ent, device_id: str, max_activations: int) -> str:
        return delivery.activate_license(session, ent, device_id, max_activations)

    def deactivate(self, session, ent, device_id: str) -> bool:
        return delivery.deactivate_license(session, ent, device_id)


# ----------------------------------------------------------- payment-rail providers
@runtime_checkable
class PaymentRailProvider(Protocol):
    scheme: str

    def build_accepts(self, rail) -> list: ...

    def pre_settle_gate(self, *args, **kwargs) -> int | None: ...

    def record_settlement(
        self, order_id: str, payment_response_header: str, scheme: str | None = None
    ) -> None: ...


class _ExactRail:
    """The built-in exact rail. ``build_accepts`` wraps
    :func:`app.payment.build_store_payment_options` (exact + flag-gated
    aggr_deferred second entry); ``record_settlement`` wraps
    :func:`app.agentic.record_settlement`. ``pre_settle_gate`` returns ``None`` —
    a provider may only NARROW (veto ≥400) a settlement, never originate one, and
    the built-in exact rail adds no veto beyond the live x402 middleware."""

    scheme = "exact"

    def build_accepts(self, rail) -> list:
        from app import payment

        return payment.build_store_payment_options(rail)

    def pre_settle_gate(self, *args, **kwargs) -> int | None:
        return None

    def record_settlement(
        self, order_id: str, payment_response_header: str, scheme: str | None = None
    ) -> None:
        from app import agentic

        agentic.record_settlement(order_id, payment_response_header, scheme)


# ----------------------------------------------------------------- theme providers
@runtime_checkable
class ThemeProvider(Protocol):
    name: str
    template: str

    def contract_ok(self) -> bool: ...


class _BuiltinTheme:
    def __init__(self, name: str) -> None:
        self.name = name
        self.template = f"{name}.html"

    def contract_ok(self) -> bool:
        """The template exists on disk and the name is in the allowed-theme set."""
        return (
            self.name in config.ALLOWED_THEMES
            and (config.THEMES_DIR / self.template).is_file()
        )


# ------------------------------------------------------------------ in-code registry
DELIVERY_PROVIDERS: dict[str, DeliveryProvider] = {
    p.kind: p for p in (_FileDelivery(), _TextDelivery(), _LicenseDelivery())
}
PAYMENT_RAIL_PROVIDERS: dict[str, PaymentRailProvider] = {
    p.scheme: p for p in (_ExactRail(),)
}
THEME_PROVIDERS: dict[str, ThemeProvider] = {
    t.name: t
    for t in (
        _BuiltinTheme("original"),
        _BuiltinTheme("bold"),
        _BuiltinTheme("editorial"),
    )
}

# The built-in seed rows for the plugins table. Delivery kinds and the exact rail
# carry a live provider object above; the MPP and period rails are registry
# ENTRIES (metadata) only — endpoint families, not accepts schemes — so they seed
# a row with no provider object. Every built-in is source='builtin', status
# 'active'. version '1' is a placeholder for the in-tree built-ins.
BUILTIN_SEED: tuple[dict, ...] = (
    {"kind": "delivery", "name": "file", "manifest": {"seam": "download_token"}},
    {"kind": "delivery", "name": "text", "manifest": {"seam": "text_secret"}},
    {"kind": "delivery", "name": "license", "manifest": {"seam": "license_key"}},
    {"kind": "payment_rail", "name": "exact", "manifest": {"scheme": "exact"}},
    {"kind": "payment_rail", "name": "mpp", "manifest": {"family": "endpoint"}},
    {"kind": "payment_rail", "name": "period", "manifest": {"family": "endpoint"}},
    {"kind": "theme", "name": "original", "manifest": {"template": "original.html"}},
    {"kind": "theme", "name": "bold", "manifest": {"template": "bold.html"}},
    {"kind": "theme", "name": "editorial", "manifest": {"template": "editorial.html"}},
)


def builtin_provider(kind: str, name: str):
    """Return the in-code provider object for a built-in, or None for a metadata-
    only entry (mpp / period) or an unknown key."""
    if kind == "delivery":
        return DELIVERY_PROVIDERS.get(name)
    if kind == "payment_rail":
        return PAYMENT_RAIL_PROVIDERS.get(name)
    if kind == "theme":
        return THEME_PROVIDERS.get(name)
    return None


# ------------------------------------------------------------------- read surface
def list_plugins(
    session: Session, kind: str | None = None, status: str | None = None
) -> list[Plugin]:
    stmt = select(Plugin).order_by(Plugin.kind, Plugin.name)
    if kind is not None:
        stmt = stmt.where(Plugin.kind == kind)
    if status is not None:
        stmt = stmt.where(Plugin.status == status)
    return list(session.scalars(stmt))


def active_names(session: Session, kind: str) -> set[str]:
    """The names of every ACTIVE plugin of `kind` (built-in ∪ approved external).
    An external row stuck in pending_review never appears here — it can never
    resolve until an operator approves it."""
    return set(
        session.scalars(
            select(Plugin.name).where(Plugin.kind == kind, Plugin.status == "active")
        )
    )


def is_active(session: Session, kind: str, name: str) -> bool:
    return name in active_names(session, kind)


def allowed_theme_names(session: Session | None = None) -> set[str]:
    """The selectable theme set (M15.2): the built-in frozenset ∪ every ACTIVE
    theme plugin. The union keeps the three built-ins selectable even before the
    plugins table is seeded; an operator-approved external theme becomes
    selectable the instant its row flips ``active``, and a still-``pending_review``
    theme never appears here. Opens a short-lived session when none is supplied
    (the create-store validators have no session in scope)."""
    if session is not None:
        return set(config.ALLOWED_THEMES) | active_names(session, "theme")
    from app.db import SessionLocal

    with SessionLocal() as s:
        return set(config.ALLOWED_THEMES) | active_names(s, "theme")


# ---------------------------------------------------------------- operator writes
def register_external(
    session: Session,
    *,
    kind: str,
    name: str,
    version: str,
    artifact_sha256: str,
    manifest: dict,
    manifest_text: str,
    operator: bool,
) -> Plugin:
    """Register an external plugin as ``pending_review`` (operator-only).

    Enforces, in order: operator gate (registry writes are operator-only), the
    INV-1 sandbox boundary (external kind must be 'theme'), then a FAIL-CLOSED
    Warden screening of the manifest text — only an explicit ALLOW proceeds; a
    BLOCK propagates (``screening.ScreeningBlocked``) and an unavailable screen
    holds the plugin out (``screening.ScreeningUnavailable``). The row is written
    ``pending_review``; a separate operator ``set_status`` flips it active."""
    if not operator:
        raise OperatorRequired("registry writes are operator-only")
    if kind not in EXTERNAL_KINDS:
        raise SandboxBoundaryError(
            f"external '{kind}' plugins are forbidden by INV-1 (theme-only)"
        )
    # Never let an external plugin claim a built-in theme name: install() writes
    # THEMES_DIR/<name>.html, which would overwrite the audited built-in file and
    # serve it live (built-ins resolve by on-disk filename, bypassing pending/
    # approve). The UNIQUE(kind,name) row also guards this, but only in a seeded
    # DB — reject explicitly so an unseeded DB can't be exploited.
    if kind == "theme" and name in config.ALLOWED_THEMES:
        raise ThemeNameConflict(f"'{name}' collides with a built-in theme")
    from app import screening

    outcome = screening.screen(manifest_text)
    if outcome.status != "allow":
        raise screening.ScreeningUnavailable(
            "manifest screening did not return ALLOW (fail-closed)"
        )
    row = Plugin(
        kind=kind,
        name=name,
        version=version,
        source="external",
        artifact_sha256=artifact_sha256,
        manifest=manifest,
        status="pending_review",
    )
    session.add(row)
    session.flush()
    return row


def set_status(
    session: Session, plugin_id: int, status: str, *, operator: bool
) -> Plugin:
    """Flip a plugin's review status (operator-only). Activation of an external
    plugin is the explicit operator approval the threat model requires."""
    if not operator:
        raise OperatorRequired("registry writes are operator-only")
    if status not in ("pending_review", "active", "disabled"):
        raise ValueError(f"invalid plugin status {status!r}")
    row = session.get(Plugin, plugin_id)
    if row is None:
        raise ValueError(f"no plugin with id {plugin_id}")
    row.status = status
    session.flush()
    return row


def seed_builtins(session: Session) -> int:
    """Idempotently insert the built-in plugin rows (source='builtin',
    status='active'). Returns the number of rows inserted. The prod path seeds
    via the 0011 migration; this covers create_all databases (tests). Nothing
    calls it at app startup — wire that only when a live path reads the table."""
    existing = {(p.kind, p.name) for p in list_plugins(session)}
    inserted = 0
    for entry in BUILTIN_SEED:
        key = (entry["kind"], entry["name"])
        if key in existing:
            continue
        session.add(
            Plugin(
                kind=entry["kind"],
                name=entry["name"],
                version="1",
                source="builtin",
                artifact_sha256=None,
                manifest=entry["manifest"],
                status="active",
            )
        )
        inserted += 1
    if inserted:
        session.flush()
    return inserted
