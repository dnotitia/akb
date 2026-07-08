import assert from "node:assert/strict";
import { test } from "vitest";

import { AkbError, createClient, createTypedFetch, unwrapAkbResponse } from "../src/index.js";
import { createClient as createLiteClient } from "../src/lite.js";

// Computed at runtime so the fake key never appears as a string literal, which
// would trip the detect-secrets keyword scan (mirrors `fixtureValue` below).
const fixtureApiKey = ["service", "key"].join("-");

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
    apiKey: fixtureApiKey,
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

test("search facade scopes search, drill-down, grep, and forwards actingAs claims", async () => {
  const seen = [];
  const client = createClient("https://akb.test/api/v1/", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      seen.push({
        url: String(input),
        headers: Object.fromEntries(new Headers(init?.headers)),
      });
      const url = new URL(String(input));
      if (url.pathname.endsWith("/drill-down")) {
        return new Response(
          JSON.stringify({ kind: "drill_down", uri: url.searchParams.get("uri"), sections: [] }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.pathname.endsWith("/grep")) {
        return new Response(
          JSON.stringify({ kind: "grep", pattern: url.searchParams.get("q"), regex: true, files: [] }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({ kind: "search", query: url.searchParams.get("q"), total: 0, returned: 0, total_matches: 0, results: [] }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    },
  }).vault("reef").actingAs({ sub: "end-user-1", app_metadata: { org_id: "org-1", role: "member" } });

  const searchResult = await client.search("typed search", {
    rerank: false,
    tags: ["sdk", "search"],
    limit: 5,
    sourceUris: ["akb://reef/doc/readme.md"],
  });
  const drillResult = await client.search.drillDown("akb://reef/doc/readme.md", { section: "Intro" });
  const grepResult = await client.search.grep("needle", { regex: true, filesWithMatches: true });

  const searchUrl = new URL(seen[0].url);
  assert.equal(searchUrl.pathname, "/api/v1/search");
  assert.equal(searchUrl.searchParams.get("q"), "typed search");
  assert.deepEqual(searchUrl.searchParams.getAll("vault"), ["reef"]);
  assert.equal(searchUrl.searchParams.get("rerank"), "false");
  assert.deepEqual(searchUrl.searchParams.getAll("tags"), ["sdk", "search"]);
  assert.equal(searchUrl.searchParams.get("limit"), "5");
  assert.deepEqual(searchUrl.searchParams.getAll("source_uris"), ["akb://reef/doc/readme.md"]);
  assert.deepEqual(JSON.parse(seen[0].headers["x-akb-claims"]), {
    sub: "end-user-1",
    app_metadata: { org_id: "org-1", role: "member" },
  });
  assert.equal(searchResult.throwOnError().data.kind, "search");

  const drillUrl = new URL(seen[1].url);
  assert.equal(drillUrl.pathname, "/api/v1/drill-down");
  assert.equal(drillUrl.searchParams.get("uri"), "akb://reef/doc/readme.md");
  assert.equal(drillUrl.searchParams.get("section"), "Intro");
  assert.equal(drillResult.throwOnError().data.kind, "drill_down");

  const grepUrl = new URL(seen[2].url);
  assert.equal(grepUrl.pathname, "/api/v1/grep");
  assert.deepEqual(grepUrl.searchParams.getAll("vault"), ["reef"]);
  assert.equal(grepUrl.searchParams.get("q"), "needle");
  assert.equal(grepUrl.searchParams.get("regex"), "true");
  assert.equal(grepUrl.searchParams.get("files_with_matches"), "true");
  assert.equal(grepResult.throwOnError().data.kind, "grep");
});

test("actingAs rejects claims that cannot satisfy the BFF parser", () => {
  const client = createClient("https://akb.test/api/v1", { apiKey: fixtureApiKey });

  assert.throws(
    () => client.actingAs({ sub: "end-user-1", app_metadata: {} }),
    /app_metadata\.org_id/,
  );
  assert.throws(
    () => client.actingAs({ sub: "end-user-1", app_metadata: { org_id: "org-1" } }),
    /app_metadata\.role/,
  );
});

test("vault sql tag parameterizes interpolations and forwards actingAs claims", async () => {
  const severity = "high' OR TRUE --";
  const minScore = 3;
  let seenUrl = "";
  let seenHeaders = {};
  let seenBody = {};

  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      seenUrl = String(input);
      seenHeaders = Object.fromEntries(new Headers(init?.headers));
      seenBody = JSON.parse(String(init?.body));
      return new Response(
        JSON.stringify({ kind: "table_query", columns: ["title"], items: [{ title: "Ship" }], total: 1 }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    },
  }).vault("eng").actingAs({ sub: "end-user-1", app_metadata: { org_id: "org-1", role: "member" } });

  const result = await client.sql`SELECT title FROM incidents WHERE severity = ${severity} AND score >= ${minScore}`;

  assert.equal(seenUrl, "https://akb.test/api/v1/tables/eng/sql");
  assert.equal(seenHeaders.authorization, "Bearer service-key");
  assert.equal(seenHeaders["content-type"], "application/json");
  assert.deepEqual(JSON.parse(seenHeaders["x-akb-claims"]), {
    sub: "end-user-1",
    app_metadata: { org_id: "org-1", role: "member" },
  });
  assert.deepEqual(seenBody, {
    sql: "SELECT title FROM incidents WHERE severity = $1 AND score >= $2",
    params: [severity, minScore],
  });
  assert.equal(seenBody.sql.includes(severity), false);
  assert.equal(result.throwOnError().data.items[0].title, "Ship");
});

test("vault sql tag requires a selected vault and tagged template", () => {
  const client = createClient("https://akb.test/api/v1", { apiKey: fixtureApiKey });

  assert.throws(() => client.sql`SELECT 1`, /Select a vault/);
  assert.throws(
    () => client.vault("eng").sql("SELECT 1"),
    /tagged template/,
  );
});

test("vault sql tag preserves permission-denied errors from cross-vault SQL", async () => {
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async () => new Response(
      JSON.stringify({
        message: "permission denied for schema external",
        code: "permission_denied",
        details: { pg_sqlstate: "42501" },
      }),
      { status: 403, statusText: "Forbidden", headers: { "content-type": "application/json" } },
    ),
  }).vault("eng");

  const result = await client.sql`SELECT * FROM external__tasks`;

  assert.equal(result.data, null);
  assert.equal(result.error instanceof AkbError, true);
  assert.equal(result.error?.code, "permission_denied");
  assert.deepEqual(result.error?.details, { pg_sqlstate: "42501" });
});

test("createTypedFetch substitutes OpenAPI path params and keeps auth boundary", async () => {
  let seenUrl = "";
  let seenInit = {};
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      seenUrl = String(input);
      seenInit = init ?? {};
      return new Response(
        JSON.stringify({ kind: "vault_table_schema", vault: "eng", tables: [], total: 0 }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    },
  }).actingAs({ sub: "end-user-1", app_metadata: { org_id: "org-1", role: "member" } });

  const typedFetch = createTypedFetch(client);
  const result = await typedFetch("get", "/api/v1/tables/{vault}/schema", {
    path: { vault: "eng" },
    query: { fresh: true },
  });
  const headers = Object.fromEntries(new Headers(seenInit.headers));

  assert.equal(seenUrl, "https://akb.test/api/v1/tables/eng/schema?fresh=true");
  assert.equal(seenInit.method, "GET");
  assert.equal(headers.authorization, "Bearer service-key");
  assert.deepEqual(JSON.parse(headers["x-akb-claims"]), {
    sub: "end-user-1",
    app_metadata: { org_id: "org-1", role: "member" },
  });
  assert.equal(result.throwOnError().data.kind, "vault_table_schema");
});

