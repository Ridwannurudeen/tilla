"""``TillaClient`` — a sync, typed wrapper over Tilla's live public HTTP surface.

Every method maps to an endpoint that exists in production today (verified against
app/main.py + app/agentic.py). The two fund-moving methods (``create_store``,
``buy``) require a caller-supplied :class:`~tilla_sdk.signing.PaymentSigner` and a
mandatory ``max_amount_micro`` cap, and they reproduce the ``warden_hire`` payer
invariants: refuse-before-sign on any pin mismatch, sign at most once, and never
re-fire a signed authorization (a post-sign transport failure raises
``SettlementUnknown``).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .errors import (
    CheckoutExpired,
    PaymentRefused,
    SettlementUnknown,
    TillaError,
)
from .models import (
    Checkout,
    CheckoutStatus,
    Discovery,
    DiscoveryResource,
    Purchase,
    StoreCreated,
    StoreFeed,
)
from .signing import PaymentSigner
from .x402_codec import (
    EXACT_SCHEME,
    USDT0_ASSET,
    X_LAYER_NETWORK,
    PaymentChallenge,
    decode_payment_required,
    select_requirement,
    settle_tx_from_response,
)

DEFAULT_BASE_URL = "https://tilla.gudman.xyz"
_PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
_PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
_PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"


class TillaClient:
    """A thin client over the Tilla storefront API.

    ``http_client`` lets a caller inject a preconfigured ``httpx.Client`` (proxies,
    retries, custom transport); otherwise the SDK owns one. Use as a context
    manager, or call :meth:`close`, to release the owned client.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
        http_client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        if http_client is not None:
            self._http = http_client
            self._owns_http = False
        else:
            self._http = httpx.Client(timeout=timeout, follow_redirects=False)
            self._owns_http = True

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "TillaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        r = self._http.get(self._url(path), params=params)
        r.raise_for_status()
        return r.json()

    # -- discovery / catalog (zero funds) ------------------------------------
    def discovery(self, limit: int = 20, offset: int = 0) -> Discovery:
        return Discovery.from_json(
            self._get_json("/discovery/resources", {"limit": limit, "offset": offset})
        )

    def search(self, q: str) -> list[DiscoveryResource]:
        data = self._get_json("/discovery/search", {"q": q})
        return [DiscoveryResource.from_json(r) for r in data.get("resources", []) or []]

    def feed(self, slug: str) -> StoreFeed:
        return StoreFeed.from_json(self._get_json(f"/s/{slug}/feed.json"))

    def llms_txt(self, slug: str) -> str:
        r = self._http.get(self._url(f"/s/{slug}/llms.txt"))
        r.raise_for_status()
        return r.text

    def agent_card(self) -> dict[str, Any]:
        return self._get_json("/.well-known/agent-card.json")

    # -- human checkout (zero funds from the SDK; a human/wallet pays) --------
    def create_checkout(self, slug: str, ref: str | None = None) -> Checkout:
        body = {"ref": ref} if ref else {}
        r = self._http.post(self._url(f"/api/checkout/{slug}"), json=body)
        r.raise_for_status()
        return Checkout.from_json(r.json())

    def checkout_status(self, cid: str) -> CheckoutStatus:
        return CheckoutStatus.from_json(self._get_json(f"/api/checkout/{cid}"))

    def submit_tx(self, cid: str, tx_hash: str) -> CheckoutStatus:
        r = self._http.post(
            self._url(f"/api/checkout/{cid}/tx"), json={"tx_hash": tx_hash}
        )
        r.raise_for_status()
        return CheckoutStatus.from_json(r.json())

    def wait_for_paid(
        self, cid: str, timeout: float = 900.0, interval: float = 5.0
    ) -> CheckoutStatus:
        """Poll ``checkout_status`` until the order is paid, expires, or ``timeout``
        elapses. ``interval`` defaults to 5s (12/min) to stay under the server's
        40/min status limit. Raises :class:`CheckoutExpired` on expiry and
        :class:`TillaError` on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            status = self.checkout_status(cid)
            if status.is_paid:
                return status
            if status.status == "expired":
                raise CheckoutExpired(f"checkout {cid} expired before payment")
            if time.monotonic() + interval > deadline:
                raise TillaError(f"timed out waiting for checkout {cid} to be paid")
            time.sleep(interval)

    # -- MCP tool surface (zero funds; pay is a separate signed step) ---------
    def mcp_call(
        self, slug: str, tool: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Call an MCP tool over JSON-RPC 2.0 (list_products / get_product /
        create_checkout / pay). Returns the tool's ``structuredContent``. Raises
        :class:`TillaError` on a JSON-RPC error."""
        rpc = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }
        r = self._http.post(self._url(f"/s/{slug}/mcp"), json=rpc)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            err = body["error"] or {}
            raise TillaError(f"MCP error {err.get('code')}: {err.get('message')}")
        result = body.get("result") or {}
        if isinstance(result, dict) and result.get("isError"):
            raise TillaError(f"MCP tool error: {result.get('content')}")
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        return result

    # -- x402 payment core (mirrors warden_hire.paid_scan invariants) --------
    def _decode_and_pin(
        self, response: httpx.Response, max_amount_micro: int, pin_pay_to: str | None
    ) -> PaymentChallenge:
        """Decode the 402 challenge and enforce every pin BEFORE any signing. Any
        failure raises :class:`PaymentRefused`; the signer is never reached."""
        header = response.headers.get(_PAYMENT_REQUIRED_HEADER)
        if not header:
            raise PaymentRefused("402 response carried no PAYMENT-REQUIRED header")
        try:
            required = decode_payment_required(header)
            challenge = select_requirement(required, EXACT_SCHEME, X_LAYER_NETWORK)
        except (ValueError, KeyError) as exc:
            raise PaymentRefused(f"undecodable x402 challenge: {exc}") from exc
        if challenge is None:
            raise PaymentRefused(
                f"no {EXACT_SCHEME} requirement on {X_LAYER_NETWORK} in the challenge"
            )
        if challenge.asset.lower() != USDT0_ASSET.lower():
            raise PaymentRefused(
                f"challenge asset {challenge.asset} is not USDT0", challenge
            )
        if challenge.amount_micro <= 0 or challenge.amount_micro > max_amount_micro:
            raise PaymentRefused(
                f"challenge amount {challenge.amount_micro} exceeds cap "
                f"{max_amount_micro}",
                challenge,
            )
        if pin_pay_to is not None and challenge.pay_to.lower() != pin_pay_to.lower():
            raise PaymentRefused(
                f"challenge payTo {challenge.pay_to} does not match the pinned "
                "recipient",
                challenge,
            )
        return challenge

    def _pay_once(
        self,
        method: str,
        path: str,
        *,
        signer: PaymentSigner,
        max_amount_micro: int,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        pin_pay_to: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """The one-clean-tx x402 payer: unpaid probe -> 402 -> pin-check -> sign
        once -> replay exactly once. Returns ``(response_json, settle_tx)``.

        Invariants (identical to warden_hire.paid_scan):
          * refuse before signing on any pin/cap failure (``PaymentRefused``);
          * sign at most once;
          * a transport failure AFTER signing raises ``SettlementUnknown`` and the
            authorization is never re-sent.
        """
        url = self._url(path)
        # 1) Unpaid probe.
        try:
            probe = self._http.request(method, url, json=json_body, params=params)
        except httpx.HTTPError as exc:
            raise TillaError(f"x402 probe failed: {exc}") from exc
        if probe.status_code != 402:
            # No paywall in front of this route (e.g. facilitator disabled) — a 2xx
            # already carries the result; anything else is a hard error. Either way
            # nothing was signed, so no funds moved.
            if probe.is_success:
                return probe.json(), settle_tx_from_response(
                    probe.headers.get(_PAYMENT_RESPONSE_HEADER)
                )
            raise TillaError(
                f"expected 402 challenge, got {probe.status_code}: {probe.text[:200]}"
            )

        # 2) Decode + PIN. No signing unless every pin matches.
        challenge = self._decode_and_pin(probe, max_amount_micro, pin_pay_to)

        # 3) Sign LOCALLY (one authorization). A signer that raises aborts before
        #    any paid request is sent.
        try:
            signature = signer.sign(challenge)
        except Exception as exc:
            raise PaymentRefused(f"signer refused/failed: {exc}", challenge) from exc

        # 4) Replay EXACTLY ONCE. A transport failure here does NOT re-fire.
        headers = {_PAYMENT_SIGNATURE_HEADER: signature}
        try:
            paid = self._http.request(
                method, url, json=json_body, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise SettlementUnknown(
                f"paid request failed after signing (do not re-pay): {exc}"
            ) from exc
        if not paid.is_success:
            raise TillaError(
                f"paid request returned {paid.status_code} (no settlement): "
                f"{paid.text[:200]}"
            )
        return paid.json(), settle_tx_from_response(
            paid.headers.get(_PAYMENT_RESPONSE_HEADER)
        )

    def create_store(
        self,
        description: str,
        receive_address: str | None = None,
        theme: str | None = None,
        *,
        signer: PaymentSigner,
        max_amount_micro: int,
    ) -> StoreCreated:
        """Create a live store by paying Tilla's x402 create-store fee (currently 0.05 USDT0).

        payTo is Tilla's own platform wallet; it is surfaced in the challenge to
        the signer for policy, and the amount is capped at ``max_amount_micro``.
        Returns the new store plus the settle tx from PAYMENT-RESPONSE.
        """
        body: dict[str, Any] = {"description": description}
        if receive_address:
            body["receive_address"] = receive_address
        if theme:
            body["theme"] = theme
        data, settle_tx = self._pay_once(
            "POST",
            "/create-store",
            signer=signer,
            max_amount_micro=max_amount_micro,
            json_body=body,
        )
        return StoreCreated.from_json(data, settle_tx)

    def buy(
        self,
        slug: str,
        *,
        signer: PaymentSigner,
        max_amount_micro: int,
        ref: str | None = None,
    ) -> Purchase:
        """Buy a store's product over x402 (payTo = the merchant, non-custodial).

        payTo cannot be pinned statically (it is the per-store merchant wallet, and
        feeds deliberately never leak it), so the guards are: the mandatory
        ``max_amount_micro`` cap, the asset/network/scheme pins, and surfacing the
        full challenge (including ``pay_to``) to the signer for a caller-policy
        veto. Returns the delivered goods plus the settle tx.
        """
        params = {"ref": ref} if ref else None
        data, settle_tx = self._pay_once(
            "POST",
            f"/s/{slug}/buy",
            signer=signer,
            max_amount_micro=max_amount_micro,
            params=params,
        )
        return Purchase.from_json(data, settle_tx)
