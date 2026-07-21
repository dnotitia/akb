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

test("docs facade scopes document operations and maps typed payloads", async () => {
  const seen = [];
  const client = createClient("https://akb.test/api/v1/", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      const url = new URL(String(input));
      const method = init?.method ?? "GET";
      seen.push({
        url: String(input),
        method,
        headers: Object.fromEntries(new Headers(init?.headers)),
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });

      if (method === "POST" || method === "PATCH") {
        return new Response(
          JSON.stringify({
            kind: "document_write",
            uri: "akb://reef/doc/guides/readme.md",
            vault: "reef",
            path: "guides/readme.md",
            commit_hash: "abc1234",
            chunks_indexed: 1,
            entities_found: 0,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (method === "DELETE") {
        return new Response(JSON.stringify({ kind: "document", deleted: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.pathname.endsWith("/browse/reef")) {
        return new Response(
          JSON.stringify({ kind: "document", vault: "reef", path: "guides", items: [] }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          kind: "document",
          uri: "akb://reef/doc/guides/read me.md",
          vault: "reef",
          path: "guides/read me.md",
          title: "Readme",
          type: "note",
          status: "active",
          tags: [],
          content: "# Readme",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    },
  }).vault("reef").actingAs({ sub: "end-user-1", app_metadata: { org_id: "org-1", role: "member" } });

  const getResult = await client.docs.get("guides/read me.md", { version: "abc1234" });
  const browseResult = await client.docs.browse({
    collection: "guides",
    depth: 0,
    includeHashes: true,
    includeArchived: true,
  });
  const putResult = await client.docs.put({
    collection: "guides",
    title: "Readme",
    content: "# Readme",
    type: "note",
    status: "active",
    tags: ["sdk"],
    dependsOn: ["AKB-090"],
    relatedTo: ["AKB-095"],
    slug: "readme",
  });
  const updateResult = await client.docs.update("guides/readme.md", {
    content: "# Updated",
    expectedCommit: "abc1234",
    expectedContentHash: "hash1234",
    tags: ["sdk", "docs"],
  });
  const deleteResult = await client.docs.delete("guides/readme.md");

  const getUrl = new URL(seen[0].url);
  assert.equal(seen[0].method, "GET");
  assert.equal(getUrl.pathname, "/api/v1/documents/reef/guides/read%20me.md");
  assert.equal(getUrl.searchParams.get("version"), "abc1234");
  assert.deepEqual(JSON.parse(seen[0].headers["x-akb-claims"]), {
    sub: "end-user-1",
    app_metadata: { org_id: "org-1", role: "member" },
  });
  assert.equal(getResult.throwOnError().data.kind, "document");

  const browseUrl = new URL(seen[1].url);
  assert.equal(browseUrl.pathname, "/api/v1/browse/reef");
  assert.equal(browseUrl.searchParams.get("collection"), "guides");
  assert.equal(browseUrl.searchParams.get("depth"), "0");
  assert.equal(browseUrl.searchParams.get("include_hashes"), "true");
  assert.equal(browseUrl.searchParams.get("include_archived"), "true");
  assert.equal(browseResult.throwOnError().data.kind, "document");

  assert.equal(seen[2].method, "POST");
  assert.equal(new URL(seen[2].url).pathname, "/api/v1/documents");
  assert.deepEqual(seen[2].body, {
    vault: "reef",
    collection: "guides",
    title: "Readme",
    content: "# Readme",
    type: "note",
    status: "active",
    tags: ["sdk"],
    depends_on: ["AKB-090"],
    related_to: ["AKB-095"],
    slug: "readme",
  });
  assert.equal(putResult.throwOnError().data.kind, "document_write");

  assert.equal(seen[3].method, "PATCH");
  assert.equal(new URL(seen[3].url).pathname, "/api/v1/documents/reef/guides/readme.md");
  assert.deepEqual(seen[3].body, {
    content: "# Updated",
    tags: ["sdk", "docs"],
    expected_commit: "abc1234",
    expected_content_hash: "hash1234",
  });
  assert.equal(updateResult.throwOnError().data.kind, "document_write");

  assert.equal(seen[4].method, "DELETE");
  assert.equal(new URL(seen[4].url).pathname, "/api/v1/documents/reef/guides/readme.md");
  assert.equal(deleteResult.throwOnError().data.deleted, true);
});

test("docs facade creates and deletes collections with exact REST semantics", async () => {
  const seen = [];
  const client = createClient("https://akb.test/api/v1/", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      const url = new URL(String(input));
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      seen.push({
        url: String(input),
        method,
        headers: Object.fromEntries(new Headers(init?.headers)),
        body,
      });

      if (method === "POST") {
        return new Response(JSON.stringify({
          kind: "collection_create",
          ok: true,
          created: body.path !== "already/there",
          collection: {
            path: body.path,
            name: body.path.split("/").at(-1),
            summary: Object.hasOwn(body, "summary") ? body.summary : "stored summary",
            doc_count: body.path === "already/there" ? 3 : 0,
          },
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.pathname.endsWith("/blocked")) {
        return new Response(JSON.stringify({
          message: "Collection is not empty",
          code: "conflict",
          detail: { message: "Collection is not empty", doc_count: 2, file_count: 0, sub_collection_count: 1, table_count: 0 },
          details: { doc_count: 2, file_count: 0, sub_collection_count: 1, table_count: 0 },
        }), { status: 409, statusText: "Conflict", headers: { "content-type": "application/json" } });
      }
      if (url.pathname.endsWith("/missing")) {
        return new Response(JSON.stringify({ message: "Collection not found", code: "not_found" }), {
          status: 404,
          statusText: "Not Found",
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({
        kind: "collection_delete",
        ok: true,
        collection: decodeURIComponent(url.pathname.split("/").slice(6).join("/")),
        deleted_docs: url.searchParams.get("recursive") === "true" ? 2 : 0,
        deleted_files: 0,
        deleted_sub_collections: 1,
        deleted_tables: 0,
      }), { status: 200, headers: { "content-type": "application/json" } });
    },
  }).vault("vault one").actingAs({ sub: "end-user-1", app_metadata: { org_id: "org-1", role: "writer" } });

  const omitted = await client.docs.createCollection({ path: "already/there" });
  const explicitNull = await client.docs.createCollection({ path: "새 공간/api", summary: null });
  const recursive = await client.docs.deleteCollection("새 공간/예약%?#", { recursive: true });
  const explicitFalse = await client.docs.deleteCollection("plain/path", { recursive: false });
  const defaultDelete = await client.docs.deleteCollection("plain/other");
  const blocked = await client.docs.deleteCollection("blocked");
  const missing = await client.docs.deleteCollection("missing");

  assert.equal(seen[0].url, "https://akb.test/api/v1/collections/vault%20one");
  assert.equal(seen[0].method, "POST");
  assert.deepEqual(seen[0].body, { path: "already/there" });
  assert.equal(Object.hasOwn(seen[0].body, "summary"), false);
  assert.deepEqual(seen[1].body, { path: "새 공간/api", summary: null });
  assert.equal(seen[2].url, "https://akb.test/api/v1/collections/vault%20one/%EC%83%88%20%EA%B3%B5%EA%B0%84/%EC%98%88%EC%95%BD%25%3F%23?recursive=true");
  assert.equal(seen[2].method, "DELETE");
  assert.equal(seen[3].url, "https://akb.test/api/v1/collections/vault%20one/plain/path");
  assert.equal(seen[4].url, "https://akb.test/api/v1/collections/vault%20one/plain/other");
  assert.ok(seen.every((call) => call.headers.authorization === "Bearer service-key"));
  assert.ok(seen.every((call) => call.headers["x-akb-claims"]));
  assert.equal(seen[0].headers["content-type"], "application/json");

  assert.deepEqual(
    [omitted.throwOnError().data.kind, omitted.data.created, omitted.data.collection.doc_count],
    ["collection_create", false, 3],
  );
  assert.deepEqual(
    [explicitNull.throwOnError().data.kind, explicitNull.data.collection.summary],
    ["collection_create", null],
  );
  assert.deepEqual(
    [recursive.throwOnError().data.kind, recursive.data.deleted_docs, recursive.data.deleted_files],
    ["collection_delete", 2, 0],
  );
  assert.deepEqual(blocked.error?.details, { doc_count: 2, file_count: 0, sub_collection_count: 1, table_count: 0 });
  assert.equal(blocked.error?.payload.detail?.file_count, 0);
  assert.equal(missing.error?.status, 404);
  assert.throws(() => blocked.throwOnError(), AkbError);
  assert.throws(() => missing.throwOnError(), AkbError);
});

test("storage facade performs presigned upload, download, list, and delete flows", async () => {
  const fileId = "11111111-1111-4111-8111-111111111111";
  const fileUri = `akb://reef/coll/media/file/${fileId}`;
  const seen = [];
  const client = createClient("https://akb.test/api/v1/", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      const url = new URL(String(input));
      const method = init?.method ?? "GET";
      seen.push({
        url: String(input),
        method,
        headers: Object.fromEntries(new Headers(init?.headers)),
      });

      if (url.origin === "https://storage.example.com" && url.pathname === "/upload") {
        return new Response(null, { status: 200 });
      }
      if (url.origin === "https://storage.example.com" && url.pathname === "/download") {
        return new Response("hello", { status: 200 });
      }
      if (url.pathname.endsWith("/files/reef/upload")) {
        return new Response(
          JSON.stringify({
            kind: "file",
            uri: fileUri,
            vault: "reef",
            collection: "media",
            upload_url: "https://storage.example.com/upload",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.pathname.endsWith(`/${fileId}/confirm`)) {
        return new Response(
          JSON.stringify({ kind: "file", uri: fileUri, vault: "reef", name: "logo.txt", size_bytes: 5 }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.pathname === "/api/v1/files/reef" && method === "GET") {
        return new Response(
          JSON.stringify({
            kind: "file",
            vault: "reef",
            items: [{ kind: "file", uri: fileUri, collection: "media", name: "logo.txt" }],
            total: 1,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.pathname.endsWith(`/${fileId}/download`)) {
        return new Response(
          JSON.stringify({
            kind: "file",
            uri: fileUri,
            name: "logo.txt",
            download_url: "https://storage.example.com/download",
            size_bytes: 5,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.pathname.endsWith(`/${fileId}`) && method === "DELETE") {
        return new Response(JSON.stringify({ kind: "file", uri: fileUri, deleted: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      throw new Error(`Unexpected request: ${method} ${url.href}`);
    },
  }).vault("reef");

  const uploadResult = await client.storage.upload("media/logo.txt", new Blob(["hello"], { type: "text/plain" }), {
    description: "Logo",
    contentHash: "hash1234",
    hashAlgorithm: "sha256",
  });
  const downloadUrlResult = await client.storage.download("media/logo.txt");
  const downloadBytesResult = await client.storage.download("media/logo.txt", { bytes: true });
  const listResult = await client.storage.list({ collection: "media", limit: 10 });
  const deleteResult = await client.storage.delete("media/logo.txt");

  const presignCall = seen.find((call) => new URL(call.url).pathname.endsWith("/files/reef/upload"));
  const presignUrl = new URL(presignCall.url);
  assert.equal(presignCall.method, "POST");
  assert.equal(presignUrl.searchParams.get("filename"), "logo.txt");
  assert.equal(presignUrl.searchParams.get("collection"), "media");
  assert.equal(presignUrl.searchParams.get("description"), "Logo");
  assert.equal(presignUrl.searchParams.get("mime_type"), "text/plain");

  const uploadCall = seen.find((call) => call.url === "https://storage.example.com/upload");
  assert.equal(uploadCall.method, "PUT");
  assert.equal(uploadCall.headers["content-type"], "text/plain");
  assert.equal(uploadCall.headers.authorization, undefined);

  const confirmUrl = new URL(seen.find((call) => new URL(call.url).pathname.endsWith(`/${fileId}/confirm`)).url);
  assert.equal(confirmUrl.searchParams.get("content_hash"), "hash1234");
  assert.equal(confirmUrl.searchParams.get("hash_algorithm"), "sha256");
  assert.equal(uploadResult.throwOnError().data.kind, "file");

  const listCalls = seen.filter((call) => new URL(call.url).pathname === "/api/v1/files/reef");
  assert.ok(listCalls.length >= 3);
  assert.equal(new URL(listCalls[0].url).searchParams.get("collection"), "media");
  assert.equal(new URL(listCalls[0].url).searchParams.get("limit"), "200");

  assert.equal(downloadUrlResult.throwOnError().data.download_url, "https://storage.example.com/download");
  assert.equal(downloadBytesResult.throwOnError().data.bytes.byteLength, 5);
  assert.equal(new URL(listCalls.at(-1).url).searchParams.get("limit"), "200");
  assert.equal(listResult.throwOnError().data.total, 1);
  assert.equal(deleteResult.throwOnError().data.deleted, true);
});

test("storage facade resolves paths with a file collection segment through list lookup", async () => {
  const fileId = "22222222-2222-4222-8222-222222222222";
  const fileUri = `akb://reef/coll/media/file/file/${fileId}`;
  const seen = [];
  const client = createClient("https://akb.test/api/v1/", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      const url = new URL(String(input));
      seen.push({ url: String(input), method: init?.method ?? "GET" });
      if (url.pathname === "/api/v1/files/reef") {
        return new Response(
          JSON.stringify({
            kind: "file",
            vault: "reef",
            items: [{ kind: "file", uri: fileUri, collection: "media/file", name: "logo.txt" }],
            total: 1,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.pathname.endsWith(`/${fileId}/download`)) {
        return new Response(
          JSON.stringify({ kind: "file", uri: fileUri, download_url: "https://storage.example.com/download" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url.href}`);
    },
  }).vault("reef");

  const result = await client.storage.download("media/file/logo.txt");

  const listUrl = new URL(seen[0].url);
  assert.equal(listUrl.pathname, "/api/v1/files/reef");
  assert.equal(listUrl.searchParams.get("collection"), "media/file");
  assert.equal(new URL(seen[1].url).pathname, `/api/v1/files/reef/${fileId}/download`);
  assert.equal(result.throwOnError().data.download_url, "https://storage.example.com/download");
});

test("storage facade rejects ambiguous path lookups instead of picking the first file", async () => {
  const seen = [];
  const client = createClient("https://akb.test/api/v1/", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      const url = new URL(String(input));
      seen.push({ url: String(input), method: init?.method ?? "GET" });
      if (url.pathname === "/api/v1/files/reef") {
        return new Response(
          JSON.stringify({
            kind: "file",
            vault: "reef",
            items: [
              { kind: "file", uri: "akb://reef/coll/media/file/33333333-3333-4333-8333-333333333333", collection: "media", name: "logo.txt" },
              { kind: "file", uri: "akb://reef/coll/media/file/44444444-4444-4444-8444-444444444444", collection: "media", name: "logo.txt" },
            ],
            total: 2,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      throw new Error(`Unexpected request: ${url.href}`);
    },
  }).vault("reef");

  const result = await client.storage.delete("media/logo.txt");

  assert.equal(result.data, null);
  assert.equal(result.error?.code, "ambiguous_file_ref");
  assert.equal(seen.length, 1);
  assert.equal(new URL(seen[0].url).searchParams.get("collection"), "media");
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

test("graph facade maps typed methods to graph URLs and preserves auth envelopes", async () => {
  const calls = [];
  const client = createClient("https://akb.test/api/v1/", {
    apiKey: fixtureApiKey,
    defaultVault: "default vault",
    fetch: async (input, init) => {
      const url = new URL(String(input));
      calls.push({
        url: String(input),
        method: init?.method ?? "GET",
        headers: Object.fromEntries(new Headers(init?.headers)),
      });
      if (url.pathname.endsWith("/graph/overview")) {
        return new Response(JSON.stringify({
          kind: "graph_overview",
          nodes: [{ uri: "akb://default vault/doc/top", name: "Top", resource_type: "doc", degree: 4 }],
          edges: [],
          nodes_total: 9,
          edges_total: 4,
          returned: 1,
          truncated: true,
          orphans_returned: 2,
          orphans_truncated: false,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.pathname.endsWith("/graph/health")) {
        return new Response(JSON.stringify({
          kind: "graph_health",
          hubs: [{ uri: "akb://scope/doc/hub", name: "Hub", resource_type: "doc", degree: 7 }],
          orphans: { count: 3, sample: [{ uri: "akb://scope/doc/orphan", name: "Orphan", resource_type: "doc" }] },
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(JSON.stringify({
        kind: "graph_neighbors",
        nodes: [{ uri: url.searchParams.get("uri"), name: "Center", resource_type: "doc", depth: 0 }],
        edges: [{ source: "a", target: "b", relation: "links_to", kind: "explicit" }],
      }), { status: 200, headers: { "content-type": "application/json" } });
    },
  }).actingAs({ sub: "end-user-2", app_metadata: { org_id: "org-2", role: "reader" } });

  const neighbors = await client.graph.neighbors("akb://default vault/doc/a b", { hops: 3, limit: 17 });
  const overview = await client.graph.overview({ topK: 37 });
  const health = await client.vault("scope/vault").graph.health({ hubThreshold: 7, limit: 11 });

  assert.equal(calls[0].url, "https://akb.test/api/v1/graph?uri=akb%3A%2F%2Fdefault+vault%2Fdoc%2Fa+b&hops=3&limit=17");
  assert.equal(calls[1].url, "https://akb.test/api/v1/graph/overview?vault=default+vault&top_k=37");
  assert.equal(calls[2].url, "https://akb.test/api/v1/graph/health?vault=scope%2Fvault&hub_threshold=7&limit=11");
  assert.deepEqual(calls.map((call) => call.method), ["GET", "GET", "GET"]);
  for (const call of calls) {
    assert.equal(call.headers.authorization, "Bearer service-key");
    assert.deepEqual(JSON.parse(call.headers["x-akb-claims"]), {
      sub: "end-user-2",
      app_metadata: { org_id: "org-2", role: "reader" },
    });
  }
  assert.equal(neighbors.throwOnError().data.kind, "graph_neighbors");
  assert.equal(neighbors.throwOnError().data.edges[0].relation, "links_to");
  assert.equal(overview.throwOnError().data.kind, "graph_overview");
  assert.equal(overview.throwOnError().data.nodes_total, 9);
  assert.equal(health.throwOnError().data.kind, "graph_health");
  assert.equal(health.throwOnError().data.orphans.count, 3);
});

test("graph facade omits defaults, preflights vault, preserves 4xx, and keeps raw request", async () => {
  const calls = [];
  const responses = [
    new Response(JSON.stringify({ kind: "graph_neighbors", nodes: [], edges: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
    new Response(JSON.stringify({ kind: "graph_overview", nodes: [], edges: [], nodes_total: 0, edges_total: 0, returned: 0, truncated: false, orphans_returned: 0, orphans_truncated: false }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
    new Response(JSON.stringify({ detail: [{ loc: ["query", "limit"], msg: "Input should be less than or equal to 200" }], code: "validation_error" }), {
      status: 422,
      statusText: "Unprocessable Entity",
      headers: { "content-type": "application/json" },
    }),
    new Response(JSON.stringify({ kind: "graph_overview", nodes: [], edges: [], nodes_total: 0, edges_total: 0, returned: 0, truncated: false, orphans_returned: 0, orphans_truncated: false }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  ];
  const scoped = createClient("https://akb.test/api/v1", {
    fetch: async (input, init) => {
      calls.push({ url: String(input), method: init?.method ?? "GET" });
      return responses.shift();
    },
  }).vault("reef");

  await scoped.graph.neighbors("akb://reef/doc/one");
  await scoped.graph.overview();
  const invalid = await scoped.graph.health({ limit: 999 });
  const raw = await scoped.graph.request("?vault=reef");

  assert.equal(calls[0].url, "https://akb.test/api/v1/graph?uri=akb%3A%2F%2Freef%2Fdoc%2Fone");
  assert.equal(calls[1].url, "https://akb.test/api/v1/graph/overview?vault=reef");
  assert.equal(calls[2].url, "https://akb.test/api/v1/graph/health?vault=reef&limit=999");
  assert.equal(calls[3].url, "https://akb.test/api/v1/graph/?vault=reef");
  assert.equal(invalid.data, null);
  assert.equal(invalid.error?.status, 422);
  assert.equal(invalid.error?.code, "validation_error");
  assert.throws(() => invalid.throwOnError(), AkbError);
  assert.equal(raw.throwOnError().data.kind, "graph_overview");

  let noVaultCalls = 0;
  const root = createClient("https://akb.test/api/v1", {
    fetch: async () => {
      noVaultCalls += 1;
      return new Response("{}", { status: 200 });
    },
  });
  assert.throws(() => root.graph.overview(), /Select a vault.*graph overview/i);
  assert.throws(() => root.graph.health(), /Select a vault.*graph health/i);
  assert.equal(noVaultCalls, 0);
});

test("graph relation and provenance facade maps exact root requests and preserves envelopes", async () => {
  const calls = [];
  let omittedUnlinks = 0;
  const claims = { sub: "relation-user", app_metadata: { org_id: "org-graph", role: "writer" } };
  const client = createClient("https://akb.test/api/v1/", {
    apiKey: fixtureApiKey,
    fetch: async (input, init) => {
      const url = new URL(String(input));
      const method = init?.method ?? "GET";
      const body = init && Object.hasOwn(init, "body") ? init.body : undefined;
      calls.push({
        url: String(input), method, body,
        hasBody: init ? Object.hasOwn(init, "body") : false,
        headers: Object.fromEntries(new Headers(init?.headers)),
      });
      if (url.pathname.endsWith("/provenance")) {
        const uri = url.searchParams.get("uri");
        return responseJson({
          kind: "provenance", doc_id: uri.endsWith("alpha doc") ? "doc-alpha" : "doc-beta",
          title: uri.endsWith("alpha doc") ? "Alpha" : "Beta", path: "proof.md",
          vault: uri.includes("vault one") ? "vault one" : "vault-two", uri,
          created_by: null, created_at: null, updated_at: "2026-07-20T00:00:00Z",
          current_commit: null, relations: [],
        });
      }
      if (method === "POST") return responseJson({ kind: "relation_link", ...JSON.parse(init.body) });
      if (method === "DELETE") {
        const relation = url.searchParams.get("relation");
        if (relation === null) omittedUnlinks += 1;
        return responseJson({
          kind: "relation_unlink", source: url.searchParams.get("source"),
          target: url.searchParams.get("target"), relation,
          unlinked: relation === null ? (omittedUnlinks === 1 ? 1 : 0) : 1,
        });
      }
      return responseJson({
        kind: "relations", uri: url.searchParams.get("uri"),
        relations: [{ direction: "outgoing", relation: url.searchParams.get("type") ?? "related_to",
          uri: "akb://vault-two/table/target", resource_type: "table", name: null }],
      });
    },
  }).actingAs(claims);

  const relationsA = await client.graph.relations("akb://vault one/doc/alpha doc", { direction: "outgoing", type: "links_to" });
  const relationsB = await client.graph.relations("akb://vault-two/table/source");
  const linkA = await client.graph.link({ source: "akb://vault one/doc/alpha doc", target: "akb://vault-two/table/target", relation: "references", metadata: { confidence: 0.75, source: "fixture-a" } });
  const linkB = await client.graph.link({ source: "akb://vault-two/doc/beta", target: "akb://vault-two/file/file-2", relation: "attached_to" });
  const namedUnlink = await client.graph.unlink({ source: "akb://vault one/doc/alpha doc", target: "akb://vault-two/table/target", relation: "references" });
  const unlinkInput = { source: "akb://vault-two/doc/beta", target: "akb://vault-two/file/file-2" };
  const unlinkFirst = await client.graph.unlink(unlinkInput);
  const unlinkAgain = await client.graph.unlink(unlinkInput);
  const provenanceA = await client.graph.provenance("akb://vault one/doc/alpha doc");
  const provenanceB = await client.graph.provenance("akb://vault-two/doc/beta");

  assert.equal(calls[0].url, "https://akb.test/api/v1/relations?uri=akb%3A%2F%2Fvault+one%2Fdoc%2Falpha+doc&direction=outgoing&type=links_to");
  assert.equal(calls[1].url, "https://akb.test/api/v1/relations?uri=akb%3A%2F%2Fvault-two%2Ftable%2Fsource");
  assert.equal(calls[2].url, "https://akb.test/api/v1/relations");
  assert.deepEqual(JSON.parse(calls[2].body), { source: "akb://vault one/doc/alpha doc", target: "akb://vault-two/table/target", relation: "references", metadata: { confidence: 0.75, source: "fixture-a" } });
  assert.deepEqual(JSON.parse(calls[3].body), { source: "akb://vault-two/doc/beta", target: "akb://vault-two/file/file-2", relation: "attached_to" });
  assert.equal(calls[4].url, "https://akb.test/api/v1/relations?source=akb%3A%2F%2Fvault+one%2Fdoc%2Falpha+doc&target=akb%3A%2F%2Fvault-two%2Ftable%2Ftarget&relation=references");
  assert.equal(calls[5].url, "https://akb.test/api/v1/relations?source=akb%3A%2F%2Fvault-two%2Fdoc%2Fbeta&target=akb%3A%2F%2Fvault-two%2Ffile%2Ffile-2");
  assert.equal(calls[6].url, calls[5].url);
  assert.equal(calls[7].url, "https://akb.test/api/v1/provenance?uri=akb%3A%2F%2Fvault+one%2Fdoc%2Falpha+doc");
  assert.equal(calls[8].url, "https://akb.test/api/v1/provenance?uri=akb%3A%2F%2Fvault-two%2Fdoc%2Fbeta");
  assert.deepEqual(calls.map((call) => call.method), ["GET", "GET", "POST", "POST", "DELETE", "DELETE", "DELETE", "GET", "GET"]);
  assert.ok(calls.slice(4, 7).every((call) => !call.hasBody && call.body === undefined));
  assert.ok(calls.every((call) => call.headers.authorization === "Bearer service-key"));
  assert.ok(calls.every((call) => call.headers["x-akb-claims"] === JSON.stringify(claims)));
  assert.equal(relationsA.throwOnError().data.kind, "relations");
  assert.equal(relationsA.data.relations[0].relation, "links_to");
  assert.equal(relationsB.data.relations[0].relation, "related_to");
  assert.equal(linkA.throwOnError().data.kind, "relation_link");
  assert.deepEqual(linkA.data.metadata, { confidence: 0.75, source: "fixture-a" });
  assert.equal(linkB.data.metadata, undefined);
  assert.equal(namedUnlink.throwOnError().data.kind, "relation_unlink");
  assert.equal(unlinkFirst.data.unlinked, 1);
  assert.equal(unlinkAgain.data.unlinked, 0);
  assert.equal(provenanceA.throwOnError().data.kind, "provenance");
  assert.equal(provenanceA.data.doc_id, "doc-alpha");
  assert.equal(provenanceB.data.doc_id, "doc-beta");
  assert.equal(Object.hasOwn(provenanceA.data, "provenance"), false);
});

test("graph relation and provenance facade preserves backend 4xx result behavior", async () => {
  const client = createClient("https://akb.test/api/v1/", {
    fetch: async (input, init) => (init?.method ?? "GET") === "POST"
      ? responseJson({ message: "writer role required", code: "permission_denied" }, 403, "Forbidden")
      : responseJson({ detail: "invalid document URI", code: "validation_error" }, 422, "Unprocessable Entity"),
  });
  const invalid = await client.graph.provenance("not-an-akb-uri");
  const denied = await client.graph.link({ source: "akb://reader/doc/a", target: "akb://reader/doc/b", relation: "related_to" });
  assert.equal(invalid.data, null);
  assert.equal(invalid.error.status, 422);
  assert.equal(invalid.error.code, "validation_error");
  assert.throws(() => invalid.throwOnError(), AkbError);
  assert.equal(denied.data, null);
  assert.equal(denied.error.status, 403);
  assert.equal(denied.error.code, "permission_denied");
  assert.throws(() => denied.throwOnError(), AkbError);
});

function responseJson(body, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "content-type": "application/json" } });
}
