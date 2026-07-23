/**
 * x402 header codec + the decoded payment challenge.
 *
 * BUILD-TIME VERIFICATION (recorded in the SDK README): the production
 * `okxweb3-app-x402` wire format for PAYMENT-REQUIRED / PAYMENT-SIGNATURE /
 * PAYMENT-RESPONSE is plain `base64(JSON)` — its `safe_base64_encode` wraps a
 * camelCase model dump. Because it is not a richer envelope, this hand-rolled codec
 * keeps the SDK dependency-light instead of taking an x402 wire library as a
 * runtime dependency. The codec tests decode the SAME real x402-encoded fixtures
 * the Python SDK's golden tests use, proving both ports are wire-compatible.
 *
 * Field names on the wire are camelCase (`payTo`, `maxTimeoutSeconds`,
 * `x402Version`); the inner `payload` object of a signature is passed through
 * verbatim, matching the production `warden_hire` payer.
 */

/** Tilla's single settlement rail — the SDK pins to it (see client.ts). */
export const EXACT_SCHEME = "exact";
export const X_LAYER_NETWORK = "eip155:196";
export const USDT0_ASSET = "0x779ded0c9e1022225f8e0630b35a9b54be713736";

type JsonObject = Record<string, unknown>;

/**
 * One decoded, exact-scheme x402 requirement, ready to pin-check and sign.
 *
 * `acceptedRaw` is the original requirement object from the server's challenge; it
 * is echoed back verbatim as the `accepted` field of the PAYMENT-SIGNATURE payload
 * so the facilitator settles against exactly what it advertised.
 */
export interface PaymentChallenge {
  readonly scheme: string;
  readonly network: string;
  readonly asset: string;
  readonly amountMicro: number;
  readonly payTo: string;
  readonly maxTimeoutSeconds: number;
  readonly eip712Name: string | undefined;
  readonly eip712Version: string | undefined;
  readonly acceptedRaw: JsonObject;
  readonly resource: JsonObject | undefined;
}

function b64encodeJson(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj), "utf-8").toString("base64");
}

function b64decodeJson(value: string): unknown {
  return JSON.parse(Buffer.from(value, "base64").toString("utf-8"));
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Decode a PAYMENT-REQUIRED header into its raw object (`accepts` etc.). */
export function decodePaymentRequired(headerValue: string): JsonObject {
  const data = b64decodeJson(headerValue);
  if (!isObject(data)) {
    throw new Error("PAYMENT-REQUIRED did not decode to an object");
  }
  return data;
}

/**
 * Return the first requirement matching `scheme` + `network`, or `null`.
 *
 * Throws only if a matching entry is malformed (non-integer amount) — an absent
 * match is a clean `null` so the caller can refuse cleanly.
 */
export function selectRequirement(
  required: JsonObject,
  scheme: string,
  network: string,
): PaymentChallenge | null {
  const resource = isObject(required.resource) ? required.resource : undefined;
  const accepts = Array.isArray(required.accepts) ? required.accepts : [];
  for (const accept of accepts) {
    if (!isObject(accept)) {
      continue;
    }
    if (accept.scheme !== scheme || String(accept.network) !== network) {
      continue;
    }
    const extra = isObject(accept.extra) ? accept.extra : {};
    const amountStr = String(accept.amount);
    if (!/^\d+$/.test(amountStr)) {
      throw new Error(
        `x402 requirement carried a non-integer amount: ${amountStr}`,
      );
    }
    const amount = Number.parseInt(amountStr, 10);
    const timeout = Number.parseInt(String(accept.maxTimeoutSeconds ?? 0), 10);
    return {
      scheme: typeof accept.scheme === "string" ? accept.scheme : "",
      network: String(accept.network ?? ""),
      asset: typeof accept.asset === "string" ? accept.asset : "",
      amountMicro: amount,
      payTo: typeof accept.payTo === "string" ? accept.payTo : "",
      maxTimeoutSeconds: Number.isInteger(timeout) ? timeout : 0,
      eip712Name: typeof extra.name === "string" ? extra.name : undefined,
      eip712Version:
        typeof extra.version === "string" ? extra.version : undefined,
      acceptedRaw: accept,
      resource,
    };
  }
  return null;
}

/**
 * Encode a PAYMENT-SIGNATURE header value.
 *
 * `payload` is the scheme payload (`{ authorization: {...}, signature: "0x.." }`);
 * `acceptedRaw` is echoed back verbatim as `accepted`. The wire shape mirrors
 * x402's PaymentPayload exactly.
 */
export function encodePaymentSignature(
  acceptedRaw: JsonObject,
  payload: JsonObject,
): string {
  return b64encodeJson({
    x402Version: 2,
    payload,
    accepted: acceptedRaw,
  });
}

/**
 * Decode a PAYMENT-RESPONSE header into its raw SettleResponse object.
 * The settle transaction hash is under `transaction`.
 */
export function decodePaymentResponse(headerValue: string): JsonObject {
  const data = b64decodeJson(headerValue);
  if (!isObject(data)) {
    throw new Error("PAYMENT-RESPONSE did not decode to an object");
  }
  return data;
}

/**
 * Best-effort settle tx hash from a PAYMENT-RESPONSE header, or `null` if the
 * header is absent/undecodable (the payment still settled on a served 200).
 */
export function settleTxFromResponse(
  headerValue: string | null,
): string | null {
  if (!headerValue) {
    return null;
  }
  try {
    const tx = decodePaymentResponse(headerValue).transaction;
    return typeof tx === "string" && tx ? tx : null;
  } catch {
    return null;
  }
}
