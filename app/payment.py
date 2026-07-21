"""One explicit x402 payment rail for Warden's paid HTTP routes."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Mapping

import httpx
from x402.http import OKXFacilitatorClient, PaymentOption
from x402.http.types import HTTPRequestContext
from x402.schemas import AssetAmount

PAYMENT_PROTOCOL = "x402-v2"
PAYMENT_FACILITATOR = "okx"
PAYMENT_SCHEME = "exact"
# The batch-buy scheme (a thin ExactEvmScheme wrapper the facilitator settles
# asynchronously). Only ever OFFERED as a second accepts-entry behind the
# TILLA_AGGR_DEFERRED flag; never claimed as settling without a real tx hash.
PAYMENT_SCHEME_AGGR_DEFERRED = "aggr_deferred"
PAYMENT_NETWORK = "eip155:196"
PAYMENT_ASSET = "0x779ded0c9e1022225f8e0630b35a9b54be713736"
PAYMENT_AMOUNT = "1000000"
PAYMENT_EIP712_NAME = "USD₮0"
PAYMENT_EIP712_VERSION = "1"
PAYMENT_SYMBOL = "USDT"
PAYMENT_DECIMALS = 6
PAYMENT_DISPLAY_PRICE = "1 USDT"
PAYMENT_TIMEOUT_SECONDS = 300
DEFAULT_FACILITATOR_URL = "https://web3.okx.com"

_FIXED_CONFIGURATION = {
    "TILLA_PAYMENT_FACILITATOR": PAYMENT_FACILITATOR,
    "TILLA_PAYMENT_SCHEME": PAYMENT_SCHEME,
    "TILLA_PAYMENT_NETWORK": PAYMENT_NETWORK,
    "TILLA_PAYMENT_ASSET": PAYMENT_ASSET,
    "TILLA_PAYMENT_AMOUNT": PAYMENT_AMOUNT,
    "TILLA_PAYMENT_EIP712_NAME": PAYMENT_EIP712_NAME,
    "TILLA_PAYMENT_EIP712_VERSION": PAYMENT_EIP712_VERSION,
    "TILLA_PAYMENT_SYMBOL": PAYMENT_SYMBOL,
    "TILLA_PAYMENT_DECIMALS": str(PAYMENT_DECIMALS),
}
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})


class NoRedirectOKXFacilitatorClient(OKXFacilitatorClient):
    """Pinned OKX client that never forwards signed headers through redirects."""

    def _get_async_client(self) -> Any:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            )
        return self._http_client

    def _get_sync_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
        )


@dataclass(frozen=True)
class PaymentRail:
    protocol: str
    facilitator: str
    facilitator_url: str
    scheme: str
    network: str
    asset: str
    amount: str
    eip712_name: str
    eip712_version: str
    symbol: str
    decimals: int
    display_price: str
    pay_to: str


def paywall_required(environment: Mapping[str, str]) -> bool:
    """Parse the paywall switch without treating misspellings as disabled."""
    configured = environment.get("TILLA_REQUIRE_PAYWALL", "")
    normalized = configured.lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(
        "TILLA_REQUIRE_PAYWALL must be one of "
        "'1', 'true', 'yes', 'on', '0', 'false', 'no', or 'off'"
    )


def load_payment_rail(environment: Mapping[str, str]) -> PaymentRail:
    """Load only the supported X Layer USDT rail and reject divergent settings."""
    paywall_required(environment)
    unknown_overrides = sorted(
        name
        for name in environment
        if name.startswith("TILLA_PAYMENT_") and name not in _FIXED_CONFIGURATION
    )
    if unknown_overrides:
        raise ValueError(f"unsupported payment setting: {unknown_overrides[0]}")
    for name, expected in _FIXED_CONFIGURATION.items():
        configured = environment.get(name)
        if configured is not None and configured != expected:
            raise ValueError(f"{name} must be {expected!r}")

    pay_to = environment.get("PAY_TO_ADDRESS", "")
    if _EVM_ADDRESS.fullmatch(pay_to) is None or int(pay_to[2:], 16) == 0:
        raise ValueError("PAY_TO_ADDRESS must be a non-zero 20-byte EVM address")

    facilitator_url = environment.get("OKX_BASE_URL", DEFAULT_FACILITATOR_URL)
    if facilitator_url != DEFAULT_FACILITATOR_URL:
        raise ValueError(f"OKX_BASE_URL must be {DEFAULT_FACILITATOR_URL!r}")

    return PaymentRail(
        protocol=PAYMENT_PROTOCOL,
        facilitator=PAYMENT_FACILITATOR,
        facilitator_url=facilitator_url,
        scheme=PAYMENT_SCHEME,
        network=PAYMENT_NETWORK,
        asset=PAYMENT_ASSET,
        amount=PAYMENT_AMOUNT,
        eip712_name=PAYMENT_EIP712_NAME,
        eip712_version=PAYMENT_EIP712_VERSION,
        symbol=PAYMENT_SYMBOL,
        decimals=PAYMENT_DECIMALS,
        display_price=PAYMENT_DISPLAY_PRICE,
        pay_to=pay_to,
    )


def build_payment_option(rail: PaymentRail) -> PaymentOption:
    """Build the installed x402 SDK option without USD-to-asset inference."""
    return PaymentOption(
        scheme=rail.scheme,
        price=AssetAmount(
            amount=rail.amount,
            asset=rail.asset,
            extra={
                "name": rail.eip712_name,
                "version": rail.eip712_version,
            },
        ),
        network=rail.network,
        pay_to=rail.pay_to,
        max_timeout_seconds=PAYMENT_TIMEOUT_SECONDS,
    )


def _dynamic_store_hooks(rail: PaymentRail):
    """The shared per-request pay_to/price resolvers for the wildcard store buy
    route. Both hooks are ASYNC (``asyncio.to_thread`` around a sync DB read) on
    purpose: ``_resolve_value`` only applies ``RouteConfig.hook_timeout_seconds``
    to coroutines — a sync hook would be unbounded AND run the blocking read on
    the event loop. The resolvers NEVER raise (a raising hook returns HTTP 500,
    not 404); on any unknown/invalid/pending/blocked store or DB error they return
    a deterministic sentinel (pay_to = Tilla's own PAY_TO_ADDRESS, price = 1
    micro). The sentinel is safe because funds move only at settle, and settle can
    never run for a dead store (the handler re-checks and returns >=400, which
    makes the middleware skip settlement)."""
    sentinel_pay_to = rail.pay_to

    async def _pay_to(ctx: HTTPRequestContext) -> str:
        from app.agentic import resolve_pay_to

        return await asyncio.to_thread(resolve_pay_to, ctx.path, sentinel_pay_to)

    async def _price(ctx: HTTPRequestContext) -> AssetAmount:
        from app.agentic import resolve_price

        return await asyncio.to_thread(resolve_price, ctx.path)

    return _pay_to, _price


def build_store_payment_option(rail: PaymentRail) -> PaymentOption:
    """The single exact WILDCARD PaymentOption for ``POST /s/:slug/buy`` (dynamic
    pay_to + price). Kept as the exact-only builder used everywhere flags are off;
    :func:`build_store_payment_options` wraps it for the accepts list."""
    pay_to, price = _dynamic_store_hooks(rail)
    return PaymentOption(
        scheme=rail.scheme,
        price=price,
        network=rail.network,
        pay_to=pay_to,
        max_timeout_seconds=PAYMENT_TIMEOUT_SECONDS,
    )


def build_store_payment_options(rail: PaymentRail) -> list[PaymentOption]:
    """The accepts list for the store buy route. ALWAYS ``[exact]`` (byte-identical
    to today); when ``config.AGGR_DEFERRED_ENABLED`` it appends a SECOND
    ``aggr_deferred`` entry sharing the exact same dynamic pay_to/price resolvers.
    The aggr entry is static (offered on every store's challenge); the agent-guard
    middleware strips it from the 402 for non-batch stores, and the buy handler
    hard-gates a non-batch aggr payment with a 409 BEFORE settle — so no dishonest
    challenge is served and zero funds move on a mis-scheme."""
    from app import config

    options = [build_store_payment_option(rail)]
    if config.AGGR_DEFERRED_ENABLED:
        pay_to, price = _dynamic_store_hooks(rail)
        options.append(
            PaymentOption(
                scheme=PAYMENT_SCHEME_AGGR_DEFERRED,
                price=price,
                network=rail.network,
                pay_to=pay_to,
                max_timeout_seconds=PAYMENT_TIMEOUT_SECONDS,
            )
        )
    return options
