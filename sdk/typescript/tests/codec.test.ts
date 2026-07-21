/**
 * Codec golden tests against the SAME real okxweb3-app-x402 header fixtures the
 * Python SDK's golden tests use. Both are plain `base64(JSON)` produced by the
 * production x402 encoder for an exact USDT0-on-X-Layer challenge and settle — so
 * decoding them here proves the TS port's hand-rolled codec is byte-for-byte
 * wire-compatible with the facilitator (and with the Python SDK).
 */

import { describe, expect, it } from "vitest";
import {
  EXACT_SCHEME,
  USDT0_ASSET,
  X_LAYER_NETWORK,
  decodePaymentRequired,
  encodePaymentSignature,
  selectRequirement,
  settleTxFromResponse,
} from "../src/x402Codec.js";

// base64(JSON) produced by x402.http.utils.encode_payment_required_header(...) —
// identical fixture to sdk/python/tests/test_codec.py.
const PAYMENT_REQUIRED_FIXTURE =
  "eyJ4NDAyVmVyc2lvbiI6MiwiYWNjZXB0cyI6W3sic2NoZW1lIjoiZXhhY3QiLCJuZXR3b3JrIjoi" +
  "ZWlwMTU1OjE5NiIsImFzc2V0IjoiMHg3NzlkZWQwYzllMTAyMjIyNWY4ZTA2MzBiMzVhOWI1NGJl" +
  "NzEzNzM2IiwiYW1vdW50IjoiMTAwMDAwMCIsInBheVRvIjoiMHhmNGM5ZmEwN2YzYmI4NTI1NDdm" +
  "ZGM0ZGY3YzFkOWZkOTk5MWNmYTUxIiwibWF4VGltZW91dFNlY29uZHMiOjMwMCwiZXh0cmEiOnsi" +
  "bmFtZSI6IlVTROKCrjAiLCJ2ZXJzaW9uIjoiMSJ9fV19";
const PAYMENT_RESPONSE_FIXTURE =
  "eyJzdWNjZXNzIjp0cnVlLCJwYXllciI6IjB4MTExMTExMTExMTExMTExMTExMTExMTExMTExMTEx" +
  "MTExMTExMTExMSIsInRyYW5zYWN0aW9uIjoiMHhhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFi" +
  "YWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiIiwibmV0d29yayI6ImVpcDE1NTox" +
  "OTYifQ==";

describe("x402 codec", () => {
  it("decodes + selects the exact requirement from the real fixture", () => {
    const required = decodePaymentRequired(PAYMENT_REQUIRED_FIXTURE);
    const challenge = selectRequirement(
      required,
      EXACT_SCHEME,
      X_LAYER_NETWORK,
    );
    expect(challenge).not.toBeNull();
    expect(challenge?.scheme).toBe("exact");
    expect(challenge?.network).toBe("eip155:196");
    expect(challenge?.asset.toLowerCase()).toBe(USDT0_ASSET);
    expect(challenge?.amountMicro).toBe(1_000_000);
    expect(challenge?.payTo).toBe("0xf4c9fa07f3bb852547fdc4df7c1d9fd9991cfa51");
    expect(challenge?.maxTimeoutSeconds).toBe(300);
    expect(challenge?.eip712Name).toBe("USD₮0");
    expect(challenge?.eip712Version).toBe("1");
    // accepted_raw is preserved verbatim for the signature echo-back.
    expect(challenge?.acceptedRaw.payTo).toBe(challenge?.payTo);
  });

  it("returns null on scheme or network mismatch", () => {
    const required = decodePaymentRequired(PAYMENT_REQUIRED_FIXTURE);
    expect(
      selectRequirement(required, "aggr_deferred", X_LAYER_NETWORK),
    ).toBeNull();
    expect(selectRequirement(required, EXACT_SCHEME, "eip155:1")).toBeNull();
  });

  it("extracts the settle tx from the real response fixture", () => {
    expect(settleTxFromResponse(PAYMENT_RESPONSE_FIXTURE)).toBe(
      `0x${"ab".repeat(32)}`,
    );
  });

  it("returns null when the response header is absent or malformed", () => {
    expect(settleTxFromResponse(null)).toBeNull();
    expect(settleTxFromResponse("not-base64-$$$")).toBeNull();
  });

  it("encodes a PAYMENT-SIGNATURE with the exact wire shape", () => {
    const required = decodePaymentRequired(PAYMENT_REQUIRED_FIXTURE);
    const challenge = selectRequirement(
      required,
      EXACT_SCHEME,
      X_LAYER_NETWORK,
    );
    expect(challenge).not.toBeNull();
    const inner = { authorization: { from: "0xabc" }, signature: "0xdeadbeef" };
    const header = encodePaymentSignature(challenge!.acceptedRaw, inner);
    const decoded = JSON.parse(Buffer.from(header, "base64").toString("utf-8"));
    expect(decoded.x402Version).toBe(2);
    expect(decoded.payload).toEqual(inner);
    // The chosen requirement is echoed back verbatim as `accepted`.
    expect(decoded.accepted.scheme).toBe("exact");
    expect(decoded.accepted.payTo).toBe(challenge!.payTo);
  });

  it("rejects a PAYMENT-REQUIRED that does not decode to an object", () => {
    const bad = Buffer.from(JSON.stringify([1, 2, 3]), "utf-8").toString(
      "base64",
    );
    expect(() => decodePaymentRequired(bad)).toThrow();
  });
});
