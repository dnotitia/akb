import assert from "node:assert/strict";
import test from "node:test";

import { AkbError, createClient, unwrapAkbResponse } from "../src/index.js";
import { createClient as createLiteClient } from "../src/lite.js";

test("unwrapAkbResponse keeps successful kind envelope as data", () => {
  const body = {
    kind: "table_query",
    columns: ["id"],
    items: [{ id: "row-1" }],
    total: 1,
  };

  const result = unwrapAkbResponse({ ok: true, status: 200, statusText: "OK" }, body);

  assert.equal(result.data, body);
  assert.equal(result.error, null);
  assert.equal(result.throwOnError().data, body);
});

test("unwrapAkbResponse maps HTTP errors to AkbError", () => {
  const result = unwrapAkbResponse(
    { ok: false, status: 403, statusText: "Forbidden" },
    {
      message: "permission denied for table incidents",
      code: "permission_denied",
      details: { pg_sqlstate: "42501" },
      hint: "Check vault membership.",
    },
  );

  assert.equal(result.data, null);
  assert.ok(result.error instanceof AkbError);
  assert.equal(result.error.message, "permission denied for table incidents");
  assert.equal(result.error.code, "permission_denied");
  assert.deepEqual(result.error.details, { pg_sqlstate: "42501" });
  assert.equal(result.error.hint, "Check vault membership.");
  assert.throws(() => result.throwOnError(), AkbError);
});

test("createClient sends bearer auth and JSON body through the boundary", async () => {
  const fixtureValue = ["fixture", "value"].join("-");
  let seenUrl = "";
  let seenHeaders = {};

  const client = createClient({
    baseUrl: "https://akb.test/api/v1/",
    token: () => fixtureValue,
    fetch: async (input, init) => {
      seenUrl = String(input);
      seenHeaders = Object.fromEntries(new Headers(init?.headers));
      return new Response(
        JSON.stringify({ kind: "table_sql", vaults: ["reef"], result: "INSERT 0 1" }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    },
  });

  const result = await client.request("/tables/reef/sql", {
    method: "POST",
    body: JSON.stringify({ sql: "INSERT INTO incidents (id) VALUES ('i1')" }),
  });

  assert.equal(seenUrl, "https://akb.test/api/v1/tables/reef/sql");
  assert.equal(seenHeaders.authorization, `Bearer ${fixtureValue}`);
  assert.equal(seenHeaders["content-type"], "application/json");
  assert.equal(result.error, null);
  assert.equal(result.data.kind, "table_sql");
});

test("createClient supports vault scoping, actingAs claims, and namespace stubs", async () => {
  let seenUrl = "";
  let seenHeaders = {};

  const client = createClient("https://akb.test/api/v1/", {
    apiKey: "service-key",
    fetch: async (input, init) => {
      seenUrl = String(input);
      seenHeaders = Object.fromEntries(new Headers(init?.headers));
      return new Response(JSON.stringify({ kind: "table_sql", result: "SELECT 1" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });

  const scoped = client
    .vault("reef")
    .actingAs({ sub: "end-user-1", app_metadata: { org_id: "org-1", role: "member" } });

  const result = await scoped.docs.request("/reef/doc/readme.md");

  assert.equal(seenUrl, "https://akb.test/api/v1/documents/reef/doc/readme.md");
  assert.equal(seenHeaders.authorization, "Bearer service-key");
  assert.deepEqual(JSON.parse(seenHeaders["x-akb-claims"]), {
    sub: "end-user-1",
    app_metadata: { org_id: "org-1", role: "member" },
  });
  assert.equal(scoped.from("incidents").vault, "reef");
  assert.equal(result.error, null);
});

test("actingAs rejects claims that cannot satisfy the BFF parser", () => {
  const client = createClient("https://akb.test/api/v1", { apiKey: "service-key" });

  assert.throws(
    () => client.actingAs({ sub: "end-user-1", app_metadata: {} }),
    /app_metadata\.org_id/,
  );
  assert.throws(
    () => client.actingAs({ sub: "end-user-1", app_metadata: { org_id: "org-1" } }),
    /app_metadata\.role/,
  );
});

test("lite subpath exposes the client boundary without a separate runtime", () => {
  assert.equal(createLiteClient, createClient);
});

test("createClient rejects cross-origin absolute URLs before adding credentials", async () => {
  const fixtureValue = ["fixture", "value"].join("-");
  let called = false;
  const client = createClient({
    baseUrl: "https://akb.test/api/v1",
    token: () => fixtureValue,
    fetch: async () => {
      called = true;
      return new Response("{}", { status: 200 });
    },
  });

  await assert.rejects(
    () => client.request("https://storage.example.com/presigned"),
    /different origin/,
  );
  assert.equal(called, false);
});
