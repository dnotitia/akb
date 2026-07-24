import assert from "node:assert/strict";

import { AkbError, createClient } from "@akb/client";

const calls = [];
const bearer = ["artifact", "proof"].join("-");
const claims = { sub: "proof-user", app_metadata: { org_id: "proof-org", role: "writer" } };

const fetchBoundary = async (input, init) => {
  const url = new URL(String(input));
  const headers = Object.fromEntries(new Headers(init?.headers));
  calls.push({ url: url.href, method: init?.method ?? "GET", headers });
  if (url.searchParams.get("author") === "error") {
    return json({ message: "fixture unavailable", code: "activity_failed" }, 503, "Unavailable");
  }
  if (url.pathname.includes("/history/")) {
    return json({
      kind: "document_history",
      uri: "akb://vault one/doc/guides/read me.md",
      history: [{ hash: "abc1234", message: "Update", author: "u1", date: "2026-07-22T00:00:00Z" }],
    });
  }
  if (url.pathname.includes("/diff/")) {
    return json({ kind: "document_diff", file: "guides/read me.md", commit: url.searchParams.get("commit"), type: "modified", diff: "@@ changed @@", error: null });
  }
  if (url.pathname.endsWith("/recent")) {
    return json({ kind: "recent_changes", changes: [{ doc_id: "d-12345678", vault: "vault one", path: "guides/read me.md", title: "Read me", type: "note", commit: null, changed_at: null }] });
  }
  return json({ kind: "activity", vault: "vault one", total: 1, activity: [{ hash: "abc1234", subject: "Update", author: "u1", date: "2026-07-22T00:00:00Z", action: "updated", summary: "guides/read me.md", agent: "api", files: [{ path: "guides/read me.md", change: "modified" }] }] });
};

const root = createClient({ baseUrl: "https://proof.invalid/api/v1", token: () => bearer, fetch: fetchBoundary }).actingAs(claims);
const scoped = root.vault("vault one");
const history = await scoped.docs.history("guides/read me.md", { limit: 0 });
const diff = await scoped.docs.diff("guides/read me.md", { vault: "override/vault", commit: "abc?123#" });
const activity = await scoped.activity.list({ collection: null, author: undefined, since: "2026-07-01T00:00:00Z", limit: 5 });
const recent = await scoped.activity.recent({ limit: 7 });
const raw = await scoped.activity.request("vault%20one?limit=1");
const failed = await scoped.activity.list({ author: "error" });
const callsBeforeMissingVault = calls.length;
assert.throws(() => root.docs.history("readme.md"), /Select a vault/);
assert.throws(() => root.docs.diff("readme.md", { commit: "abc1234" }), /Select a vault/);
assert.throws(() => root.activity.list(), /Select a vault/);
assert.equal(calls.length, callsBeforeMissingVault);
const crossVault = await root.activity.recent();

assert.equal(calls[0].url, "https://proof.invalid/api/v1/history/vault%20one/guides/read%20me.md?limit=0");
assert.equal(calls[1].url, "https://proof.invalid/api/v1/diff/override%2Fvault/guides/read%20me.md?commit=abc%3F123%23");
assert.equal(calls[2].url, "https://proof.invalid/api/v1/activity/vault%20one?since=2026-07-01T00%3A00%3A00Z&limit=5");
assert.equal(calls[3].url, "https://proof.invalid/api/v1/recent?vault=vault+one&limit=7");
assert.equal(calls[4].url, "https://proof.invalid/api/v1/activity/vault%20one?limit=1");
assert.equal(calls.at(-1).url, "https://proof.invalid/api/v1/recent");
assert.ok(calls.every((call) => call.method === "GET"));
assert.ok(calls.every((call) => call.headers.authorization === `Bearer ${bearer}`));
assert.ok(calls.every((call) => call.headers["x-akb-claims"] === JSON.stringify(claims)));
assert.deepEqual([history.data.kind, diff.data.kind, activity.data.kind, recent.data.kind, crossVault.data.kind], ["document_history", "document_diff", "activity", "recent_changes", "recent_changes"]);
assert.equal(diff.data.error, null);
assert.equal(activity.data.activity[0].files[0].change, "modified");
assert.equal(recent.data.changes[0].commit, null);
assert.equal(raw.data.kind, "activity");
assert.equal(failed.error?.status, 503);
assert.throws(() => failed.throwOnError(), AkbError);

console.log(JSON.stringify({
  overall_behavior: "satisfies_contract",
  target: { type: "packed npm artifact", access: "repository-external source-blind consumer" },
  calls: calls.map(({ url, method, headers }) => ({
    url,
    method,
    authorization: headers.authorization?.startsWith("Bearer ") ? "Bearer <redacted>" : null,
    claims: headers["x-akb-claims"] ? "present" : null,
  })),
  kinds: [history.data.kind, diff.data.kind, activity.data.kind, recent.data.kind],
  omitted: { list_nullish_filters: "absent", cross_vault_recent_query: "absent" },
  preflight: { missing_vault_rejected_before_fetch: true },
  errors: { result_status: failed.error.status, throwOnError: "AkbError" },
  public_exports: ["createClient", "AkbError"],
  screenshot: "not_applicable_non_visual_sdk",
}, null, 2));

function json(body, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "content-type": "application/json" } });
}
