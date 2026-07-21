/**
 * TillaClient tests — every method against a hand-rolled fetch mock (zero network,
 * zero funds, zero real keys). Includes the x402 funds-safety matrix: pin/cap
 * mismatches refuse BEFORE the signer is reached, and a transport failure after
 * signing raises SettlementUnknown with exactly one signed replay and no re-sign.
 */

import { describe, expect, it } from "vitest";
import {
  PaymentRefused,
  SettlementUnknown,
  TillaClient,
  TillaError,
  type PaymentChallenge,
  type PaymentSigner,
} from "../src/index.js";

const BASE = "https://tilla.test";
const USDT0 = "0x779ded0c9e1022225f8e0630b35a9b54be713736";
const PAY_TO = `0x${"22".repeat(20)}`;

const PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED";
const PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE";
const PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE";

// A real x402-encoded settle response (see codec test) — tx = 0xabab...
const SETTLE_FIXTURE =
  "eyJzdWNjZXNzIjp0cnVlLCJwYXllciI6IjB4MTExMTExMTExMTExMTExMTExMTExMTExMTExMTEx" +
  "MTExMTExMTExMSIsInRyYW5zYWN0aW9uIjoiMHhhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFi" +
  "YWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiIiwibmV0d29yayI6ImVpcDE1NTox" +
  "OTYifQ==";
const SETTLE_TX = `0x${"ab".repeat(32)}`;

function b64(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj), "utf-8").toString("base64");
}

function accept(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    scheme: "exact",
    network: "eip155:196",
    asset: USDT0,
    amount: "1000000",
    payTo: PAY_TO,
    maxTimeoutSeconds: 300,
    extra: { name: "USD₮0", version: "1" },
    ...over,
  };
}

function required(...accepts: unknown[]): string {
  return b64({ x402Version: 2, accepts });
}

class FakeSigner implements PaymentSigner {
  readonly calls: PaymentChallenge[] = [];
  sign(challenge: PaymentChallenge): string {
    this.calls.push(challenge);
    return "FAKE-SIGNATURE-HEADER";
  }
}

type Handler = (req: {
  url: string;
  method: string;
  headers: Headers;
}) => Response | never;

/** A fetch mock: routes each call through `handler`, records the call count. */
function mockFetch(handler: Handler): {
  fetch: typeof fetch;
  calls: () => number;
} {
  let count = 0;
  const fn = (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    count += 1;
    const url = typeof input === "string" ? input : input.toString();
    const method = (init?.method ?? "GET").toUpperCase();
    const headers = new Headers(init?.headers);
    return Promise.resolve(handler({ url, method, headers }));
  };
  return { fetch: fn as unknown as typeof fetch, calls: () => count };
}

