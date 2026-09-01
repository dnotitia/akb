import assert from "node:assert/strict";
import { test } from "vitest";

import { AkbError } from "../src/errors.js";
import {
  createControlPlaneAdminClient,
  createControlPlaneAppClient,
  exchangeAppCredential,
} from "../src/control-plane.js";

test("control-plane facade encodes paths, separates tokens, and preserves errors", async () => {
  const calls = [];
  const fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const headers = Object.fromEntries(new Headers(init.headers));
    const body = init.body === undefined ? undefined : JSON.parse(String(init.body));
    calls.push({ url, headers, body, method: init.method });
    if (url.pathname.endsWith("/denied")) {
      return new Response(JSON.stringify({ message: "denied", code: "permission_denied" }), {
        status: 403,
        statusText: "Forbidden",
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ job_id: "job-1", targets: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  const admin = createControlPlaneAdminClient({
    baseUrl: "https://control.invalid/api/v1/",
    adminToken: () => "admin-token",
    fetch,
  });
  const app = createControlPlaneAppClient({
    baseUrl: "https://control.invalid",
    appToken: "app-token",
    fetch,
  });

  await admin.apps.create({ app_key: "generic-app", metadata: { owner: "unit" } });
  await admin.rollouts.request(
    "app/id",
    { release_id: "release/id", manifest_checksum: "a".repeat(64) },
    "resume-key",
  );
  await app.inventory.list({ limit: 3, cursor: "cursor value" });
  await app.rollouts.resume(
    "rollout/id",
    { release_id: "release/id", manifest_checksum: "b".repeat(64) },
    "app-key",
  );
  await exchangeAppCredential({
    baseUrl: "https://control.invalid/api/v1",
    credential: "deployment-credential",
    fetch,
  });
  const denied = await admin.apps.get("denied");

  assert.equal(calls[0].url.pathname, "/api/v1/apps");
  assert.equal(calls[0].headers.authorization, "Bearer admin-token");
  assert.deepEqual(calls[0].body, { app_key: "generic-app", metadata: { owner: "unit" } });
  assert.equal(calls[1].url.pathname, "/api/v1/apps/app%2Fid/rollouts");
  assert.equal(calls[1].headers["idempotency-key"], "resume-key");
  assert.equal(calls[2].url.searchParams.get("cursor"), "cursor value");
  assert.equal(calls[2].headers.authorization, "Bearer app-token");
  assert.equal(calls[3].url.pathname, "/api/v1/app/rollouts/rollout%2Fid/resume");
  assert.equal(calls[3].headers["idempotency-key"], "app-key");
  assert.equal(calls[4].headers.authorization, undefined);
  assert.equal(denied.data, null);
  assert.ok(denied.error instanceof AkbError);
  assert.equal(denied.error.code, "permission_denied");
});

test("control-plane operations forward request-scoped AbortSignals", async () => {
  const signal = new AbortController().signal;
  const seen = [];
  const fetch = async (input, init = {}) => {
    seen.push({ url: String(input), signal: init.signal });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  const admin = createControlPlaneAdminClient({
    baseUrl: "https://control.invalid",
    adminToken: "admin-token",
    fetch,
  });
  const app = createControlPlaneAppClient({
    baseUrl: "https://control.invalid",
    appToken: "app-token",
    fetch,
  });

  await admin.apps.get("app-1", { signal });
  await admin.inventory.list("app-1", { limit: 3, signal });
  await app.inventory.list({ limit: 2, signal });
  await exchangeAppCredential({ baseUrl: "https://control.invalid", credential: "credential", signal, fetch });

  assert.equal(seen.length, 4);
  assert.ok(seen.every((call) => call.signal === signal));
  assert.equal(new URL(seen[1].url).searchParams.has("signal"), false);
  assert.equal(new URL(seen[2].url).searchParams.has("signal"), false);
});

test("control-plane injected fetch and response failures use the shared result boundary", async () => {
  const transport = createControlPlaneAdminClient({
    baseUrl: "https://control.invalid",
    adminToken: "admin-token",
    fetch: async () => {
      throw new Error("control transport details must stay private");
    },
  });
  const failedTransport = await transport.apps.get("app-1");
  assert.equal(failedTransport.error?.code, "transport_error");
  assert.equal(failedTransport.error?.status, null);
  assert.doesNotMatch(failedTransport.error?.message ?? "", /control transport details|private/);

  const malformed = createControlPlaneAdminClient({
    baseUrl: "https://control.invalid",
    adminToken: "admin-token",
    fetch: async () => new Response("{broken", {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  });
  const failedJson = await malformed.apps.get("app-1");
  assert.equal(failedJson.error?.code, "invalid_json");
  assert.equal(failedJson.error?.status, 200);
});

test("control-plane subpath does not expose data-plane constructors", async () => {
  const exports = await import("../src/control-plane.js");
  for (const forbidden of ["createClient", "AkbClient", "akbFetch", "unwrapAkbResponse"]) {
    assert.equal(forbidden in exports, false, forbidden);
  }
});
