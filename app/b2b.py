"""M16 B2B wholesale tiers: identity-gated pricing.

Wholesale tiers live inside the existing ``Product.pricing_params`` JSON column
(no migration) under a ``tiers`` key:

    pricing_params["tiers"] = [{"buyer": "erc8004:<id>" | "any_agent",
                                "price_micro": int}, ...]

The CORE INVARIANT (INV-1): a tier price is granted ONLY when the settled payer
wallet equals the on-chain owner of the presented ERC-8004 agent id. Ownership is
resolved by a READ-ONLY ``eth_call`` to the IdentityRegistry (``app.chain``). Any
unverifiable / mismatched claim, RPC outage, or unconfigured registry falls back
to the base price — fail-to-base, never fail-open-to-a-discount. Money is micro
ints only; no float arithmetic ever touches an amount here.
"""

from __future__ import annotations

import logging
import re
import threading
import time

from app import chain, config, payment
from app.models import Product

logger = logging.getLogger("tilla")

_BUYER_ANY = "any_agent"
_BUYER_ERC8004_RE = re.compile(r"^erc8004:(\d{1,20})$")

# Positive-result identity cache: agent_id -> (owner_wallet, expires_at). Only
# verified owners are cached (never an outage/None), so a discount is never served
# on stale trust; the TTL bounds enumeration/DoS on the public quote endpoint.
_owner_cache: dict[int, tuple[str, float]] = {}
_owner_cache_lock = threading.Lock()


def parse_agent_id(raw: str) -> int | None:
    """Accept a bare numeric id or the ``erc8004:<id>`` form; None if malformed."""
    raw = (raw or "").strip()
    m = _BUYER_ERC8004_RE.match(raw)
    if m:
        return int(m.group(1))
    if raw.isdigit():
        return int(raw)
    return None


def _tiers(product: Product) -> list[dict]:
    params = product.pricing_params if isinstance(product.pricing_params, dict) else {}
    tiers = params.get("tiers")
    return tiers if isinstance(tiers, list) else []


def match_tier(product: Product, agent_id: int) -> int | None:
    """The wholesale price_micro this agent id would pay: an exact
    ``erc8004:<id>`` tier wins over an ``any_agent`` tier. None if neither exists.
    Pure DB read — no on-chain call, no ownership check (INV-1 is enforced by
    :func:`effective_price_micro`)."""
    exact = f"erc8004:{agent_id}"
    exact_price: int | None = None
    any_price: int | None = None
    for tier in _tiers(product):
        buyer = tier.get("buyer")
        price = tier.get("price_micro")
        if not isinstance(price, int):
            continue
        if buyer == exact:
            exact_price = price
        elif buyer == _BUYER_ANY:
            any_price = price
    return exact_price if exact_price is not None else any_price


def verify_agent_owner(agent_id: int) -> str | None:
    """The on-chain owner wallet of ``agent_id`` via a read-only registry
    ``eth_call``, lowercased. None on ANY error: registry unconfigured, RPC
    outage/busy, empty/short result, or the zero address (fail-to-base)."""
    registry = config.ERC8004_REGISTRY
    if not registry:
        return None
    now = time.monotonic()
    with _owner_cache_lock:
        hit = _owner_cache.get(agent_id)
        if hit is not None and hit[1] > now:
            return hit[0]
    data = config.ERC8004_OWNER_SELECTOR + f"{agent_id:064x}"
    try:
        result = chain.eth_call(
            payment.CANONICAL_CHAIN, registry, data, timeout=config.RPC_TIMEOUT_REQUEST
        )
    except Exception:
        logger.exception("verify_agent_owner: eth_call failed for %s", agent_id)
        return None
    hexstr = (result or "")[2:] if (result or "").startswith("0x") else (result or "")
    if len(hexstr) < 64:
        return None
    owner = "0x" + hexstr[-40:].lower()
    try:
        if int(owner, 16) == 0:
            return None
    except ValueError:
        return None
    with _owner_cache_lock:
        _owner_cache[agent_id] = (owner, now + config.ERC8004_LOOKUP_TTL)
    return owner


def effective_price_micro(
    product: Product, agent_id: int | None, payer_wallet: str | None
) -> tuple[int, bool]:
    """INV-1 gate — the single source of truth for what a buy actually costs.

    Returns ``(price_micro, tier_applied)``. The tier price applies ONLY when a
    tier matches ``agent_id`` AND ``payer_wallet`` equals the verified on-chain
    owner of that agent id. Every other case — no agent id, no matching tier,
    unverifiable owner (RPC outage / unconfigured registry), or a payer/owner
    mismatch — returns the base ``product.price_micro`` with ``tier_applied=False``.
    This is the function the settle path re-derives against so a discount is never
    granted on trust."""
    base = product.price_micro
    if not agent_id or not payer_wallet:
        return base, False
    tier_price = match_tier(product, agent_id)
    if tier_price is None:
        return base, False
    owner = verify_agent_owner(agent_id)
    if owner is None or owner != payer_wallet.lower():
        return base, False
    return tier_price, True
