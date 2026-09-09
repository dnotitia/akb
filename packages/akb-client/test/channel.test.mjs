import assert from "node:assert/strict";
import { test } from "vitest";

import { AkbError, createClient } from "../dist/index.js";
import { createClient as createLiteClient } from "../dist/lite.js";

const textEncoder = new TextEncoder();

test("channel is vault-scoped and exposes the same auth boundary from main and lite", async () => {
  let fetchCalls = 0;
  const fetch = async () => {
    fetchCalls += 1;
    return new Response(null, { status: 204 });
  };
  const root = createClient({ baseUrl: "https://akb.test/api/v1", fetch });
  const lite = createLiteClient({ baseUrl: "https://akb.test/api/v1", fetch });

  assert.throws(() => root.channel(), /Select a vault/);
  assert.throws(() => lite.channel(), /Select a vault/);
  assert.equal(fetchCalls, 0);
  assert.equal(typeof root.vault("vault/one").channel, "function");
  assert.equal(typeof lite.vault("vault/one").channel, "function");
});

test("channel parses split SSE frames, filters listeners, advances through checkpoints, and unsubscribes", async () => {
  const calls = [];
  const tokens = ["token-one"];
  const order = [];
  const changes = [];
  const checkpoints = [];
  const fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    calls.push({ url, headers: Object.fromEntries(new Headers(init.headers)) });
    return sseResponse([
      ": heartbeat\r\n\r\n",
      "retry: 1\r\n\r\n",
      "event: checkpoint\r\nid: ec1.checkpoint\r\ndata: {\"version\":1,\"cursor\":\"ec1.checkpoint\"}\r\n\r\n",
      "event: change\r\nid: ec1.a\r\ndata: {\"version\":1,\"cursor\":\"ec1.a\",\"occurred_at\":\"2026-01-01T00:00:00Z\",\"vault\":\"vault/one\",\"kind\":\"document.put\",\"payload\":{\"id\":\"a\"}}\r\n\r\n",
      "event: change\r\nid: ec1.b\r\ndata: {\"version\":1,\"cursor\":\"ec1.b\",\"occurred_at\":\"2026-01-01T00:00:01Z\",\"vault\":\"vault/one\",\"kind\":\"table.rows_changed\",\"payload\":{\"id\":\"b\"}}\r\n\r\n",
      "event: change\r\nid: ec1.z\r\ndata: {\"version\":1,\"cursor\":\"ec1.z\",\"occurred_at\":\"2026-01-01T00:00:02Z\",\"vault\":\"vault/one\",\"kind\":\"document.put\",\"payload\":{\"id\":\"z\"}}\r\n\r\n",
    ], 200, init.signal, false);
  };
  const claims = { sub: "user-1", app_metadata: { org_id: "org-1", role: "reader" } };
  const client = createClient({
    baseUrl: "https://akb.test/api/v1",
    token: () => tokens.at(-1),
    fetch,
  }).vault("vault/one").actingAs(claims);

  const subscription = await client.channel()
    .on("change", { kinds: ["document.put", "table.rows_changed", "document.put"] }, async (event) => {
      order.push(`first:${event.kind}`);
      await Promise.resolve();
      changes.push(["first", event.kind]);
    })
    .on("change", { kinds: ["table.rows_changed"] }, (event) => {
      order.push(`second:${event.kind}`);
      changes.push(["second", event.kind]);
    })
    .on("checkpoint", (checkpoint) => {
      checkpoints.push(checkpoint.cursor);
    })
    .subscribe({ start: "earliest" });

  await waitFor(() => changes.length >= 4);
  assert.deepEqual(calls[0].url.pathname, "/api/v1/events/vault%2Fone");
  assert.deepEqual(calls[0].url.searchParams.getAll("kind"), ["document.put", "table.rows_changed"]);
  assert.equal(calls[0].url.searchParams.get("start"), "earliest");
  assert.equal(calls[0].headers.authorization, "Bearer token-one");
  assert.equal(calls[0].headers["x-akb-claims"], JSON.stringify(claims));
  assert.deepEqual(checkpoints, ["ec1.checkpoint"]);
  assert.deepEqual(order, ["first:document.put", "first:table.rows_changed", "second:table.rows_changed", "first:document.put"]);
  assert.equal(subscription.cursor, "ec1.z");

  await subscription.unsubscribe();
  await subscription.closed;
});

