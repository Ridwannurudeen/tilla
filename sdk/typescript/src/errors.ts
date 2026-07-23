/**
 * Exception hierarchy for the Tilla SDK.
 *
 * Every SDK-raised error derives from {@link TillaError}. The two payment errors
 * mirror the production-proven `warden_hire` payer invariants:
 *
 * - {@link PaymentRefused} — a pre-sign veto. The decoded x402 challenge failed a
 *   pin/cap check (wrong asset/network/scheme, or amount over the caller's cap), so
 *   the signer hook was **never** invoked and no authorization was ever produced.
 * - {@link SettlementUnknown} — a transport failure AFTER the authorization was
 *   signed and the paid request was fired exactly once. The SDK does **not** retry a
 *   signed authorization, so the outcome on-chain is unknown; the caller must
 *   reconcile out-of-band (e.g. check the store / order) rather than re-pay.
 */

import type { PaymentChallenge } from "./x402Codec.js";

/** Base class for every error the SDK raises. */
export class TillaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TillaError";
  }
}

/**
 * A pin/cap check on the x402 challenge failed BEFORE signing.
 *
 * The signer hook was never called and no funds can move. `challenge` is the
 * decoded challenge that was rejected (`undefined` when the 402 was malformed).
 */
export class PaymentRefused extends TillaError {
  readonly challenge: PaymentChallenge | undefined;

  constructor(message: string, challenge?: PaymentChallenge) {
    super(message);
    this.name = "PaymentRefused";
    this.challenge = challenge;
  }
}

/**
 * The paid request failed at the transport layer AFTER signing.
 *
 * Exactly one signed authorization was sent and it is never re-sent, so whether
 * settlement landed on-chain is unknown. Reconcile out-of-band; do not re-pay.
 */
export class SettlementUnknown extends TillaError {
  constructor(message: string) {
    super(message);
    this.name = "SettlementUnknown";
  }
}

/** A human checkout order reached its expiry before it was paid. */
export class CheckoutExpired extends TillaError {
  constructor(message: string) {
    super(message);
    this.name = "CheckoutExpired";
  }
}
