/**
 * LocalEip3009Signer tests: the emitted authorization must recover to the signer
 * and match the warden_hire type struct / domain byte-for-byte (viem as the
 * oracle). A fixed throwaway key is used — no real funds, no network.
 */

import { describe, expect, it } from "vitest";
import { privateKeyToAccount } from "viem/accounts";
import { recoverTypedDataAddress } from "viem";
import {
  LocalEip3009Signer,
  TRANSFER_WITH_AUTHORIZATION_TYPES,
  VALIDITY_BUFFER_SECONDS,
} from "../src/signing.js";
import type { PaymentChallenge } from "../src/x402Codec.js";

// Throwaway key — public knowledge, holds nothing.
const TEST_KEY = `0x${"11".repeat(32)}` as const;
const ASSET = "0x779ded0c9e1022225f8e0630b35a9b54be713736";
const PAY_TO = `0x${"22".repeat(20)}`;

function challenge(amount = 1_000_000): PaymentChallenge {
  const acceptedRaw = {
    scheme: "exact",
    network: "eip155:196",
    asset: ASSET,
    amount: String(amount),
    payTo: PAY_TO,
    maxTimeoutSeconds: 300,
    extra: { name: "USD₮0", version: "1" },
  };
  return {
    scheme: "exact",
    network: "eip155:196",
    asset: ASSET,
    amountMicro: amount,
    payTo: PAY_TO,
    maxTimeoutSeconds: 300,
    eip712Name: "USD₮0",
    eip712Version: "1",
    acceptedRaw,
    resource: undefined,
  };
}

describe("LocalEip3009Signer", () => {
  it("keeps the type struct identical to warden_hire", () => {
    expect(TRANSFER_WITH_AUTHORIZATION_TYPES).toEqual({
      TransferWithAuthorization: [
        { name: "from", type: "address" },
        { name: "to", type: "address" },
        { name: "value", type: "uint256" },
        { name: "validAfter", type: "uint256" },
        { name: "validBefore", type: "uint256" },
        { name: "nonce", type: "bytes32" },
      ],
    });
  });

  it("emits an authorization that recovers to the signer address", async () => {
    const signer = new LocalEip3009Signer(TEST_KEY);
    const acct = privateKeyToAccount(TEST_KEY);
    expect(signer.address).toBe(acct.address);

    const before = Math.floor(Date.now() / 1000);
    const header = await signer.sign(challenge());
    const after = Math.floor(Date.now() / 1000);

    const payload = JSON.parse(Buffer.from(header, "base64").toString("utf-8"));
    const auth = payload.payload.authorization;
    const signature = payload.payload.signature;

    // Wire shape mirrors warden_hire's ExactEIP3009Payload.
    expect(auth.from).toBe(acct.address);
    expect(auth.to).toBe(PAY_TO);
    expect(auth.value).toBe("1000000");
    expect(auth.nonce.startsWith("0x")).toBe(true);
    expect(auth.nonce.length).toBe(66);
    // validBefore = now + maxTimeoutSeconds; validAfter = now - 60s buffer.
    expect(Number(auth.validBefore) - Number(auth.validAfter)).toBe(
      300 + VALIDITY_BUFFER_SECONDS,
    );
    expect(before - VALIDITY_BUFFER_SECONDS - 1).toBeLessThanOrEqual(
      Number(auth.validAfter),
    );
    expect(Number(auth.validAfter)).toBeLessThanOrEqual(
      after - VALIDITY_BUFFER_SECONDS + 1,
    );

    // Rebuild the exact typed data and recover — proves domain + struct + message
    // are internally consistent with a valid signature by the signer's key.
    const recovered = await recoverTypedDataAddress({
      domain: {
        name: "USD₮0",
        version: "1",
        chainId: 196,
        verifyingContract: ASSET as `0x${string}`,
      },
      types: TRANSFER_WITH_AUTHORIZATION_TYPES,
      primaryType: "TransferWithAuthorization",
      message: {
        from: auth.from,
        to: auth.to,
        value: BigInt(auth.value),
        validAfter: BigInt(auth.validAfter),
        validBefore: BigInt(auth.validBefore),
        nonce: auth.nonce,
      },
      signature,
    });
    expect(recovered).toBe(acct.address);

    // The chosen requirement is echoed back verbatim.
    expect(payload.accepted.payTo).toBe(PAY_TO);
  });

  it("never leaks the key in JSON/string form", () => {
    const signer = new LocalEip3009Signer(TEST_KEY);
    expect(JSON.stringify({ signer })).not.toContain("1111111111");
    expect(String(signer.toJSON())).not.toContain(TEST_KEY);
  });

  it("rejects an empty key", () => {
    expect(() => new LocalEip3009Signer("")).toThrow();
  });
});
