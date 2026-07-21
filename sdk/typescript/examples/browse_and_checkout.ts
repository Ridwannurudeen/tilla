#!/usr/bin/env -S npx tsx
/**
 * Browse Tilla and open a human checkout — ZERO funds, ZERO keys required.
 *
 * This talks only to read-only + checkout-create endpoints. It never signs
 * anything and never moves money: it prints the pay-to address and amount so a
 * HUMAN can choose to pay from their own wallet (or just walk away — the order
 * expires server-side).
 *
 *   npx tsx examples/browse_and_checkout.ts [--slug SLUG] [--base-url URL]
 *
 * With no --slug it discovers a store from the public index and uses the first one.
 */

import { TillaClient } from "../src/index.js";

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

async function main(): Promise<void> {
  const baseUrl = arg("base-url") ?? "https://tilla.gudman.xyz";
  const client = new TillaClient({ baseUrl });

  let slug = arg("slug");
  if (!slug) {
    const disc = await client.discovery(5);
    console.log(`discovered ${disc.total} live store(s):`);
    for (const r of disc.resources) {
      console.log(`  - ${r.slug}: ${r.name} (${r.soldCount} sold)`);
    }
    if (disc.resources.length === 0) {
      console.log("no live stores to browse; pass --slug once one exists.");
      return;
    }
    slug = disc.resources[0]!.slug;
  }

  const feed = await client.feed(slug);
  console.log(`\nstore: ${feed.name} — ${feed.description}`);
  for (const p of feed.products) {
    console.log(
      `  product ${p.id}: ${p.title} — ${p.priceAmount} ${p.currency}`,
    );
  }

  const checkout = await client.createCheckout(slug);
  console.log("\n--- human checkout opened (nothing paid yet) ---");
  console.log(`  order id : ${checkout.id}`);
  console.log(`  pay to   : ${checkout.payTo}`);
  console.log(`  amount   : ${checkout.amountMicro} micro-USDT (X Layer)`);
  console.log(`  expires  : ${checkout.expiresAt}`);
  console.log(
    "\nTo complete: send exactly that amount of USDT0 to the pay-to address from " +
      "your own wallet, then call client.submitTx(orderId, txHash) or " +
      "waitForPaid(orderId). To abandon: do nothing — it expires.",
  );

  const status = await client.checkoutStatus(checkout.id);
  console.log(`\ncurrent status: ${status.status}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
