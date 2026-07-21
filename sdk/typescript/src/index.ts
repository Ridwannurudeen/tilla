/**
 * tilla-sdk — a typed, dependency-light TypeScript client for the Tilla
 * storefront API.
 *
 * Browse and human-checkout need nothing but the platform `fetch`. The x402 pay
 * paths (`createStore` / `buy`) additionally need a caller-supplied signer; the
 * shipped `LocalEip3009Signer` signs locally with `viem`. The SDK never bundles,
 * reads, defaults, or logs a private key.
 */

export { TillaClient, DEFAULT_BASE_URL } from "./client.js";
export type { TillaClientOptions } from "./client.js";

export {
  TillaError,
  PaymentRefused,
  SettlementUnknown,
  CheckoutExpired,
} from "./errors.js";

export {
  LocalEip3009Signer,
  TRANSFER_WITH_AUTHORIZATION_TYPES,
  VALIDITY_BUFFER_SECONDS,
  DEFAULT_EIP712_NAME,
  DEFAULT_EIP712_VERSION,
  DEFAULT_TIMEOUT_SECONDS,
} from "./signing.js";
export type { PaymentSigner } from "./signing.js";

export {
  EXACT_SCHEME,
  X_LAYER_NETWORK,
  USDT0_ASSET,
  decodePaymentRequired,
  selectRequirement,
  encodePaymentSignature,
  decodePaymentResponse,
  settleTxFromResponse,
} from "./x402Codec.js";
export type { PaymentChallenge } from "./x402Codec.js";

export {
  isPaid,
  feedProductFromJson,
  storeFeedFromJson,
  discoveryResourceFromJson,
  discoveryFromJson,
  checkoutFromJson,
  checkoutStatusFromJson,
  storeCreatedFromJson,
  purchaseFromJson,
} from "./models.js";
export type {
  FeedProduct,
  StoreFeed,
  DiscoveryResource,
  Discovery,
  Checkout,
  CheckoutStatus,
  StoreCreated,
  Purchase,
} from "./models.js";
