/**
 * Typed result models parsed from the live Tilla surface (verified against
 * app/main.py + app/agentic.py, same shapes the Python SDK parses). Unknown keys
 * are ignored so a forward-compatible server never breaks an older SDK.
 */

type JsonObject = Record<string, unknown>;

function asObject(value: unknown): JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function str(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  return fallback;
}

function int(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value))
    return Math.trunc(value);
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number.parseInt(value, 10);
    if (Number.isInteger(n)) return n;
  }
  return fallback;
}

function optStr(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function optInt(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value))
    return Math.trunc(value);
  return null;
}

export interface FeedProduct {
  id: string;
  title: string;
  description: string;
  link: string;
  priceAmount: string;
  currency: string;
  availability: string;
  x402Endpoint: string;
  x402Network: string;
  x402Asset: string;
  schemes: string[];
}

export function feedProductFromJson(d: JsonObject): FeedProduct {
  const price = asObject(d.price);
  const x402 = asObject(d.x402);
  return {
    id: str(d.id),
    title: str(d.title),
    description: str(d.description),
    link: str(d.link),
    priceAmount: str(price.amount),
    currency: str(price.currency),
    availability: str(d.availability),
    x402Endpoint: str(x402.endpoint),
    x402Network: str(x402.network),
    x402Asset: str(x402.asset),
    schemes: asArray(x402.schemes).map((s) => str(s)),
  };
}

export interface StoreFeed {
  slug: string;
  name: string;
  description: string;
  url: string;
  products: FeedProduct[];
}

export function storeFeedFromJson(d: JsonObject): StoreFeed {
  const store = asObject(d.store);
  return {
    slug: str(store.slug),
    name: str(store.name),
    description: str(store.description),
    url: str(store.url),
    products: asArray(d.products).map((p) => feedProductFromJson(asObject(p))),
  };
}

export interface DiscoveryResource {
  slug: string;
  name: string;
  description: string;
  url: string;
  feed: string;
  buy: string;
  mcp: string;
  priceMinMicro: number | null;
  priceMaxMicro: number | null;
  currency: string;
  network: string;
  soldCount: number;
}

export function discoveryResourceFromJson(d: JsonObject): DiscoveryResource {
  return {
    slug: str(d.slug),
    name: str(d.name),
    description: str(d.description),
    url: str(d.url),
    feed: str(d.feed),
    buy: str(d.buy),
    mcp: str(d.mcp),
    priceMinMicro: optInt(d.price_min_micro),
    priceMaxMicro: optInt(d.price_max_micro),
    currency: str(d.currency),
    network: str(d.network),
    soldCount: int(d.sold_count),
  };
}

export interface Discovery {
  service: string;
  total: number;
  resources: DiscoveryResource[];
}

export function discoveryFromJson(d: JsonObject): Discovery {
  return {
    service: str(d.service),
    total: int(d.total),
    resources: asArray(d.resources).map((r) =>
      discoveryResourceFromJson(asObject(r)),
    ),
  };
}

/** A freshly created human checkout order (POST /api/checkout/{slug}). */
export interface Checkout {
  id: string;
  payTo: string;
  amountMicro: number;
  expiresAt: string;
  network: string;
  token: string;
}

export function checkoutFromJson(d: JsonObject): Checkout {
  if (typeof d.id !== "string") {
    throw new Error("checkout response missing string id");
  }
  return {
    id: d.id,
    payTo: str(d.pay_to),
    amountMicro: int(d.amount_micro),
    expiresAt: str(d.expires_at),
    network: str(d.network),
    token: str(d.token),
  };
}

/** A checkout order's current state (GET /api/checkout/{cid}). */
export interface CheckoutStatus {
  id: string;
  status: string;
  amountMicro: number;
  payTo: string;
  txHash: string | null;
  delivery: string | null;
  downloadUrl: string | null;
  licenseKey: string | null;
}

export function checkoutStatusFromJson(d: JsonObject): CheckoutStatus {
  if (typeof d.id !== "string") {
    throw new Error("checkout status response missing string id");
  }
  return {
    id: d.id,
    status: str(d.status),
    amountMicro: int(d.amount_micro),
    payTo: str(d.pay_to),
    txHash: optStr(d.tx_hash),
    delivery: optStr(d.delivery),
    downloadUrl: optStr(d.download_url),
    licenseKey: optStr(d.license_key),
  };
}

export function isPaid(status: CheckoutStatus): boolean {
  return status.status === "paid";
}

/** The result of a successful x402-paid create-store. */
export interface StoreCreated {
  slug: string;
  url: string;
  manageKey: string;
  storeName: string;
  settleTx: string | null;
}

export function storeCreatedFromJson(
  d: JsonObject,
  settleTx: string | null,
): StoreCreated {
  return {
    slug: str(d.slug),
    url: str(d.url),
    manageKey: str(d.manage_key),
    storeName: str(d.store_name),
    settleTx,
  };
}

/** The result of a successful x402 agent buy (POST /s/{slug}/buy). */
export interface Purchase {
  orderId: string;
  amountMicro: number;
  delivery: string | null;
  downloadUrl: string | null;
  licenseKey: string | null;
  settleTx: string | null;
}

export function purchaseFromJson(
  d: JsonObject,
  settleTx: string | null,
): Purchase {
  return {
    orderId: str(d.order_id),
    amountMicro: int(d.amount_micro),
    delivery: optStr(d.delivery),
    downloadUrl: optStr(d.download_url),
    licenseKey: optStr(d.license_key),
    settleTx,
  };
}