test("from query builder is lazy thenable and fires once", async () => {
  let calls = 0;
  let seenUrl = "";
  let seenInit = {};
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      calls += 1;
      seenUrl = String(input);
      seenInit = init ?? {};
      return new Response(
        JSON.stringify({ kind: "table_query", columns: ["title"], items: [{ title: "Ship" }], total: 1 }),
        { status: 200, headers: { "content-type": "application/json", "content-range": "0-0/1" } },
      );
    },
  });

  const builder = client
    .vault("eng")
    .from("tasks")
    .select("title,status")
    .eq("status", "todo")
    .order("title", { ascending: false })
    .range(0, 9)
    .count();

  assert.equal(calls, 0);
  const first = await builder;
  const second = await builder;

  assert.equal(calls, 1);
  assert.equal(first, second);
  assert.equal(
    seenUrl,
    "https://akb.test/api/v1/tables/eng/tasks/rows?select=title%2Cstatus&status=eq.todo&order=title.desc",
  );
  assert.equal(seenInit.method, "GET");
  assert.equal(new Headers(seenInit.headers).get("prefer"), "count=exact");
  assert.equal(new Headers(seenInit.headers).get("range"), "0-9");
  assert.equal(first.throwOnError().data.items[0].title, "Ship");
});

test("from query builder serializes read operators to the URL contract", async () => {
  let seenUrl = "";
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async (input) => {
      seenUrl = String(input);
      return new Response(JSON.stringify({ kind: "table_query", columns: [], items: [], total: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });

  await client
    .vault("eng")
    .from("incidents")
    .select("id,metadata->>tier")
    .eq("title", "ship")
    .neq("severity", "low")
    .gt("score", 1)
    .gte("score", 2)
    .lt("score", 10)
    .lte("score", 11)
    .like("title", "*ship*")
    .ilike("metadata->>tier", "gold*")
    .is("archived", null)
    .in("severity", ["high", "low"])
    .cs("metadata", ["a", "b"])
    .not("status", "eq", "closed")
    .and("owner_id.eq.u1,score.gte.3")
    .filter("metadata#>>{audit,actor}::text", "eq", "kim");

  assert.deepEqual([...new URL(seenUrl).searchParams.entries()], [
    ["select", "id,metadata->>tier"],
    ["title", "eq.ship"],
    ["severity", "neq.low"],
    ["score", "gt.1"],
    ["score", "gte.2"],
    ["score", "lt.10"],
    ["score", "lte.11"],
    ["title", "like.*ship*"],
    ["metadata->>tier", "ilike.gold*"],
    ["archived", "is.null"],
    ["severity", "in.(high,low)"],
    ["metadata", "cs.{a,b}"],
    ["status", "not.eq.closed"],
    ["and", "(owner_id.eq.u1,score.gte.3)"],
    ["metadata#>>{audit,actor}::text", "eq.kim"],
  ]);
});

test("from query builder falls back to AST on URL budget and nested groups", async () => {
  const calls = [];
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    maxUrlBytes: 8,
    fetch: async (input, init) => {
      calls.push({ input: String(input), init });
      return new Response(JSON.stringify({ kind: "table_query", columns: [], items: [], total: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });

  await client.vault("eng").from("tasks").select("*").eq("status", "todo");
  assert.equal(calls[0].input, "https://akb.test/api/v1/tables/eng/tasks/query");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    select: "*",
    filter: { col: "status", op: "eq", val: "todo" },
  });

  const nestedClient = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      calls.push({ input: String(input), init });
      return new Response(JSON.stringify({ kind: "table_query", columns: [], items: [], total: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });
  await nestedClient
    .vault("eng")
    .from("tasks")
    .or((group) => group.eq("status", "todo").and((inner) => inner.eq("owner_id", "u1")));

  assert.equal(calls[1].input, "https://akb.test/api/v1/tables/eng/tasks/query");
  assert.deepEqual(JSON.parse(calls[1].init.body), {
    filter: {
      or: [
        { col: "status", op: "eq", val: "todo" },
        { and: [{ col: "owner_id", op: "eq", val: "u1" }] },
      ],
    },
  });
});

test("from query builder maps jsonb and boolean string groups to AST fallback", async () => {
  let seenInit = {};
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    maxUrlBytes: 4,
    fetch: async (_input, init) => {
      seenInit = init ?? {};
      return new Response(JSON.stringify({ kind: "table_query", columns: [], items: [], total: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });

  await client
    .vault("eng")
    .from("incidents")
    .select("id")
    .eq("metadata->>tier", "gold")
    .or("title.eq.ship,and(severity.eq.high,score.gte.3)")
    .order("metadata->>rank::int", { ascending: false })
    .limit(5)
    .count();

  assert.deepEqual(JSON.parse(seenInit.body), {
    select: "id",
    filter: {
      and: [
        { jsonb: { col: "metadata", path: ["tier"] }, op: "eq", val: "gold" },
        {
          or: [
            { col: "title", op: "eq", val: "ship" },
            {
              and: [
                { col: "severity", op: "eq", val: "high" },
                { col: "score", op: "gte", val: "3" },
              ],
            },
          ],
        },
      ],
    },
    order: [{ jsonb: { col: "metadata", path: ["rank"], cast: "int" }, dir: "desc" }],
    limit: 5,
    count: "exact",
  });
  assert.equal(new Headers(seenInit.headers).get("prefer"), "count=exact");
});

test("from query builder uses AST when URL arrays cannot round-trip losslessly", async () => {
  const calls = [];
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      calls.push({ input: String(input), init });
      return new Response(JSON.stringify({ kind: "table_query", columns: [], items: [], total: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });

  await client.vault("eng").from("tasks").in("title", ["ACME, Inc."]);

  assert.equal(calls[0].input, "https://akb.test/api/v1/tables/eng/tasks/query");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    filter: { col: "title", op: "in", val: ["ACME, Inc."] },
  });

  await client.vault("eng").from("tasks").cs("metadata", [{ tier: "gold" }]);

  assert.equal(calls[1].input, "https://akb.test/api/v1/tables/eng/tasks/query");
  assert.deepEqual(JSON.parse(calls[1].init.body), {
    filter: { col: "metadata", op: "cs", val: [{ tier: "gold" }] },
  });
});

test("from query builder uses AST when boolean scalar values cannot round-trip in URL groups", async () => {
  const calls = [];
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      calls.push({ input: String(input), init });
      return new Response(JSON.stringify({ kind: "table_query", columns: [], items: [], total: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });

  await client.vault("eng").from("tasks").or((group) => group.eq("title", "ACME, Inc."));

  assert.equal(calls[0].input, "https://akb.test/api/v1/tables/eng/tasks/query");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    filter: { or: [{ col: "title", op: "eq", val: "ACME, Inc." }] },
  });
});

test("from query builder applies maxUrlBytes to the full rows URL", async () => {
  const calls = [];
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    maxUrlBytes: 15,
    fetch: async (input, init) => {
      calls.push({ input: String(input), init });
      return new Response(JSON.stringify({ kind: "table_query", columns: [], items: [], total: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });

  await client.vault("eng").from("tasks").select("id");

  assert.equal(calls[0].input, "https://akb.test/api/v1/tables/eng/tasks/query");
  assert.deepEqual(JSON.parse(calls[0].init.body), { select: "id" });
});

test("from write builder maps insert/update/upsert/delete to rows verbs", async () => {
  const calls = [];
  const responses = [
    new Response(null, { status: 204, headers: { "content-range": "*/1" } }),
    new Response(JSON.stringify({ kind: "table_query", columns: ["id", "title"], items: [{ id: "1", title: "Ship" }], total: 1 }), {
      status: 201,
      headers: { "content-type": "application/json", "content-range": "0-0/1" },
    }),
    new Response(JSON.stringify({ kind: "table_query", columns: ["id", "status"], items: [{ id: "1", status: "done" }], total: 1 }), {
      status: 200,
      headers: { "content-type": "application/json", "content-range": "0-0/1" },
    }),
    new Response(JSON.stringify({ kind: "table_query", columns: ["id"], items: [{ id: "1" }], total: 1 }), {
      status: 201,
      headers: { "content-type": "application/json", "content-range": "0-0/1" },
    }),
    new Response(null, { status: 204, headers: { "content-range": "*/3" } }),
  ];
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      calls.push({ input: String(input), init });
      return responses.shift();
    },
  });

  const insertedMinimal = await client.vault("eng").from("tasks").insert({ title: "Draft" });
  const inserted = await client.vault("eng").from("tasks").insert({ title: "Ship" }).select("id,title").single();
  const updated = await client.vault("eng").from("tasks").update({ status: "done" }).eq("id", "1").select("id,status");
  await client
    .vault("eng")
    .from("tasks")
    .upsert({ external_id: "TASK-1", title: "Ship" }, { onConflict: "external_id", ignoreDuplicates: true })
    .select("*");
  await client.vault("eng").from("tasks").delete().all();

  assert.equal(insertedMinimal.data, null);
  assert.deepEqual(inserted.throwOnError().data, { id: "1", title: "Ship" });
  assert.equal(updated.throwOnError().data.items[0].status, "done");

  assert.equal(calls[0].input, "https://akb.test/api/v1/tables/eng/tasks/rows");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(new Headers(calls[0].init.headers).get("prefer"), "return=minimal");
  assert.deepEqual(JSON.parse(calls[0].init.body), { title: "Draft" });

  assert.equal(calls[1].input, "https://akb.test/api/v1/tables/eng/tasks/rows?select=id%2Ctitle");
  assert.equal(new Headers(calls[1].init.headers).get("prefer"), "return=representation");

  assert.equal(calls[2].input, "https://akb.test/api/v1/tables/eng/tasks/rows?select=id%2Cstatus&id=eq.1");
  assert.equal(calls[2].init.method, "PATCH");
  assert.equal(new Headers(calls[2].init.headers).get("prefer"), "return=representation");
  assert.deepEqual(JSON.parse(calls[2].init.body), { status: "done" });

  assert.equal(calls[3].input, "https://akb.test/api/v1/tables/eng/tasks/rows?select=*&on_conflict=external_id");
  assert.equal(calls[3].init.method, "POST");
  assert.equal(new Headers(calls[3].init.headers).get("prefer"), "resolution=ignore-duplicates, return=representation");

  assert.equal(calls[4].input, "https://akb.test/api/v1/tables/eng/tasks/rows?all=true");
  assert.equal(calls[4].init.method, "DELETE");
  assert.equal(new Headers(calls[4].init.headers).get("prefer"), "return=minimal");
  assert.equal(calls[4].init.body, undefined);
});

test("from write builder covers bulk insert, filtered delete, and default upsert conflict", async () => {
  const calls = [];
  const responses = [
    new Response(null, { status: 204 }),
    new Response(null, { status: 204 }),
    new Response(JSON.stringify({ kind: "table_query", columns: ["id"], items: [{ id: "1" }], total: 1 }), {
      status: 201,
      headers: { "content-type": "application/json", "content-range": "0-0/1" },
    }),
  ];
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      calls.push({ input: String(input), init });
      return responses.shift();
    },
  });

  await client.vault("eng").from("tasks").insert([{ title: "One" }, { title: "Two", status: "todo" }]);
  await client.vault("eng").from("tasks").delete().eq("status", "done");
  await client.vault("eng").from("tasks").upsert({ id: "1", title: "Ship" }).select("id");

  assert.equal(calls[0].input, "https://akb.test/api/v1/tables/eng/tasks/rows");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(new Headers(calls[0].init.headers).get("prefer"), "return=minimal");
  assert.deepEqual(JSON.parse(calls[0].init.body), [{ title: "One" }, { title: "Two", status: "todo" }]);

  assert.equal(calls[1].input, "https://akb.test/api/v1/tables/eng/tasks/rows?status=eq.done");
  assert.equal(calls[1].init.method, "DELETE");
  assert.equal(new Headers(calls[1].init.headers).get("prefer"), "return=minimal");
  assert.equal(calls[1].init.body, undefined);

  assert.equal(calls[2].input, "https://akb.test/api/v1/tables/eng/tasks/rows?select=id&on_conflict=id");
  assert.equal(calls[2].init.method, "POST");
  assert.equal(new Headers(calls[2].init.headers).get("prefer"), "return=representation");
  assert.deepEqual(JSON.parse(calls[2].init.body), { id: "1", title: "Ship" });
});

