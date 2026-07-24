// Subscription sidecar prototype (x402 "period" scheme).
//
// Two routes:
//   POST /subscriptions/challenge  -> builds a real 402 subscription challenge
//                                     with the OKX SDK's server-side scheme.
//                                     NO facilitator call, NO OKX creds.
//   POST /subscriptions/verify     -> decodes a PAYMENT-SIGNATURE header, runs
//                                     the SDK's local terms-bind check, and
//                                     prints the exact body the facilitator
//                                     subscribe call WOULD send (dry-run).
//
// The facilitator client is a stub whose network methods throw, so this file
// can never accidentally reach out to web3.okx.com.

const express = require("express");
const {
  PermitSubscriptionScheme,
} = require("@okxweb3/app-x402-evm/subscription");
const {
  PERMIT2_ADDRESS,
  SUBSCRIPTION_CONTRACT_ADDRESS,
} = require("@okxweb3/app-x402-evm");
const {
  InMemoryStore,
  decodePaymentPayload,
  asSubscriptionPaymentInner,
  parseChainIdFromNetwork,
  buildPermit2TypedData,
  computePermitSingleStructHash,
  buildSubscriptionTermsTypedData,
  encodePaymentPayload,
  buildCancelAuthTypedData,
} = require("@okxweb3/app-x402-core/subscription");
const { keccak256, stringToHex } = require("viem");
const crypto = require("crypto");

// The on-chain planId is bytes32 = keccak256(utf8(plan.id)) — the hash of the seller's
// business plan string (extra.plan.id, e.g. "pro-monthly"). It rides in `terms.planId`
// on the wire but is NOT part of the signed 17-field terms digest; the facilitator
// cross-checks the hash. The published SDK omits it, so we inject it (see buildWriteBody).
function planIdHash(id) {
  return keccak256(stringToHex(String(id ?? "default")));
}
// Real facilitator client — used ONLY by the creds-gated /health/creds and
// /subscriptions/settle routes below. The challenge/verify routes keep the stub.
const { OKXFacilitatorClient } = require("@okxweb3/app-x402-core");

// ── Config (all env-driven; no secrets required for the dry-run routes) ──────
const NETWORK = process.env.SIDECAR_NETWORK || "eip155:196"; // X Layer mainnet
const ASSET =
  process.env.SIDECAR_ASSET || "0x779ded0c9e1022225f8e0630b35a9b54be713736"; // USDT0, 6dp
const RESOURCE =
  process.env.SIDECAR_RESOURCE || "https://tilla.gudman.xyz/x402/subscription";
// The facilitator's on-chain EOA. In production this is read from the OKX
// /supported response; here it is stubbed from env so we never call out.
const FACILITATOR_ADDRESS =
  process.env.SIDECAR_FACILITATOR_ADDRESS ||
  "0x0000000000000000000000000000000000000000";
const SUBSCRIPTION_CONTRACT =
  process.env.SIDECAR_SUBSCRIPTION_CONTRACT || SUBSCRIPTION_CONTRACT_ADDRESS;
const PERMIT2_CONTRACT =
  process.env.SIDECAR_PERMIT2_CONTRACT || PERMIT2_ADDRESS;
const OKX_FACILITATOR_BASE_URL =
  process.env.OKX_FACILITATOR_BASE_URL || "https://web3.okx.com";

// ── Stub facilitator: satisfies SubscriptionFacilitatorClient at construction
//    time but refuses every network method. The challenge/verify routes never
//    invoke it — if they ever do, this throws loudly instead of paying/calling.
const stubFacilitator = new Proxy(
  {},
  {
    get(_t, prop) {
      return async () => {
        throw new Error(
          `stub facilitator: refused to call '${String(prop)}' — this prototype never contacts the facilitator`,
        );
      };
    },
  },
);

const scheme = new PermitSubscriptionScheme({
  facilitator: stubFacilitator,
  network: NETWORK,
  store: new InMemoryStore(),
});

