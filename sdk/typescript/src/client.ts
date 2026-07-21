/**
 * TillaClient — a typed wrapper over Tilla's live public HTTP surface.
 *
 * Every method maps to an endpoint live in production today (same surface the
 * Python `tilla-sdk` wraps). The two fund-moving methods (`createStore`, `buy`)
 * require a caller-supplied {@link PaymentSigner} and a mandatory `maxAmountMicro`
 * cap, and they reproduce the `warden_hire` payer invariants: refuse-before-sign on
 * any pin mismatch, sign at most once, and never re-fire a signed authorization (a
 * post-sign transport failure raises {@link SettlementUnknown}).
 */

import {
  CheckoutExpired,
  PaymentRefused,
  SettlementUnknown,
  TillaError,
} from "./errors.js";
import {
  type Checkout,
  type CheckoutStatus,
  type Discovery,
  type DiscoveryResource,
  type Purchase,
  type StoreCreated,
  type StoreFeed,
  checkoutFromJson,
  checkoutStatusFromJson,
  discoveryFromJson,
  discoveryResourceFromJson,
  isPaid,
  purchaseFromJson,
  storeCreatedFromJson,
  storeFeedFromJson,
} from "./models.js";
import type { PaymentSigner } from "./signing.js";
import {
  EXACT_SCHEME,
  USDT0_ASSET,
  X_LAYER_NETWORK,
  type PaymentChallenge,
  decodePaymentRequired,
  selectRequirement,
  settleTxFromResponse,
} from "./x402Codec.js";

export const DEFAULT_BASE_URL = "https://tilla.gudman.xyz";
const PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED";
const PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE";
const PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE";

type JsonObject = Record<string, unknown>;
type FetchLike = typeof fetch;
type QueryParams = Record<string, string | number | undefined>;

export interface TillaClientOptions {
  baseUrl?: string;
  /** Request timeout in milliseconds (default 15000). */
  timeoutMs?: number;
  /** Injectable fetch (tests / custom transports); defaults to global fetch. */
  fetch?: FetchLike;
}

function buildQuery(params: QueryParams | undefined): string {
  if (!params) return "";
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined) sp.set(k, String(v));
  }
  const q = sp.toString();
  return q ? `?${q}` : "";
}

export class TillaClient {
  readonly #baseUrl: string;
  readonly #timeoutMs: number;
  readonly #fetch: FetchLike;

  constructor(options: TillaClientOptions = {}) {
    this.#baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.#timeoutMs = options.timeoutMs ?? 15_000;
    this.#fetch = options.fetch ?? globalThis.fetch;
  }

