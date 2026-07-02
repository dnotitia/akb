import assert from "node:assert/strict";
import test from "node:test";

import { AkbError, createClient, unwrapAkbResponse } from "../src/index.js";

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
