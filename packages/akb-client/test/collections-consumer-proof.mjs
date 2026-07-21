import assert from "node:assert/strict";

import { AkbError, createClient } from "@akb/client";

const calls = [];
const bearer = ["artifact", "proof"].join("-");
const claims = { sub: "proof-user", app_metadata: { org_id: "proof-org", role: "writer" } };

const fetchBoundary = async (input, init) => {
  const url = new URL(String(input));
  const method = init?.method ?? "GET";
  const headers = Object.fromEntries(new Headers(init?.headers));
  const body = init?.body ? JSON.parse(String(init.body)) : null;
  calls.push({ url: url.href, method, headers, body });

  const last = decodeURIComponent(url.pathname.split("/").at(-1));
  const errorByPath = {
    bad: [400, "invalid_argument"],
    unauthenticated: [401, "permission_denied"],
    forbidden: [403, "permission_denied"],
    missing: [404, "not_found"],
    blocked: [409, "conflict"],
    invalid: [422, "invalid_argument"],
  };
  if (method === "DELETE" && errorByPath[last]) {
    const [status, code] = errorByPath[last];
    const counts = { doc_count: 2, file_count: 0, sub_collection_count: 1, table_count: 0 };
    return json({
      message: status === 409 ? "Collection is not empty" : `fixture ${status}`,
      code,
      detail: status === 409 ? { message: "Collection is not empty", ...counts } : `fixture ${status}`,
      ...(status === 409 ? { details: counts } : {}),
    }, status, `Fixture ${status}`);
  }
  if (method === "POST") {
    return json({
      kind: "collection_create",
      ok: true,
      created: body.path !== "already/there",
      collection: {
        path: body.path,
        name: body.path.split("/").at(-1),
        summary: Object.hasOwn(body, "summary") ? body.summary : "stored summary",
        doc_count: body.path === "already/there" ? 4 : 0,
      },
    });
  }
  return json({
    kind: "collection_delete",
    ok: true,
    collection: decodeURIComponent(url.pathname.split("/").slice(5).join("/")),
    deleted_docs: url.searchParams.get("recursive") === "true" ? 3 : 0,
    deleted_files: 0,
    deleted_sub_collections: url.searchParams.get("recursive") === "true" ? 2 : 0,
    deleted_tables: 0,
  });
};

const root = createClient({
  baseUrl: "https://proof.invalid/api/v1",
  token: () => bearer,
  fetch: fetchBoundary,
}).actingAs(claims);
const alpha = root.vault("vault one");
const beta = root.vault("두 번째");

const omitted = await alpha.docs.createCollection({ path: "already/there" });
const explicitNull = await beta.docs.createCollection({ path: "새 공간/api", summary: null });
const recursive = await beta.docs.deleteCollection("새 공간/예약%?#", { recursive: true });
const explicitFalse = await alpha.docs.deleteCollection("plain/path", { recursive: false });
const defaultDelete = await alpha.docs.deleteCollection("plain/other");
const errors = {};
for (const path of ["bad", "unauthenticated", "forbidden", "missing", "blocked", "invalid"]) {
  errors[path] = await alpha.docs.deleteCollection(path);
}

assert.equal(calls[0].url, "https://proof.invalid/api/v1/collections/vault%20one");
assert.equal(calls[0].method, "POST");
assert.deepEqual(calls[0].body, { path: "already/there" });
assert.equal(Object.hasOwn(calls[0].body, "summary"), false);
assert.equal(calls[1].url, "https://proof.invalid/api/v1/collections/%EB%91%90%20%EB%B2%88%EC%A7%B8");
assert.deepEqual(calls[1].body, { path: "새 공간/api", summary: null });
assert.equal(calls[2].url, "https://proof.invalid/api/v1/collections/%EB%91%90%20%EB%B2%88%EC%A7%B8/%EC%83%88%20%EA%B3%B5%EA%B0%84/%EC%98%88%EC%95%BD%25%3F%23?recursive=true");
assert.equal(calls[2].method, "DELETE");
assert.equal(calls[3].url, "https://proof.invalid/api/v1/collections/vault%20one/plain/path");
assert.equal(calls[4].url, "https://proof.invalid/api/v1/collections/vault%20one/plain/other");
assert.ok(calls.every((call) => call.headers.authorization === `Bearer ${bearer}`));
assert.ok(calls.every((call) => call.headers["x-akb-claims"] === JSON.stringify(claims)));
assert.equal(calls[0].headers["content-type"], "application/json");

assert.deepEqual([omitted.data.kind, omitted.data.created, omitted.data.collection.summary, omitted.data.collection.doc_count], ["collection_create", false, "stored summary", 4]);
assert.deepEqual([explicitNull.data.kind, explicitNull.data.collection.summary, explicitNull.data.collection.doc_count], ["collection_create", null, 0]);
assert.deepEqual([recursive.data.kind, recursive.data.deleted_docs, recursive.data.deleted_files, recursive.data.deleted_sub_collections, recursive.data.deleted_tables], ["collection_delete", 3, 0, 2, 0]);
assert.deepEqual([explicitFalse.data.deleted_docs, defaultDelete.data.deleted_docs], [0, 0]);

const expectedStatuses = { bad: 400, unauthenticated: 401, forbidden: 403, missing: 404, blocked: 409, invalid: 422 };
for (const [path, status] of Object.entries(expectedStatuses)) {
  assert.equal(errors[path].data, null);
  assert.equal(errors[path].error?.status, status);
  assert.throws(() => errors[path].throwOnError(), AkbError);
}
assert.deepEqual(errors.blocked.error.details, { doc_count: 2, file_count: 0, sub_collection_count: 1, table_count: 0 });
assert.equal(errors.blocked.error.payload.detail.table_count, 0);

console.log(JSON.stringify({
  overall_behavior: "satisfies_contract",
  target: { type: "packed npm artifact", access: "repository-external consumer" },
  calls: calls.map(({ url, method, headers, body }) => ({
    url,
    method,
    body,
    authorization: headers.authorization?.startsWith("Bearer ") ? "Bearer <redacted>" : null,
    claims: headers["x-akb-claims"] ? "present" : null,
  })),
  kinds: [omitted.data.kind, explicitNull.data.kind, recursive.data.kind],
  varied: { summaries: [omitted.data.collection.summary, explicitNull.data.collection.summary], counts: [omitted.data.collection.doc_count, recursive.data.deleted_docs, recursive.data.deleted_sub_collections] },
  omitted: { summary: "absent", recursive_false: "absent", recursive_default: "absent" },
  errors: Object.fromEntries(Object.entries(errors).map(([path, result]) => [path, result.error?.status])),
  conflict: { details: errors.blocked.error.details, legacy_detail: "present", throwOnError: "AkbError" },
  screenshot: "not_applicable_non_visual_sdk",
}, null, 2));

function json(body, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "content-type": "application/json" } });
}