// The "supported kind" is normally the cached OKX /supported response. We
// synthesize it from config so enhancePaymentRequirements can inject the
// contracts + EIP-712 domain without any network round-trip.
function supportedKind() {
  return {
    x402Version: 2,
    scheme: "period",
    network: NETWORK,
    extra: {
      facilitatorAddress: FACILITATOR_ADDRESS,
      subscriptionContract: SUBSCRIPTION_CONTRACT,
      permit2Contract: PERMIT2_CONTRACT,
    },
  };
}

const app = express();
app.use(express.json());

// POST /subscriptions/challenge
// Body: { payTo, amount, period, maxPeriods?, plan?, resource?, description? }
//   amount  = atomic units per period as a decimal string (USDT0 = 6dp, so
//             "1000000" == 1 USDT0)
//   period  = seconds per billing period (e.g. 2592000 for ~30 days)
app.post("/subscriptions/challenge", async (req, res) => {
  const { payTo, amount, period, maxPeriods, plan, resource, description } =
    req.body || {};
  if (!payTo || !amount || !period) {
    return res
      .status(400)
      .json({ error: "payTo, amount and period are required" });
  }

  const baseRequirements = {
    scheme: "period",
    network: NETWORK,
    maxAmountRequired: String(amount),
    resource: resource || RESOURCE,
    description: description || "Subscription access",
    mimeType: "application/json",
    payTo,
    maxTimeoutSeconds: 60,
    asset: ASSET,
    extra: {
      amountPerPeriod: String(amount),
      periodSec: Number(period),
      periodMode: 0, // 0 = fixed_seconds
      maxPeriods: Number(maxPeriods ?? 0), // 0 = open-ended
      plan: plan || { id: "default", tier: 1, name: "Default plan" },
    },
  };

  // Seller-side SDK call: injects contracts, facilitator address and the
  // A2APaySubscription EIP-712 domain. Pure/local — no facilitator call.
  const enhanced = await scheme.enhancePaymentRequirements(
    baseRequirements,
    supportedKind(),
    [],
  );

  const paymentRequired = {
    x402Version: 2,
    accepts: [enhanced],
    error: "X-PAYMENT header required (subscription)",
  };

  // Faithful transport: the buyer SDK reads accepts from this base64 header.
  const headerB64 = Buffer.from(
    JSON.stringify(paymentRequired.accepts),
  ).toString("base64");
  res.setHeader("APP-PAYMENT-REQUIRED", headerB64);
  return res.status(402).json(paymentRequired);
});

// POST /subscriptions/verify
// Header: PAYMENT-SIGNATURE (base64 JSON, buyer's signed payload)
// Body (optional): { requirements }  -> the accepts[0] echoed back so we can
//   run the SDK's LOCAL terms-bind check (verifySubscribe). No facilitator call.
app.post("/subscriptions/verify", async (req, res) => {
  const header = req.get("PAYMENT-SIGNATURE");
  if (!header) {
    return res.status(400).json({ error: "PAYMENT-SIGNATURE header required" });
  }

  let payload;
  let inner;
  try {
    payload = decodePaymentPayload(header);
    inner = asSubscriptionPaymentInner(payload);
  } catch (e) {
    return res
      .status(400)
      .json({ error: `could not decode PAYMENT-SIGNATURE: ${e.message}` });
  }

  // Requirements come from the request body if echoed back; otherwise fall
  // back to the `accepted` requirements the buyer embedded in the payload.
  const requirements = (req.body && req.body.requirements) || payload.accepted;
  const network = requirements && requirements.network;
  if (!network) {
    return res.status(400).json({
      error:
        "could not resolve network (no requirements and no payload.accepted)",
    });
  }

  // Optional local verify (pure crypto/param check, no network).
  let localVerify = {
    skipped: "pass `requirements` in body to run scheme.verifySubscribe",
  };
  if (req.body && req.body.requirements) {
    localVerify = await scheme.verifySubscribe(payload, req.body.requirements);
  }

  // DRY-RUN: exactly the body OKXFacilitatorClient.buildWriteBody would POST to
  // /api/v6/pay/x402/subscriptions. We build and print it; we do NOT send it.
  const dryRun = {
    method: "POST",
    url: `${OKX_FACILITATOR_BASE_URL}/api/v6/pay/x402/subscriptions`,
    authHeadersRequired: [
      "OK-ACCESS-KEY",
      "OK-ACCESS-SIGN",
      "OK-ACCESS-TIMESTAMP",
      "OK-ACCESS-PASSPHRASE",
    ],
    body: {
      chainIndex: parseChainIdFromNetwork(network),
      terms: inner.terms,
      permit: inner.permitSingle,
      termsSig: inner.termsSignature,
      permitSig: inner.permitSingleSignature,
      syncSettle: true,
    },
  };

  console.log(
    "[/subscriptions/verify] decoded network:",
    network,
    "payer:",
    inner.terms.payer,
  );
  console.log(
    "[/subscriptions/verify] local verifySubscribe:",
    JSON.stringify(localVerify),
  );
  console.log(
    "[/subscriptions/verify] facilitator subscribe body it WOULD send:",
  );
  console.log(JSON.stringify(dryRun, null, 2));

  return res.json({
    decoded: {
      scheme: "period",
      network,
      payer: inner.terms.payer,
    },
    localVerify,
    facilitatorDryRun: dryRun,
    note: "dry-run only: no request was sent to the facilitator",
  });
});

