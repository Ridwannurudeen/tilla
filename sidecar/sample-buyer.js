// End-to-end demo driver. Starts the sidecar in-process, then:
//   1. POST /subscriptions/challenge -> gets the 402 subscription challenge.
//   2. Signs the Permit2 + SubscriptionTerms EIP-712 messages with a THROWAWAY
//      key (generated fresh each run — testnet/demo only, never funded).
//   3. Encodes the PAYMENT-SIGNATURE header via the SDK.
//   4. POST /subscriptions/verify -> shows the local check + facilitator dry-run.
//
// Nothing here touches a real network or the OKX facilitator.

const http = require("http");
const { generatePrivateKey, privateKeyToAccount } = require("viem/accounts");
const {
  buildPermit2TypedData,
  computePermitSingleStructHash,
  buildSubscriptionTermsTypedData,
  encodePaymentPayload,
} = require("@okxweb3/app-x402-core/subscription");
const { app } = require("./server");

function post(server, path, { headers = {}, body } = {}) {
  const { port } = server.address();
  const payload = body ? JSON.stringify(body) : undefined;
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: "127.0.0.1",
        port,
        path,
        method: "POST",
        headers: { "content-type": "application/json", ...headers },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () =>
          resolve({
            status: res.statusCode,
            json: data ? JSON.parse(data) : null,
          }),
        );
      },
    );
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
}

(async () => {
  const server = app.listen(0);
  await new Promise((r) => server.once("listening", r));

  // 1. Ask for a subscription challenge: 5 USDT0 / ~30 days.
  const challenge = await post(server, "/subscriptions/challenge", {
    body: {
      payTo: "0x1111111111111111111111111111111111111111",
      amount: "5000000", // 5 USDT0 (6dp)
      period: 2592000, // 30 days in seconds
      maxPeriods: 12,
      plan: { id: "pro-monthly", tier: 2, name: "Pro Monthly" },
    },
  });
  console.log("=== /subscriptions/challenge -> HTTP", challenge.status, "===");
  console.log(JSON.stringify(challenge.json, null, 2));

  const selected = challenge.json.accepts[0];

  // 2. Buyer signs with a throwaway key (fresh, unfunded — demo only).
  const account = privateKeyToAccount(generatePrivateKey());
  const now = Math.floor(Date.now() / 1000);

  const permitEnvelope = buildPermit2TypedData({
    selected,
    nonce: 0,
    expiration: now + 365 * 24 * 3600,
    sigDeadline: String(now + 3600),
  });
  const permitSingle = permitEnvelope.message;
  const permitSingleSignature = await account.signTypedData({
    domain: permitEnvelope.domain,
    types: permitEnvelope.types,
    primaryType: permitEnvelope.primaryType,
    message: permitEnvelope.message,
  });

  const permitHash = computePermitSingleStructHash(permitSingle);
  const salt = "0x" + "ab".repeat(32);

  const termsEnvelope = buildSubscriptionTermsTypedData({
    selected,
    payer: account.address,
    startAt: 0, // 0 = use block.timestamp on-chain
    termsDeadline: now + 3600,
    salt,
    permitHash,
  });
  const terms = termsEnvelope.message;
  const termsSignature = await account.signTypedData({
    domain: termsEnvelope.domain,
    types: termsEnvelope.types,
    primaryType: termsEnvelope.primaryType,
    message: termsEnvelope.message,
  });

  // 3. Encode PAYMENT-SIGNATURE header via the SDK.
  const paymentSignature = encodePaymentPayload({
    selected,
    permitSingle,
    permitSingleSignature,
    terms,
    termsSignature,
  });
  console.log("\n=== buyer signed with throwaway key", account.address, "===");
  console.log(
    "PAYMENT-SIGNATURE (",
    paymentSignature.length,
    "chars ):",
    paymentSignature.slice(0, 64) + "...",
  );

  // 4. Verify (local bind-check + facilitator dry-run).
  const verify = await post(server, "/subscriptions/verify", {
    headers: { "PAYMENT-SIGNATURE": paymentSignature },
    body: { requirements: selected },
  });
  console.log("\n=== /subscriptions/verify -> HTTP", verify.status, "===");
  console.log(JSON.stringify(verify.json, null, 2));

  server.close();
})().catch((e) => {
  console.error("demo failed:", e);
  process.exit(1);
});