test("reconnects after a retryable handshake and re-evaluates the token provider", async () => {
  const calls = [];
  let tokenCalls = 0;
  let attempt = 0;
  const fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const headers = Object.fromEntries(new Headers(init.headers));
    calls.push({ url, headers });
    attempt += 1;
    if (attempt === 1) {
      return json({ message: "temporary", code: "temporary_failure" }, 503, "Unavailable");
    }
    return sseResponse([
      "event: checkpoint\nid: ec1.ready\ndata: {\"version\":1,\"cursor\":\"ec1.ready\"}\n\n",
    ]);
  };
  const client = createClient({
    baseUrl: "https://akb.test/api/v1",
    token: () => `token-${++tokenCalls}`,
    fetch,
  }).vault("retry vault");

  const subscription = await client.channel().subscribe({ cursor: "ec1.start" });
  await waitFor(() => subscription.cursor === "ec1.ready");
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url.searchParams.get("cursor"), "ec1.start");
  assert.equal(calls[0].headers.authorization, "Bearer token-1");
  assert.equal(calls[1].headers["last-event-id"], "ec1.start");
  assert.equal(calls[1].headers.authorization, "Bearer token-2");
  await subscription.unsubscribe();
});

test("listener failure rejects closed and preserves the last safe cursor", async () => {
  const original = new Error("handler failed");
  const fetch = async () => sseResponse([
    "event: checkpoint\nid: ec1.safe\ndata: {\"version\":1,\"cursor\":\"ec1.safe\"}\n\n",
    "event: change\nid: ec1.failed\ndata: {\"version\":1,\"cursor\":\"ec1.failed\",\"occurred_at\":\"2026-01-01T00:00:00Z\",\"vault\":\"vault\",\"kind\":\"document.put\",\"payload\":{}}\n\n",
  ]);
  const subscription = await createClient({
    baseUrl: "https://akb.test/api/v1",
    fetch,
  }).vault("vault").channel().on("change", () => {
    throw original;
  }).subscribe();

  await assert.rejects(subscription.closed, (error) => error === original);
  assert.equal(subscription.cursor, "ec1.safe");
});

test("event gap and invalid cursor remain AkbError responses", async () => {
  const fetch = async (_input, init = {}) => {
    assert.equal(init.headers instanceof Headers, true);
    return json({
      message: "Event cursor is outside the retained Vault tail",
      code: "event_gap",
      details: { earliest_cursor: "ec1.earliest", latest_cursor: "ec1.latest" },
    }, 410, "Gone");
  };
  const client = createClient({ baseUrl: "https://akb.test/api/v1", fetch }).vault("vault");
  await assert.rejects(
    client.channel().subscribe({ cursor: "ec1.old" }),
    (error) => error instanceof AkbError
      && error.code === "event_gap"
      && error.details.earliest_cursor === "ec1.earliest"
      && error.details.latest_cursor === "ec1.latest",
  );
});

function sseResponse(chunks, status = 200, signal = null, close = true) {
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(textEncoder.encode(chunk));
      if (close) {
        controller.close();
      } else {
        signal?.addEventListener("abort", () => {
          controller.error(new DOMException("aborted", "AbortError"));
        }, { once: true });
      }
    },
  });
  return new Response(body, {
    status,
    headers: { "content-type": "text/event-stream" },
  });
}

function json(body, status, statusText) {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { "content-type": "application/json" },
  });
}

async function waitFor(predicate) {
  const deadline = Date.now() + 2_000;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("timed out waiting for channel state");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}