// ── Real facilitator, built ONLY when OKX creds are present in the env. Absent
//    creds -> null, and both routes below hard-refuse (503) so the committed
//    default never contacts web3.okx.com. Env names match the Python side.
function realFacilitator() {
  const apiKey = process.env.OKX_API_KEY;
  const secretKey = process.env.OKX_SECRET_KEY;
  const passphrase = process.env.OKX_PASSPHRASE;
  if (!apiKey || !secretKey || !passphrase) return null;
  const client = new OKXFacilitatorClient({
    apiKey,
    secretKey,
    passphrase,
    baseUrl: OKX_FACILITATOR_BASE_URL,
  });
  // Version gap: app-x402-core@0.2.1 omits planId from the subscribe body, but the
  // live X Layer facilitator requires it as a bytes32 ("invalid_bytes32: planId"
  // otherwise). Inject it from the requirements' (already-bytes32) plan id.
  const origBuildWriteBody = client.buildWriteBody.bind(client);
  client.buildWriteBody = (payload, requirements, syncSettle) => {
    const body = origBuildWriteBody(payload, requirements, syncSettle);
    // app-x402@0.2.x omits planId; the live facilitator requires terms.planId =
    // keccak256(utf8(plan.id)). It is not in the signed digest, so adding it here does
    // not affect the buyer's signature.
    const rawPlanId =
      requirements && requirements.extra && requirements.extra.plan
        ? requirements.extra.plan.id
        : undefined;
    if (rawPlanId && body.terms && body.terms.planId === undefined) {
      body.terms.planId = planIdHash(rawPlanId);
    }
    return body;
  };
  return client;
}

// GET /health — liveness only, for scripts/watchdog.sh. No creds, no facilitator
// round-trip, no secrets in the body. /health/creds is NOT a liveness probe: it calls
// out to web3.okx.com and 503s whenever creds are absent.
app.get("/health", (req, res) => res.json({ ok: true }));

// GET /health/creds — read-only creds probe (the orchestrator's JS-side check).
// No creds -> 503 (never contacts the facilitator). With creds it calls
// getSupported() (reports which schemes /supported lists, incl. whether `period`
// is present) and, as a second signal, a read-only subscription detail lookup
// with a syntactically valid but nonexistent id: a structured not-found means the
// creds authenticate against the subscriptions family (funds-gated only), while a
// 401 means creds-gated. Nothing here can move funds.
app.get("/health/creds", async (req, res) => {
  const fac = realFacilitator();
  if (!fac) {
    return res
      .status(503)
      .json({ configured: false, reason: "OKX creds not set in env" });
  }
  const out = { configured: true };
  try {
    const supported = await fac.getSupported();
    out.supported = supported;
    out.periodSupported = JSON.stringify(supported).includes('"period"');
  } catch (e) {
    out.supportedError = String(e && e.message ? e.message : e);
  }
  try {
    await fac.getSubscription("sub_nonexistent_probe_0000000000000000");
    out.subscriptionLookup = "ok";
  } catch (e) {
    out.subscriptionLookup = String(e && e.message ? e.message : e);
  }
  return res.json(out);
});

