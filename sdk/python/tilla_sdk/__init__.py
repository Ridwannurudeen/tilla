"""tilla-sdk — a sync, typed, dependency-light client for the Tilla storefront API.

Browse and human-checkout need nothing but ``httpx``. The x402 pay paths
(``create_store`` / ``buy``) additionally need a caller-supplied signer; the
shipped ``LocalEip3009Signer`` needs the optional ``[signer]`` extra (eth-account).
The SDK never bundles, reads, defaults, or logs a private key.
"""

from .client import DEFAULT_BASE_URL, TillaClient
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
    FeedProduct,
    Purchase,
    StoreCreated,
    StoreFeed,
)
from .signing import LocalEip3009Signer, PaymentSigner
from .x402_codec import (
    EXACT_SCHEME,
    USDT0_ASSET,
    X_LAYER_NETWORK,
    PaymentChallenge,
)

__version__ = "0.1.0"

__all__ = [
    "TillaClient",
    "DEFAULT_BASE_URL",
    "PaymentSigner",
    "LocalEip3009Signer",
    "PaymentChallenge",
    "EXACT_SCHEME",
    "X_LAYER_NETWORK",
    "USDT0_ASSET",
    "TillaError",
    "PaymentRefused",
    "SettlementUnknown",
    "CheckoutExpired",
    "Checkout",
    "CheckoutStatus",
    "Discovery",
    "DiscoveryResource",
    "FeedProduct",
    "StoreFeed",
    "StoreCreated",
    "Purchase",
]