test("from write builder falls back to write AST for unsafe mutation filters", async () => {
  let seenUrl = "";
  let seenInit = {};
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      seenUrl = String(input);
      seenInit = init ?? {};
      return new Response(JSON.stringify({ kind: "table_query", columns: ["id"], items: [{ id: "1" }], total: 1 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  });

  await client.vault("eng").from("tasks").update({ status: "done" }).or((group) => group.eq("title", "ACME, Inc.")).select("id");

  assert.equal(seenUrl, "https://akb.test/api/v1/tables/eng/tasks/query");
  assert.equal(seenInit.method, "POST");
  assert.equal(new Headers(seenInit.headers).get("prefer"), "return=representation");
  assert.deepEqual(JSON.parse(seenInit.body), {
    update: { status: "done" },
    filter: { or: [{ col: "title", op: "eq", val: "ACME, Inc." }] },
    returning: "id",
  });
});

test("from write builder rejects unsupported read modifiers before destructive mutations", async () => {
  let called = false;
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async () => {
      called = true;
      return new Response(null, { status: 204 });
    },
  });

  const result = await client.vault("eng").from("tasks").delete().eq("status", "done").limit(1);

  assert.equal(called, false);
  assert.equal(result.data, null);
  assert.equal(result.error instanceof AkbError, true);
  assert.equal(result.error?.code, "unsupported_write_modifier");
  assert.deepEqual(result.error?.details, { modifiers: ["limit"] });
});

test("from query builder single and maybeSingle unwrap table rows", async () => {
  const responses = [
    new Response(JSON.stringify({ kind: "table_query", columns: ["id"], items: [], total: 0 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
    new Response(JSON.stringify({ kind: "table_query", columns: ["id"], items: [{ id: "1" }], total: 2 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  ];
  const client = createClient("https://akb.test/api/v1", {
    apiKey: fixtureApiKey,
    fetch: async () => responses.shift(),
  });

  const maybe = await client.vault("eng").from("tasks").select("id").maybeSingle();
  const tooMany = await client.vault("eng").from("tasks").select("id").single();

  assert.equal(maybe.error, null);
  assert.equal(maybe.data, null);
  assert.equal(tooMany.data, null);
  assert.equal(tooMany.error?.code, "invalid_single_result");
  assert.equal(tooMany.error instanceof AkbError, true);
  assert.throws(() => tooMany.throwOnError(), /single\(\) expected exactly one row/);
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