// POST /subscriptions/settle — the ONLY route that contacts the facilitator.
// Header PAYMENT-SIGNATURE + body { requirements }. No creds -> 503 (the committed
// default: this prototype never reaches web3.okx.com without creds). With creds it
// swaps the stub for a real OKXFacilitatorClient and POSTs the subscribe
// (syncSettle=true). Local terms-bind check runs first; a facilitator failure ->
// 502; only a facilitator success returns { settled: true, facilitator }.
app.post("/subscriptions/settle", async (req, res) => {
  const fac = realFacilitator();
  if (!fac) {
    return res.status(503).json({
      error: "OKX creds not set; refusing to contact the facilitator",
    });
  }
  const header = req.get("PAYMENT-SIGNATURE");
  if (!header) {
    return res.status(400).json({ error: "PAYMENT-SIGNATURE header required" });
  }
  const requirements = req.body && req.body.requirements;
  if (!requirements) {
    return res.status(400).json({ error: "requirements required in body" });
  }
  let payload;
  try {
    payload = decodePaymentPayload(header);
  } catch (e) {
    return res
      .status(400)
      .json({ error: `could not decode PAYMENT-SIGNATURE: ${e.message}` });
  }
  let localVerify;
  try {
    localVerify = await scheme.verifySubscribe(payload, requirements);
  } catch (e) {
    return res.status(400).json({ error: `local verify failed: ${e.message}` });
  }
  if (!localVerify || localVerify.ok === false) {
    return res
      .status(402)
      .json({ error: "local verify rejected the subscription", localVerify });
  }
  try {
    const result = await fac.subscribe(payload, requirements, true);
    // The OKX facilitator returns a 200 body even on failure (e.g. code 30001
    // "max_periods_invalid"), so a non-throwing call is NOT proof of settlement.
    // Only an OKX success code ("0") is settled; anything else is a rejection and
    // MUST NOT deliver (fail-closed — never a false settle).
    const code = result && (result.code ?? result.error_code);
    if (String(code) !== "0") {
      const detail =
        (result && (result.error_message || result.msg || result.detailMsg)) ||
        `code ${code}`;
      return res.status(402).json({
        settled: false,
        error: `facilitator rejected: ${detail}`,
        facilitator: result,
      });
    }
    return res.json({ settled: true, localVerify, facilitator: result });
  } catch (e) {
    return res
      .status(502)
      .json({ error: `facilitator subscribe failed: ${e.message}` });
  }
});

// POST /subscriptions/prepare — browser-signing helper (pure/local, no facilitator).
// A bundler-less store page cannot run the JS SDK's EIP-712 builders, so it asks the
// sidecar to build both typed-data envelopes (Permit2 + SubscriptionTerms) from the
// server-owned `selected` requirements. The browser signs each with eth_signTypedData_v4
// and echoes the messages back to /encode. Body: { selected, payer, nonce }.
app.post("/subscriptions/prepare", (req, res) => {
  const { selected, payer, nonce } = req.body || {};
  if (!selected || !payer) {
    return res.status(400).json({ error: "selected and payer are required" });
  }
  try {
    const now = Math.floor(Date.now() / 1000);
    const permitEnvelope = buildPermit2TypedData({
      selected,
      nonce: Number(nonce ?? 0),
      expiration: now + 365 * 24 * 3600,
      sigDeadline: String(now + 3600),
    });
    const permitSingle = permitEnvelope.message;
    const permitHash = computePermitSingleStructHash(permitSingle);
    const salt = "0x" + crypto.randomBytes(32).toString("hex");
    const termsEnvelope = buildSubscriptionTermsTypedData({
      selected,
      payer,
      startAt: 0, // 0 = use block.timestamp on-chain
      termsDeadline: now + 3600,
      salt,
      permitHash,
    });
    return res.json({
      permitTypedData: permitEnvelope,
      termsTypedData: termsEnvelope,
      permitSingle,
      terms: termsEnvelope.message,
    });
  } catch (e) {
    return res.status(400).json({ error: `prepare failed: ${e.message}` });
  }
});

