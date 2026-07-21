#!/usr/bin/env -S npx tsx
/**
 * Agent x402 buy — THIS MOVES REAL FUNDS. Never run in CI.
 *
 * You supply and control the key (TILLA_BUYER_KEY); the SDK enforces sign-once + an
 * amount cap and surfaces the full challenge (including the merchant pay-to) before
 * signing. A wrong asset/network/scheme, or an amount over your --max-usdt cap, is
 * refused BEFORE any signature is produced.
 *
 *   export TILLA_BUYER_KEY=0x<your funded X Layer key>
 *   npx tsx examples/agent_buy.ts --slug SLUG --max-usdt 1.0
 *
 * The script prints the decoded challenge and requires you to type "YES" to proceed.
 */

import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import {
  LocalEip3009Signer,
  PaymentRefused,
  SettlementUnknown,
  TillaClient,
  type PaymentChallenge,
  type PaymentSigner,
} from "../src/index.js";

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

/**
 * Wraps LocalEip3009Signer with an interactive confirmation that shows the real
 * pay-to + amount before a signature (real funds) is ever produced.
 */
class ConfirmingSigner implements PaymentSigner {
  constructor(private readonly inner: LocalEip3009Signer) {}

  async sign(challenge: PaymentChallenge): Promise<string> {
    console.log("\n--- x402 challenge (about to spend REAL USDT) ---");
    console.log(`  pay to : ${challenge.payTo}  (the merchant wallet)`);
    console.log(`  amount : ${challenge.amountMicro} micro-USDT`);
    console.log(`  asset  : ${challenge.asset}`);
    console.log(`  network: ${challenge.network}`);
    const rl = createInterface({ input: stdin, output: stdout });
    const answer = await rl.question(
      'Type "YES" to sign and spend, anything else to abort: ',
    );
    rl.close();
    if (answer.trim() !== "YES") {
      throw new Error("aborted by operator");
    }
    return this.inner.sign(challenge);
  }
}

async function main(): Promise<void> {
  const baseUrl = arg("base-url") ?? "https://tilla.gudman.xyz";
  const slug = arg("slug");
  const maxUsdt = arg("max-usdt");
  if (!slug || !maxUsdt) {
    console.error("usage: agent_buy.ts --slug SLUG --max-usdt 1.0");
    process.exit(2);
  }

  const key = process.env.TILLA_BUYER_KEY;
  if (!key) {
    console.error("set TILLA_BUYER_KEY to a funded X Layer key first");
    process.exit(2);
  }

  const maxAmountMicro = Math.round(Number(maxUsdt) * 1_000_000);
  const signer = new ConfirmingSigner(new LocalEip3009Signer(key));
  const client = new TillaClient({ baseUrl });

  try {
    const purchase = await client.buy(slug, { signer, maxAmountMicro });
    console.log("\n--- purchase complete ---");
    console.log(`  order id : ${purchase.orderId}`);
    console.log(`  settle tx: ${purchase.settleTx}`);
    console.log(`  delivery : ${purchase.delivery}`);
    if (purchase.downloadUrl)
      console.log(`  download : ${purchase.downloadUrl}`);
    if (purchase.licenseKey) console.log(`  license  : ${purchase.licenseKey}`);
  } catch (err) {
    if (err instanceof PaymentRefused) {
      console.error(`refused before signing (no funds moved): ${err.message}`);
      process.exit(1);
    }
    if (err instanceof SettlementUnknown) {
      console.error(
        `transport failed AFTER signing — do NOT re-run; reconcile the order/store ` +
          `first: ${err.message}`,
      );
      process.exit(1);
    }
    throw err;
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
