import assert from "node:assert/strict";

import { AkbError, createClient } from "@akb/client";

const calls = [];
const bearer = ["artifact", "proof"].join("-");
const claims = { sub: "proof-user", app_metadata: { org_id: "proof-org", role: "writer" } };
let omittedUnlinks = 0;

const fetchBoundary = async (input, init) => {
  const url = new URL(String(input));
  const method = init?.method ?? "GET";
  const hasBody = init ? Object.hasOwn(init, "body") : false;
  const headers = Object.fromEntries(new Headers(init?.headers));
  calls.push({ url: url.href, method, hasBody, body: init?.body, headers });

  if (url.searchParams.get("uri") === "invalid-uri") {
    return json({ detail: "invalid resource URI", code: "validation_error" }, 422, "Unprocessable Entity");
  }
  if (method === "POST" && JSON.parse(init.body).source.includes("reader")) {
    return json({ message: "writer role required", code: "permission_denied" }, 403, "Forbidden");
  }
  if (url.pathname.endsWith("/provenance")) {
    const uri = url.searchParams.get("uri");
    const alpha = uri.includes("alpha doc");
    return json({
      kind: "provenance",
      doc_id: alpha ? "doc-alpha" : "doc-beta",
      title: alpha ? "Alpha" : "Beta",
      path: alpha ? "alpha.md" : "beta.md",
      vault: alpha ? "vault one" : "vault-two",
      uri,
      created_by: alpha ? null : "fixture-author",
      created_at: null,
      updated_at: alpha ? "2026-07-20T00:00:00Z" : null,
      current_commit: null,
      relations: [],
    });
  }
  if (method === "POST") return json({ kind: "relation_link", linked: true, ...JSON.parse(init.body) });
  if (method === "DELETE") {
    const relation = url.searchParams.get("relation");
    if (relation === null) omittedUnlinks += 1;
    return json({
      kind: "relation_unlink",
      source: url.searchParams.get("source"),
      target: url.searchParams.get("target"),
      unlinked: relation === null ? (omittedUnlinks === 1 ? 1 : 0) : 1,
    });
  }
  return json({
    kind: "relations",
    uri: url.searchParams.get("uri"),
    relations: [{
      direction: url.searchParams.get("direction") === "incoming" ? "incoming" : "outgoing",
      relation: url.searchParams.get("type") ?? "related_to",
      uri: "akb://vault-two/table/target",
      resource_type: "table",
      name: null,
    }],
  });
};

const client = createClient({
  baseUrl: "https://proof.invalid/api/v1",
  token: () => bearer,
  fetch: fetchBoundary,
}).actingAs(claims);

const relationsA = await client.graph.relations("akb://vault one/doc/alpha doc", { direction: "outgoing", type: "links_to" });
const relationsB = await client.graph.relations("akb://vault-two/table/source");
const linkA = await client.graph.link({ source: "akb://vault one/doc/alpha doc", target: "akb://vault-two/table/target", relation: "references", metadata: { confidence: 0.75, fixture: "alpha" } });
const linkB = await client.graph.link({ source: "akb://vault-two/doc/beta", target: "akb://vault-two/file/file-2", relation: "attached_to" });
const namedUnlink = await client.graph.unlink({ source: "akb://vault one/doc/alpha doc", target: "akb://vault-two/table/target", relation: "references" });
const unlinkInput = { source: "akb://vault-two/doc/beta", target: "akb://vault-two/file/file-2" };
const unlinkFirst = await client.graph.unlink(unlinkInput);
const unlinkAgain = await client.graph.unlink(unlinkInput);
const provenanceA = await client.graph.provenance("akb://vault one/doc/alpha doc");
const provenanceB = await client.graph.provenance("akb://vault-two/doc/beta");
const invalid = await client.graph.provenance("invalid-uri");
const denied = await client.graph.link({ source: "akb://reader/doc/a", target: "akb://reader/doc/b", relation: "related_to" });