// POST /subscriptions/encode — assemble the base64 PAYMENT-SIGNATURE header from the
// buyer's two signatures. Body: { selected, permitSingle, permitSingleSignature,
// terms, termsSignature }. Pure/local — no facilitator call.
app.post("/subscriptions/encode", (req, res) => {
  const {
    selected,
    permitSingle,
    permitSingleSignature,
    terms,
    termsSignature,
  } = req.body || {};
  if (
    !selected ||
    !permitSingle ||
    !permitSingleSignature ||
    !terms ||
    !termsSignature
  ) {
    return res.status(400).json({
      error:
        "selected, permitSingle, permitSingleSignature, terms and termsSignature are required",
    });
  }
  try {
    const paymentSignature = encodePaymentPayload({
      selected,
      permitSingle,
      permitSingleSignature,
      terms,
      termsSignature,
    });
    return res.json({ paymentSignature });
  } catch (e) {
    return res.status(400).json({ error: `encode failed: ${e.message}` });
  }
});

// The A2APaySubscription EIP-712 domain (same contract the terms bind to) — used to
// build the buyer's CancelAuth signature.
function subscriptionDomain() {
  return {
    name: "A2APaySubscription",
    version: "1",
    chainId: Number(NETWORK.split(":")[1]),
    verifyingContract: SUBSCRIPTION_CONTRACT,
  };
}

// POST /subscriptions/cancel-prepare — build the CancelAuth typed data the buyer signs
// to cancel a subscription. Body: { subId }. Pure/local. Returns the envelope + the
// unsigned cancelAuth message (the client adds `signature` and calls /cancel).
app.post("/subscriptions/cancel-prepare", (req, res) => {
  const { subId } = req.body || {};
  if (!subId) return res.status(400).json({ error: "subId is required" });
  try {
    const now = Math.floor(Date.now() / 1000);
    const nonce = "0x" + crypto.randomBytes(32).toString("hex");
    const deadline = now + 3600;
    const envelope = buildCancelAuthTypedData({
      domain: subscriptionDomain(),
      subId,
      initiator: "payer",
      nonce,
      deadline,
    });
    return res.json({
      cancelTypedData: envelope,
      cancelAuth: { action: 0, subId, initiator: 0, nonce, deadline },
    });
  } catch (e) {
    return res
      .status(400)
      .json({ error: `cancel-prepare failed: ${e.message}` });
  }
});

// POST /subscriptions/cancel — submit the buyer's signed CancelAuth to the facilitator.
// Body: { subId, cancelAuth } (cancelAuth = the cancel-prepare message + `signature`).
// Fail-closed on any non-"0" facilitator code (same contract as /settle).
app.post("/subscriptions/cancel", async (req, res) => {
  const fac = realFacilitator();
  if (!fac) {
    return res.status(503).json({ error: "OKX creds not set" });
  }
  const { subId, cancelAuth } = req.body || {};
  if (!subId || !cancelAuth || !cancelAuth.signature) {
    return res
      .status(400)
      .json({ error: "subId and a signed cancelAuth are required" });
  }
  try {
    const result = await fac.cancelSubscription(subId, cancelAuth, true);
    const code = result && (result.code ?? result.error_code);
    if (String(code) !== "0") {
      const detail =
        (result && (result.error_message || result.msg)) || `code ${code}`;
      return res.status(402).json({
        canceled: false,
        error: `facilitator rejected: ${detail}`,
        facilitator: result,
      });
    }
    return res.json({ canceled: true, facilitator: result });
  } catch (e) {
    return res
      .status(502)
      .json({ error: `facilitator cancel failed: ${e.message}` });
  }
});

const PORT = Number(process.env.PORT || 8790);
if (require.main === module) {
  app.listen(PORT, () =>
    console.log(
      `subscription sidecar listening on :${PORT} (network ${NETWORK})`,
    ),
  );
}

module.exports = { app, scheme, supportedKind };