  #url(path: string): string {
    return `${this.#baseUrl}${path}`;
  }

  async #request(
    method: string,
    path: string,
    opts: {
      json?: JsonObject;
      params?: QueryParams;
      headers?: Record<string, string>;
    } = {},
  ): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.#timeoutMs);
    try {
      const headers: Record<string, string> = { ...(opts.headers ?? {}) };
      let body: string | undefined;
      if (opts.json !== undefined) {
        headers["content-type"] = "application/json";
        body = JSON.stringify(opts.json);
      }
      return await this.#fetch(this.#url(path) + buildQuery(opts.params), {
        method,
        headers,
        body,
        redirect: "manual",
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  async #getJson(path: string, params?: QueryParams): Promise<unknown> {
    const r = await this.#request("GET", path, { params });
    if (!r.ok) {
      throw new TillaError(
        `GET ${path} returned ${r.status}: ${(await r.text()).slice(0, 200)}`,
      );
    }
    return r.json();
  }

  // -- discovery / catalog (zero funds) ------------------------------------
  async discovery(limit = 20, offset = 0): Promise<Discovery> {
    return discoveryFromJson(
      asObject(await this.#getJson("/discovery/resources", { limit, offset })),
    );
  }

  async search(q: string): Promise<DiscoveryResource[]> {
    const data = asObject(await this.#getJson("/discovery/search", { q }));
    const rows = Array.isArray(data.resources) ? data.resources : [];
    return rows.map((r) => discoveryResourceFromJson(asObject(r)));
  }

  async feed(slug: string): Promise<StoreFeed> {
    return storeFeedFromJson(
      asObject(await this.#getJson(`/s/${slug}/feed.json`)),
    );
  }

  async llmsTxt(slug: string): Promise<string> {
    const r = await this.#request("GET", `/s/${slug}/llms.txt`);
    if (!r.ok) {
      throw new TillaError(`GET /s/${slug}/llms.txt returned ${r.status}`);
    }
    return r.text();
  }

  async agentCard(): Promise<JsonObject> {
    return asObject(await this.#getJson("/.well-known/agent-card.json"));
  }

  // -- human checkout (zero funds from the SDK; a human/wallet pays) --------
  async createCheckout(slug: string, ref?: string): Promise<Checkout> {
    const r = await this.#request("POST", `/api/checkout/${slug}`, {
      json: ref ? { ref } : {},
    });
    if (!r.ok) {
      throw new TillaError(`create_checkout returned ${r.status}`);
    }
    return checkoutFromJson(asObject(await r.json()));
  }

  async checkoutStatus(cid: string): Promise<CheckoutStatus> {
    return checkoutStatusFromJson(
      asObject(await this.#getJson(`/api/checkout/${cid}`)),
    );
  }

  async submitTx(cid: string, txHash: string): Promise<CheckoutStatus> {
    const r = await this.#request("POST", `/api/checkout/${cid}/tx`, {
      json: { tx_hash: txHash },
    });
    if (!r.ok) {
      throw new TillaError(`submit_tx returned ${r.status}`);
    }
    return checkoutStatusFromJson(asObject(await r.json()));
  }

  /**
   * Poll `checkoutStatus` until the order is paid, expires, or `timeoutMs`
   * elapses. `intervalMs` defaults to 5s (12/min) to stay under the server's
   * 40/min status limit. Throws {@link CheckoutExpired} on expiry and
   * {@link TillaError} on timeout.
   */
  async waitForPaid(
    cid: string,
    timeoutMs = 900_000,
    intervalMs = 5_000,
  ): Promise<CheckoutStatus> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const status = await this.checkoutStatus(cid);
      if (isPaid(status)) {
        return status;
      }
      if (status.status === "expired") {
        throw new CheckoutExpired(`checkout ${cid} expired before payment`);
      }
      if (Date.now() + intervalMs > deadline) {
        throw new TillaError(
          `timed out waiting for checkout ${cid} to be paid`,
        );
      }
      await sleep(intervalMs);
    }
  }

  // -- MCP tool surface (zero funds; pay is a separate signed step) ---------
  /**
   * Call an MCP tool over JSON-RPC 2.0 (list_products / get_product /
   * create_checkout / pay). Returns the tool's `structuredContent`. Throws
   * {@link TillaError} on a JSON-RPC error.
   */
  async mcpCall(
    slug: string,
    tool: string,
    args: JsonObject = {},
  ): Promise<unknown> {
    const r = await this.#request("POST", `/s/${slug}/mcp`, {
      json: {
        jsonrpc: "2.0",
        id: 1,
        method: "tools/call",
        params: { name: tool, arguments: args },
      },
    });
    if (!r.ok) {
      throw new TillaError(`MCP call returned ${r.status}`);
    }
    const body = asObject(await r.json());
    if ("error" in body && body.error) {
      const err = asObject(body.error);
      throw new TillaError(
        `MCP error ${String(err.code)}: ${String(err.message)}`,
      );
    }
    const result = asObject(body.result);
    if (result.isError) {
      throw new TillaError(`MCP tool error: ${JSON.stringify(result.content)}`);
    }
    if ("structuredContent" in result) {
      return result.structuredContent;
    }
    return result;
  }

  // -- x402 payment core (mirrors warden_hire payer invariants) -------------
  /**
   * Decode the 402 challenge and enforce every pin BEFORE any signing. Any
   * failure throws {@link PaymentRefused}; the signer is never reached.
   */
  #decodeAndPin(
    response: Response,
    maxAmountMicro: number,
    pinPayTo: string | undefined,
  ): PaymentChallenge {
    const header = response.headers.get(PAYMENT_REQUIRED_HEADER);
    if (!header) {
      throw new PaymentRefused(
        "402 response carried no PAYMENT-REQUIRED header",
      );
    }
    let challenge: PaymentChallenge | null;
    try {
      const required = decodePaymentRequired(header);
      challenge = selectRequirement(required, EXACT_SCHEME, X_LAYER_NETWORK);
    } catch (exc) {
      throw new PaymentRefused(`undecodable x402 challenge: ${String(exc)}`);
    }
    if (challenge === null) {
      throw new PaymentRefused(
        `no ${EXACT_SCHEME} requirement on ${X_LAYER_NETWORK} in the challenge`,
      );
    }
    if (challenge.asset.toLowerCase() !== USDT0_ASSET.toLowerCase()) {
      throw new PaymentRefused(
        `challenge asset ${challenge.asset} is not USDT0`,
        challenge,
      );
    }
    if (challenge.amountMicro <= 0 || challenge.amountMicro > maxAmountMicro) {
      throw new PaymentRefused(
        `challenge amount ${challenge.amountMicro} exceeds cap ${maxAmountMicro}`,
        challenge,
      );
    }
    if (
      pinPayTo !== undefined &&
      challenge.payTo.toLowerCase() !== pinPayTo.toLowerCase()
    ) {
      throw new PaymentRefused(
        `challenge payTo ${challenge.payTo} does not match the pinned recipient`,
        challenge,
      );
    }
    return challenge;
  }

  /**
   * The one-clean-tx x402 payer: unpaid probe -> 402 -> pin-check -> sign once ->
   * replay exactly once. Returns `[responseJson, settleTx]`.
   *
   * Invariants (identical to warden_hire.paid_scan):
   *   - refuse before signing on any pin/cap failure ({@link PaymentRefused});
   *   - sign at most once;
   *   - a transport failure AFTER signing throws {@link SettlementUnknown} and the
   *     authorization is never re-sent.
   */
  async #payOnce(
    method: string,
    path: string,
    opts: {
      signer: PaymentSigner;
      maxAmountMicro: number;
      json?: JsonObject;
      params?: QueryParams;
      pinPayTo?: string;
    },
  ): Promise<[JsonObject, string | null]> {
    // 1) Unpaid probe.
    let probe: Response;
    try {
      probe = await this.#request(method, path, {
        json: opts.json,
        params: opts.params,
      });
    } catch (exc) {
      throw new TillaError(`x402 probe failed: ${String(exc)}`);
    }
    if (probe.status !== 402) {
      // No paywall in front of this route — a 2xx already carries the result;
      // anything else is a hard error. Either way nothing was signed.
      if (probe.ok) {
        return [
          asObject(await probe.json()),
          settleTxFromResponse(probe.headers.get(PAYMENT_RESPONSE_HEADER)),
        ];
      }
      throw new TillaError(
        `expected 402 challenge, got ${probe.status}: ${(await probe.text()).slice(0, 200)}`,
      );
    }

    // 2) Decode + PIN. No signing unless every pin matches.
    const challenge = this.#decodeAndPin(
      probe,
      opts.maxAmountMicro,
      opts.pinPayTo,
    );

    // 3) Sign LOCALLY (one authorization). A signer that throws aborts before any
    //    paid request is sent.
    let signature: string;
    try {
      signature = await opts.signer.sign(challenge);
    } catch (exc) {
      throw new PaymentRefused(
        `signer refused/failed: ${String(exc)}`,
        challenge,
      );
    }

    // 4) Replay EXACTLY ONCE. A transport failure here does NOT re-fire.
    let paid: Response;
    try {
      paid = await this.#request(method, path, {
        json: opts.json,
        params: opts.params,
        headers: { [PAYMENT_SIGNATURE_HEADER]: signature },
      });
    } catch (exc) {
      throw new SettlementUnknown(
        `paid request failed after signing (do not re-pay): ${String(exc)}`,
      );
    }
    if (!paid.ok) {
      throw new TillaError(
        `paid request returned ${paid.status} (no settlement): ${(await paid.text()).slice(0, 200)}`,
      );
    }
    return [
      asObject(await paid.json()),
      settleTxFromResponse(paid.headers.get(PAYMENT_RESPONSE_HEADER)),
    ];
  }

  /**
   * Create a live store by paying Tilla's x402 create-store fee. payTo is Tilla's
   * platform wallet; it is surfaced to the signer for policy, and the amount is
   * capped at `maxAmountMicro`. Returns the new store plus the settle tx.
   */
  async createStore(
    description: string,
    opts: {
      signer: PaymentSigner;
      maxAmountMicro: number;
      receiveAddress?: string;
      theme?: string;
      /** Refuse (before signing) unless the 402 challenge's payTo matches. */
      pinPayTo?: string;
    },
  ): Promise<StoreCreated> {
    const body: JsonObject = { description };
    if (opts.receiveAddress) body.receive_address = opts.receiveAddress;
    if (opts.theme) body.theme = opts.theme;
    const [data, settleTx] = await this.#payOnce("POST", "/create-store", {
      signer: opts.signer,
      maxAmountMicro: opts.maxAmountMicro,
      json: body,
      pinPayTo: opts.pinPayTo,
    });
    return storeCreatedFromJson(data, settleTx);
  }

  /**
   * Buy a store's product over x402 (payTo = the merchant, non-custodial).
   *
   * payTo cannot be pinned statically (it is the per-store merchant wallet, and
   * feeds deliberately never leak it), so the guards are: the mandatory
   * `maxAmountMicro` cap, the asset/network/scheme pins, and surfacing the full
   * challenge (including `payTo`) to the signer for a caller-policy veto.
   */
  async buy(
    slug: string,
    opts: { signer: PaymentSigner; maxAmountMicro: number; ref?: string },
  ): Promise<Purchase> {
    const [data, settleTx] = await this.#payOnce("POST", `/s/${slug}/buy`, {
      signer: opts.signer,
      maxAmountMicro: opts.maxAmountMicro,
      params: opts.ref ? { ref: opts.ref } : undefined,
    });
    return purchaseFromJson(data, settleTx);
  }
}

function asObject(value: unknown): JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