assert.equal(calls[0].url, "https://proof.invalid/api/v1/relations?uri=akb%3A%2F%2Fvault+one%2Fdoc%2Falpha+doc&direction=outgoing&type=links_to");
assert.equal(calls[1].url, "https://proof.invalid/api/v1/relations?uri=akb%3A%2F%2Fvault-two%2Ftable%2Fsource");
assert.equal(calls[2].url, "https://proof.invalid/api/v1/relations");
assert.deepEqual(JSON.parse(calls[2].body), { source: "akb://vault one/doc/alpha doc", target: "akb://vault-two/table/target", relation: "references", metadata: { confidence: 0.75, fixture: "alpha" } });
assert.deepEqual(JSON.parse(calls[3].body), { source: "akb://vault-two/doc/beta", target: "akb://vault-two/file/file-2", relation: "attached_to" });
assert.equal(calls[4].url, "https://proof.invalid/api/v1/relations?source=akb%3A%2F%2Fvault+one%2Fdoc%2Falpha+doc&target=akb%3A%2F%2Fvault-two%2Ftable%2Ftarget&relation=references");
assert.equal(calls[5].url, "https://proof.invalid/api/v1/relations?source=akb%3A%2F%2Fvault-two%2Fdoc%2Fbeta&target=akb%3A%2F%2Fvault-two%2Ffile%2Ffile-2");
assert.equal(calls[6].url, calls[5].url);
assert.equal(calls[7].url, "https://proof.invalid/api/v1/provenance?uri=akb%3A%2F%2Fvault+one%2Fdoc%2Falpha+doc");
assert.equal(calls[8].url, "https://proof.invalid/api/v1/provenance?uri=akb%3A%2F%2Fvault-two%2Fdoc%2Fbeta");
assert.deepEqual(calls.slice(0, 9).map((call) => call.method), ["GET", "GET", "POST", "POST", "DELETE", "DELETE", "DELETE", "GET", "GET"]);
assert.ok(calls.slice(4, 7).every((call) => !call.hasBody));
assert.ok(calls.every((call) => call.headers.authorization === `Bearer ${bearer}`));
assert.ok(calls.every((call) => call.headers["x-akb-claims"] === JSON.stringify(claims)));
assert.equal(relationsA.data.relations[0].relation, "links_to");
assert.equal(relationsB.data.relations[0].relation, "related_to");
assert.equal(linkA.data.kind, "relation_link");
assert.deepEqual(linkA.data.metadata, { confidence: 0.75, fixture: "alpha" });
assert.equal(linkB.data.metadata, undefined);
assert.equal(namedUnlink.data.kind, "relation_unlink");
assert.deepEqual([unlinkFirst.data.unlinked, unlinkAgain.data.unlinked], [1, 0]);
assert.deepEqual([provenanceA.data.doc_id, provenanceB.data.doc_id], ["doc-alpha", "doc-beta"]);
assert.equal(Object.hasOwn(provenanceA.data, "provenance"), false);
assert.deepEqual([invalid.data, invalid.error?.status, invalid.error?.code], [null, 422, "validation_error"]);
assert.deepEqual([denied.data, denied.error?.status, denied.error?.code], [null, 403, "permission_denied"]);
assert.throws(() => invalid.throwOnError(), AkbError);
assert.throws(() => denied.throwOnError(), AkbError);

console.log(JSON.stringify({
  overall_behavior: "satisfies_contract",
  calls: calls.map(({ url, method, hasBody, headers }) => ({
    url, method, body: hasBody ? "present" : "absent",
    authorization: headers.authorization?.startsWith("Bearer ") ? "Bearer <redacted>" : null,
    claims: headers["x-akb-claims"] ? "present" : null,
  })),
  kinds: [relationsA.data?.kind, linkA.data?.kind, namedUnlink.data?.kind, provenanceA.data?.kind],
  varied: { relations: [relationsA.data?.relations[0].relation, relationsB.data?.relations[0].relation], provenance: [provenanceA.data?.doc_id, provenanceB.data?.doc_id] },
  omitted: { relation_options: "absent", link_metadata: "absent", unlink_relation: "absent", delete_body: "absent" },
  unlinked: [unlinkFirst.data?.unlinked, unlinkAgain.data?.unlinked],
  errors: [{ status: invalid.error?.status, code: invalid.error?.code }, { status: denied.error?.status, code: denied.error?.code }],
  throwOnError: "AkbError",
  screenshot: "not_applicable_non_visual_sdk",
}, null, 2));

function json(body, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "content-type": "application/json" } });
}