function json(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function client(fetchImpl: typeof fetch): TillaClient {
  return new TillaClient({ baseUrl: BASE, timeoutMs: 2000, fetch: fetchImpl });
}

// ============================================================ read surface
describe("read surface", () => {
  it("parses discovery", async () => {
    const m = mockFetch(() =>
      json(200, {
        service: "tilla",
        total: 2,
        resources: [
          {
            slug: "alpha",
            name: "Alpha",
            description: "d",
            url: "/s/alpha/",
            feed: "/s/alpha/feed.json",
            buy: "/s/alpha/buy",
            mcp: "/s/alpha/mcp",
            price_min_micro: 1000000,
            price_max_micro: 9000000,
            currency: "USDT",
            network: "eip155:196",
            sold_count: 3,
          },
        ],
      }),
    );
    const d = await client(m.fetch).discovery(10, 0);
    expect(d.total).toBe(2);
    expect(d.resources[0]?.slug).toBe("alpha");
    expect(d.resources[0]?.soldCount).toBe(3);
  });

  it("parses search", async () => {
    const m = mockFetch(() =>
      json(200, {
        resources: [{ slug: "beta", name: "Beta", buy: "/s/beta/buy" }],
      }),
    );
    const rows = await client(m.fetch).search("beta");
    expect(rows).toHaveLength(1);
    expect(rows[0]?.slug).toBe("beta");
  });

  it("parses a feed with products", async () => {
    const m = mockFetch(() =>
      json(200, {
        store: {
          slug: "alpha",
          name: "Alpha",
          description: "d",
          url: `${BASE}/s/alpha/`,
        },
        products: [
          {
            id: "7",
            title: "Thing",
            description: "a thing",
            link: `${BASE}/s/alpha/`,
            price: { amount: "9.00", currency: "USDT" },
            availability: "in_stock",
            x402: {
              endpoint: "/s/alpha/buy",
              network: "eip155:196",
              asset: USDT0,
              schemes: ["exact"],
            },
          },
        ],
      }),
    );
    const feed = await client(m.fetch).feed("alpha");
    expect(feed.name).toBe("Alpha");
    expect(feed.products[0]?.priceAmount).toBe("9.00");
    expect(feed.products[0]?.schemes).toEqual(["exact"]);
  });

  it("reads llms.txt and the agent card", async () => {
    const m = mockFetch((req) => {
      if (req.url.endsWith("/llms.txt"))
        return new Response("# Alpha\n", { status: 200 });
      return json(200, { name: "Tilla" });
    });
    const c = client(m.fetch);
    expect((await c.llmsTxt("alpha")).startsWith("# Alpha")).toBe(true);
    expect((await c.agentCard()).name).toBe("Tilla");
  });
});

// ============================================================ human checkout
describe("human checkout", () => {
  it("creates a checkout and reads status", async () => {
    const m = mockFetch((req) => {
      if (req.method === "POST") {
        return json(200, {
          id: "cid1",
          pay_to: PAY_TO,
          amount_micro: 9000000,
          expires_at: "2026-01-01T00:10:00Z",
          network: "X Layer (chainId 196)",
          token: "USDT",
        });
      }
      return json(200, {
        id: "cid1",
        status: "created",
        amount_micro: 9000000,
        pay_to: PAY_TO,
      });
    });
    const c = client(m.fetch);
    const chk = await c.createCheckout("alpha", `0x${"9".repeat(40)}`);
    expect(chk.id).toBe("cid1");
    expect(chk.amountMicro).toBe(9000000);
    const st = await c.checkoutStatus("cid1");
    expect(st.status).toBe("created");
  });

  it("polls wait_for_paid until paid", async () => {
    const responses = [
      json(200, {
        id: "c",
        status: "created",
        amount_micro: 1,
        pay_to: PAY_TO,
      }),
      json(200, {
        id: "c",
        status: "paid",
        amount_micro: 1,
        pay_to: PAY_TO,
        tx_hash: SETTLE_TX,
      }),
    ];
    let i = 0;
    const m = mockFetch(() => responses[i++] as Response);
    const st = await client(m.fetch).waitForPaid("c", 5000, 10);
    expect(st.status).toBe("paid");
    expect(st.txHash).toBe(SETTLE_TX);
  });

  it("raises on expiry", async () => {
    const { CheckoutExpired } = await import("../src/index.js");
    const m = mockFetch(() =>
      json(200, {
        id: "c",
        status: "expired",
        amount_micro: 1,
        pay_to: PAY_TO,
      }),
    );
    await expect(
      client(m.fetch).waitForPaid("c", 1000, 10),
    ).rejects.toBeInstanceOf(CheckoutExpired);
  });
});

// ============================================================ MCP
describe("MCP", () => {
  it("returns structuredContent", async () => {
    const m = mockFetch(() =>
      json(200, {
        jsonrpc: "2.0",
        id: 1,
        result: {
          content: [{ type: "text", text: "{}" }],
          structuredContent: { products: [{ id: "1" }] },
        },
      }),
    );
    const out = await client(m.fetch).mcpCall("alpha", "list_products", {});
    expect(out).toEqual({ products: [{ id: "1" }] });
  });

  it("raises on a JSON-RPC error", async () => {
    const m = mockFetch(() =>
      json(200, {
        jsonrpc: "2.0",
        id: 1,
        error: { code: -32602, message: "bad" },
      }),
    );
    await expect(
      client(m.fetch).mcpCall("alpha", "nope", {}),
    ).rejects.toBeInstanceOf(TillaError);
  });
});

// ============================================================ x402 buy (happy)
describe("x402 buy — happy path", () => {
  it("signs once and returns the purchase", async () => {
    const m = mockFetch((req) => {
      if (req.headers.has(PAYMENT_SIGNATURE_HEADER)) {
        return json(
          200,
          {
            order_id: "o1",
            amount_micro: 1000000,
            delivery: "here is your file",
            download_url: `${BASE}/api/download/tok`,
          },
          { [PAYMENT_RESPONSE_HEADER]: SETTLE_FIXTURE },
        );
      }
      return new Response(null, {
        status: 402,
        headers: { [PAYMENT_REQUIRED_HEADER]: required(accept()) },
      });
    });
    const signer = new FakeSigner();
    const purchase = await client(m.fetch).buy("alpha", {
      signer,
      maxAmountMicro: 2000000,
    });
    expect(purchase.orderId).toBe("o1");
    expect(purchase.settleTx).toBe(SETTLE_TX);
    expect(purchase.downloadUrl?.endsWith("/tok")).toBe(true);
    expect(m.calls()).toBe(2); // probe + one signed replay
    expect(signer.calls).toHaveLength(1);
    // The signer saw the FULL challenge, including payTo (the merchant wallet).
    expect(signer.calls[0]?.payTo).toBe(PAY_TO);
    expect(signer.calls[0]?.amountMicro).toBe(1000000);
  });

  it("creates a store over x402", async () => {
    const m = mockFetch((req) => {
      if (req.headers.has(PAYMENT_SIGNATURE_HEADER)) {
        return json(
          200,
          {
            slug: "new-store",
            url: `${BASE}/s/new-store/`,
            manage_key: "mk-secret",
            store_name: "New Store",
          },
          { [PAYMENT_RESPONSE_HEADER]: SETTLE_FIXTURE },
        );
      }
      return new Response(null, {
        status: 402,
        headers: { [PAYMENT_REQUIRED_HEADER]: required(accept()) },
      });
    });
    const signer = new FakeSigner();
    const created = await client(m.fetch).createStore("I sell socks", {
      signer,
      maxAmountMicro: 1000000,
    });
    expect(created.slug).toBe("new-store");
    expect(created.manageKey).toBe("mk-secret");
    expect(created.settleTx).toBe(SETTLE_TX);
    expect(signer.calls).toHaveLength(1);
  });
});

// ============================================================ x402 funds-safety
describe("x402 funds-safety", () => {
  const cases: Array<{
    name: string;
    accept: Record<string, unknown>;
    cap: number;
  }> = [
    {
      name: "wrong asset",
      accept: accept({ asset: `0x${"de".repeat(20)}` }),
      cap: 2000000,
    },
    {
      name: "unknown scheme",
      accept: accept({ scheme: "weird" }),
      cap: 2000000,
    },
    {
      name: "wrong network",
      accept: accept({ network: "eip155:1" }),
      cap: 2000000,
    },
    {
      name: "amount over cap",
      accept: accept({ amount: "9000000" }),
      cap: 1000000,
    },
  ];

  for (const tc of cases) {
    it(`refuses before signing: ${tc.name}`, async () => {
      const m = mockFetch(
        () =>
          new Response(null, {
            status: 402,
            headers: { [PAYMENT_REQUIRED_HEADER]: required(tc.accept) },
          }),
      );
      const signer = new FakeSigner();
      await expect(
        client(m.fetch).buy("alpha", { signer, maxAmountMicro: tc.cap }),
      ).rejects.toBeInstanceOf(PaymentRefused);
      expect(signer.calls).toEqual([]); // signer PROVABLY never called
      expect(m.calls()).toBe(1); // only the unpaid probe; no signed request
    });
  }

  it("createStore refuses before signing when pinPayTo mismatches the challenge", async () => {
    const m = mockFetch(
      () =>
        new Response(null, {
          status: 402,
          headers: { [PAYMENT_REQUIRED_HEADER]: required(accept({})) },
        }),
    );
    const signer = new FakeSigner();
    await expect(
      client(m.fetch).createStore("a coffee shop", {
        signer,
        maxAmountMicro: 2000000,
        pinPayTo: `0x${"ab".repeat(20)}`,
      }),
    ).rejects.toBeInstanceOf(PaymentRefused);
    expect(signer.calls).toEqual([]);
    expect(m.calls()).toBe(1);
  });

  it("refuses a 402 with no PAYMENT-REQUIRED header", async () => {
    const m = mockFetch(() => new Response(null, { status: 402 }));
    const signer = new FakeSigner();
    await expect(
      client(m.fetch).buy("alpha", { signer, maxAmountMicro: 2000000 }),
    ).rejects.toBeInstanceOf(PaymentRefused);
    expect(signer.calls).toEqual([]);
  });

  it("maps a transport failure after signing to SettlementUnknown, no re-fire", async () => {
    let n = 0;
    const m = mockFetch(() => {
      n += 1;
      if (n === 1) {
        return new Response(null, {
          status: 402,
          headers: { [PAYMENT_REQUIRED_HEADER]: required(accept()) },
        });
      }
      throw new TypeError("connection dropped after signing");
    });
    const signer = new FakeSigner();
    await expect(
      client(m.fetch).buy("alpha", { signer, maxAmountMicro: 2000000 }),
    ).rejects.toBeInstanceOf(SettlementUnknown);
    expect(signer.calls).toHaveLength(1); // signed exactly once
    expect(m.calls()).toBe(2); // probe + one paid attempt, never re-fired
  });

  it("maps a signer exception to PaymentRefused", async () => {
    const m = mockFetch(
      () =>
        new Response(null, {
          status: 402,
          headers: { [PAYMENT_REQUIRED_HEADER]: required(accept()) },
        }),
    );
    const boom: PaymentSigner = {
      sign() {
        throw new Error("policy vetoed");
      },
    };
    await expect(
      client(m.fetch).buy("alpha", { signer: boom, maxAmountMicro: 2000000 }),
    ).rejects.toBeInstanceOf(PaymentRefused);
  });
});
