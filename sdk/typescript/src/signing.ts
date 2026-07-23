/**
 * The signer hook — the ONLY place a private key is ever used.
 *
 * The SDK NEVER bundles, reads, defaults, or logs a key. The caller constructs a
 * signer (from their own env / keystore / EIP-1193 wallet / remote service) and
 * hands it to the x402 methods. A remote or in-browser signer can satisfy
 * {@link PaymentSigner} without the key ever entering this process.
 *
 * {@link LocalEip3009Signer} reproduces the production `warden_hire` payer's local
 * EIP-712 `TransferWithAuthorization` signing: same type struct, same 60-second
 * `validAfter` buffer, 32-byte random nonce, and the same wire shape. It signs with
 * `viem` (see README for why viem over hand-rolled noble EIP-712).
 */

import { randomBytes } from "node:crypto";
import { privateKeyToAccount } from "viem/accounts";
import type { Address, Hex, TypedDataDomain } from "viem";
import { encodePaymentSignature, type PaymentChallenge } from "./x402Codec.js";

/**
 * The EIP-3009 struct, identical to app/warden_hire.py. Field order/types must
 * match the facilitator's expectation exactly.
 */
export const TRANSFER_WITH_AUTHORIZATION_TYPES = {
  TransferWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
} as const;

/** Small clock-skew buffer for validAfter (mirrors the facilitator window). */
export const VALIDITY_BUFFER_SECONDS = 60;
/** Fallbacks only when the challenge omits extra.name / extra.version. */
export const DEFAULT_EIP712_NAME = "USD₮0";
export const DEFAULT_EIP712_VERSION = "1";
export const DEFAULT_TIMEOUT_SECONDS = 300;

/**
 * A callable that turns a decoded x402 challenge into a PAYMENT-SIGNATURE header
 * value. Implementations receive the FULL challenge (including `payTo`) so a caller
 * policy can veto before producing a signature. May be sync or async (an EIP-1193
 * `personal_sign`/`eth_signTypedData_v4` round-trip is async).
 */
export interface PaymentSigner {
  sign(challenge: PaymentChallenge): string | Promise<string>;
}

function requireHexAddress(asset: string): Address {
  if (!/^0x[0-9a-fA-F]{40}$/.test(asset)) {
    throw new Error(`challenge asset is not a 20-byte address: ${asset}`);
  }
  return asset as Address;
}

/**
 * Signs EIP-3009 authorizations locally with a caller-supplied private key.
 *
 * The key is held only on this instance and is never logged or serialized. Use a
 * throwaway/dedicated key; this object moves real funds when its signature is
 * submitted to a live facilitator.
 */
export class LocalEip3009Signer implements PaymentSigner {
  readonly #account: ReturnType<typeof privateKeyToAccount>;

  constructor(privateKey: string) {
    if (!privateKey) {
      throw new Error("privateKey is required");
    }
    const key = (
      privateKey.startsWith("0x") ? privateKey : `0x${privateKey}`
    ) as Hex;
    this.#account = privateKeyToAccount(key);
  }

  get address(): Address {
    return this.#account.address;
  }

  /** Keep the key out of any accidental string/JSON dump. */
  toJSON(): string {
    return `LocalEip3009Signer(${this.#account.address})`;
  }

  async sign(challenge: PaymentChallenge): Promise<string> {
    const chainId = Number.parseInt(challenge.network.split(":")[1] ?? "", 10);
    if (!Number.isInteger(chainId)) {
      throw new Error(
        `challenge network is not eip155:<chainId>: ${challenge.network}`,
      );
    }
    const name = challenge.eip712Name ?? DEFAULT_EIP712_NAME;
    const version = challenge.eip712Version ?? DEFAULT_EIP712_VERSION;
    const now = Math.floor(Date.now() / 1000);
    const validAfter = now - VALIDITY_BUFFER_SECONDS;
    const validBefore =
      now + (challenge.maxTimeoutSeconds || DEFAULT_TIMEOUT_SECONDS);
    const nonceHex = `0x${randomBytes(32).toString("hex")}` as Hex;
    const value = challenge.amountMicro;

    const domain: TypedDataDomain = {
      name,
      version,
      chainId,
      verifyingContract: requireHexAddress(challenge.asset),
    };

    const signature = await this.#account.signTypedData({
      domain,
      types: TRANSFER_WITH_AUTHORIZATION_TYPES,
      primaryType: "TransferWithAuthorization",
      message: {
        from: this.#account.address,
        to: requireHexAddress(challenge.payTo),
        value: BigInt(value),
        validAfter: BigInt(validAfter),
        validBefore: BigInt(validBefore),
        nonce: nonceHex,
      },
    });

    const payload = {
      authorization: {
        from: this.#account.address,
        to: challenge.payTo,
        value: String(value),
        validAfter: String(validAfter),
        validBefore: String(validBefore),
        nonce: nonceHex,
      },
      signature,
    };
    return encodePaymentSignature(challenge.acceptedRaw, payload);
  }
}
