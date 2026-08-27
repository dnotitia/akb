// Contract test for the proxy's destructure patterns.
// Runs against fixed JSON shapes the backend now returns post-envelope
// adoption (kind/id/items/vault). No real network calls — we verify
// the proxy's parsing assumptions still hold.
//
// Run with: node packages/akb-mcp-client/test/contract.test.mjs

import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AKBProxy } from "../lib/proxy.mjs";

let pass = 0;
let fail = 0;
function it(name, fn) {
  try {
    fn();
    pass++;
    console.log(`  ✓ ${name}`);
  } catch (e) {
    fail++;
    console.log(`  ✗ ${name}: ${e.message}`);
  }
}

const pending = [];
function itAsync(name, fn) {
  pending.push(fn().then(
    () => { pass++; console.log(`  ✓ ${name}`); },
    (e) => { fail++; console.log(`  ✗ ${name}: ${e.message}`); },
  ));
}

// ── Fixtures: representative backend envelope responses ──────────

const initiateUploadResp = JSON.stringify({
  kind: "file",
  uri: "akb://myvault/file/11111111-2222-3333-4444-555555555555",
  vault: "myvault",
  upload_url: "https://s3.example/presigned",
  s3_key: "myvault/abc123_file.bin",
  expires_in: 3600,
});

const downloadResp = JSON.stringify({
  kind: "file",
  name: "file.bin",
  download_url: "https://s3.example/presigned-get",
  mime_type: "application/octet-stream",
  size_bytes: 4096,
  content_hash: "a".repeat(64),
  hash_algorithm: "sha256",
  etag: "etag-1",
  storage_version: "version-1",
  version: "version-1",
  expires_in: 3600,
});

const deleteResp = JSON.stringify({
  kind: "file",
  id: "11111111-2222-3333-4444-555555555555",
  vault: "myvault",
  name: "file.bin",
  deleted: true,
});

const listResp = JSON.stringify({
  kind: "file",
  vault: "myvault",
  items: [
    { kind: "file", id: "id-1", name: "a.txt", mime_type: "text/plain", size_bytes: 10 },
  ],
  total: 1,
});

// ── Proxy-pattern destructure mirrors ────────────────────────────

it("_putFile destructures uri + upload_url", () => {
  const { uri, upload_url } = JSON.parse(initiateUploadResp);
  assert.match(uri, /^akb:\/\/myvault\/file\//);
  assert.match(upload_url, /^https:/);
});

it("_getFile destructures name + download_url + size_bytes + hash/version fields", () => {
  const { name: filename, download_url, size_bytes, content_hash, hash_algorithm, version } = JSON.parse(downloadResp);
  assert.equal(filename, "file.bin");
  assert.match(download_url, /^https:/);
  assert.equal(size_bytes, 4096);
  assert.match(content_hash, /^[0-9a-f]{64}$/);
  assert.equal(hash_algorithm, "sha256");
  assert.equal(version, "version-1");
});

it("_deleteFile passthrough produces a dict (not bool)", () => {
  const d = JSON.parse(deleteResp);
  assert.equal(typeof d, "object");
  assert.notEqual(d, null);
  assert.equal(d.deleted, true);
  assert.equal(d.kind, "file");
});

it("list response uses items, not files", () => {
  const d = JSON.parse(listResp);
  assert.ok(Array.isArray(d.items), "items should be an array");
  assert.equal(d.items.length, 1);
  assert.equal(d.total, 1);
  assert.equal(d.items[0].kind, "file");
});

it("envelope adds kind without breaking legacy fields", () => {
  // Confirm the old keys the proxy reads are still present alongside
  // the new envelope discriminator.
  const init = JSON.parse(initiateUploadResp);
  for (const k of ["kind", "uri", "upload_url", "s3_key", "expires_in"]) {
    assert.ok(k in init, `initiate response missing ${k}`);
  }
  const dl = JSON.parse(downloadResp);
  for (const k of ["kind", "name", "download_url", "size_bytes", "content_hash", "hash_algorithm"]) {
    assert.ok(k in dl, `download response missing ${k}`);
  }
});

const modernMeta = {
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientCapabilities": {},
  "io.modelcontextprotocol/clientInfo": { name: "modern-test", version: "1" },
};

function modernRequest(id, method, params = {}) {
  return {
    jsonrpc: "2.0",
    id,
    method,
    params: { ...params, _meta: modernMeta },
  };
}

itAsync("modern discover fixes the proxy generation and advertises stateless support", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  proxy._forward = async (msg) => ({
    jsonrpc: "2.0",
    id: msg.id,
    result: { supportedVersions: ["2026-07-28"], capabilities: { tools: {} } },
  });

  const response = await proxy._handle(modernRequest(1, "server/discover"));

  assert.equal(proxy._protocolGeneration, "modern");
  assert.equal(response.result.supportedVersions[0], "2026-07-28");
  assert.equal(response.result._meta["io.modelcontextprotocol/serverInfo"].name, "akb-mcp");
});

itAsync("modern backend requests carry exact routing headers and request metadata", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  proxy._protocolGeneration = "modern";
  proxy._clientMeta = modernMeta;
  let request;
  proxy._http = async (method, path, body, headers) => {
    request = { method, path, body: JSON.parse(body.toString()), headers };
    return { text: JSON.stringify({ jsonrpc: "2.0", id: 1, result: { ok: true } }), headers: {} };
  };

  const result = await proxy._rpc("tools/call", { name: "akb_help", arguments: {} });

  assert.deepEqual(result, { ok: true });
  assert.equal(request.headers["mcp-protocol-version"], "2026-07-28");
  assert.equal(request.headers["mcp-method"], "tools/call");
  assert.equal(request.headers["mcp-name"], "akb_help");
  assert.equal(request.body.params._meta["io.modelcontextprotocol/protocolVersion"], "2026-07-28");
  assert.deepEqual(
    request.body.params._meta["io.modelcontextprotocol/clientCapabilities"].experimental[
      "io.dnotitia.akb/vault-skill-preflight"
    ],
    { version: 2 },
  );
});

itAsync("legacy initialize remains local and rejects modern discovery mixing", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  proxy._startBackendMonitor = () => {};

  const initialized = await proxy._handle({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "legacy-test", version: "1" },
    },
  });
  const mixed = await proxy._handle(modernRequest(2, "server/discover"));

  assert.equal(initialized.result.protocolVersion, "2025-06-18");
  assert.equal(initialized.result.serverInfo.version, "2.3.0");
  assert.equal(mixed.error.code, -32022);
});

itAsync("modern clients cannot send initialize or initialized notifications", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  const modern = modernRequest(1, "server/discover");
  proxy._forward = async (msg) => ({
    jsonrpc: "2.0",
    id: msg.id,
    result: { supportedVersions: ["2026-07-28"] },
  });
  await proxy._handle(modern);

  const initialize = await proxy._handle({
    jsonrpc: "2.0",
    id: 2,
    method: "initialize",
    params: { protocolVersion: "2025-06-18", capabilities: {} },
  });
  const initialized = await proxy._handle(modernRequest(3, "notifications/initialized"));

  assert.equal(initialize.error.code, -32022);
  assert.equal(initialized.error.code, -32600);
});

itAsync("legacy and modern tool catalogs share tools but keep modern cache metadata private", async () => {
  const cached = {
    tools: [{ name: "akb_help", inputSchema: { type: "object", properties: {} } }],
    resultType: "complete",
    ttlMs: 300000,
    cacheScope: "public",
  };
  const modern = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  modern._protocolGeneration = "modern";
  modern._cachedTools = cached;
  modern._startBackendMonitor = () => {};
  const legacy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  legacy._protocolGeneration = "legacy";
  legacy._cachedTools = cached;
  legacy._startBackendMonitor = () => {};

  const modernList = await modern._toolsList(1, { _meta: modernMeta });
  const legacyList = await legacy._toolsList(1, {});

  assert.deepEqual(modernList.result.tools, legacyList.result.tools);
  assert.equal(modernList.result.cacheScope, "public");
  assert.equal(legacyList.result.cacheScope, undefined);
  assert.equal(legacyList.result.resultType, undefined);
  assert.equal(legacyList.result._meta, undefined);
});

itAsync("_putFile omits initiate hash, uploads bytes, and returns the canonical confirm shape", async () => {
  const directory = await mkdtemp(join(tmpdir(), "akb-mcp-contract-"));
  const filePath = join(directory, "proxy.bin");
  const data = Buffer.from("proxy upload payload");
  const digest = "d".repeat(64);
  const fileId = "11111111-2222-3333-4444-555555555555";
  const canonical = {
    kind: "file",
    uri: `akb://myvault/coll/proof/file/${fileId}`,
    file_id: fileId,
    vault: "myvault",
    collection: "proof",
    name: "proxy.bin",
    content_hash: digest,
    hash_algorithm: "sha256",
    storage_driver: "fscas",
  };
  await writeFile(filePath, data);
  try {
    const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
    const calls = [];
    proxy._http = async (method, path) => {
      calls.push({ method, path });
      if (calls.length === 1) {
        return { text: JSON.stringify({
          kind: "file", uri: canonical.uri, upload_url: "http://transfer.test/put",
        }) };
      }
      return { text: JSON.stringify(canonical) };
    };
    const uploads = [];
    proxy._uploadToS3 = async (...args) => { uploads.push(args); };

    const result = await proxy._putFile({ vault: "myvault", collection: "proof", file_path: filePath });

    assert.deepEqual(result, canonical);
    assert.equal(uploads.length, 1);
    assert.deepEqual(uploads[0], ["http://transfer.test/put", filePath, data.length, "application/octet-stream"]);
    assert.equal(calls[0].method, "POST");
    assert.match(calls[0].path, /^\/api\/v1\/files\/myvault\/upload\?/);
    assert.ok(!new URL(`http://test${calls[0].path}`).searchParams.has("content_hash"));
    const confirm = new URL(`http://test${calls[1].path}`);
    assert.equal(confirm.searchParams.get("content_hash"), await proxy._sha256File(filePath));
    assert.equal(confirm.searchParams.get("hash_algorithm"), "sha256");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

itAsync("_putImage proxies bounded bytes and returns ready-to-paste Markdown", async () => {
  const directory = await mkdtemp(join(tmpdir(), "akb-mcp-image-"));
  const imagePath = join(directory, "architecture one.png");
  const data = Buffer.from("server-validates-these-image-bytes");
  const assetId = "11111111-2222-4333-8444-555555555555";
  await writeFile(imagePath, data);
  try {
    const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
    const calls = [];
    proxy._http = async (method, path, body, headers) => {
      calls.push({ method, path, body, headers });
      return {
        text: JSON.stringify({
          id: assetId,
          url: `/api/assets/${assetId}`,
          name: "architecture one.png",
          mime_type: "image/png",
          size_bytes: data.length,
          width: 640,
          height: 480,
        }),
      };
    };

    const result = await proxy._putImage({
      parent: "akb://myvault/coll/specs",
      file_path: imagePath,
      alt_text: "Architecture [request path]",
    });

    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "POST");
    assert.equal(
      calls[0].path,
      "/api/v1/assets/myvault?filename=architecture+one.png",
    );
    assert.deepEqual(calls[0].body, data);
    assert.deepEqual(calls[0].headers, { "Content-Type": "image/png" });
    assert.deepEqual(result, {
      kind: "document_image",
      vault: "myvault",
      url: `/api/assets/${assetId}`,
      markdown: `![Architecture \\[request path\\]](/api/assets/${assetId})`,
      name: "architecture one.png",
      mime_type: "image/png",
      size_bytes: data.length,
      width: 640,
      height: 480,
    });
    assert.ok(!("id" in result), "asset UUID stays encapsulated in the stable URL");
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

itAsync("_updateFile stages bytes and forwards both optimistic-concurrency pins", async () => {
  const directory = await mkdtemp(join(tmpdir(), "akb-mcp-replace-"));
  const filePath = join(directory, "replacement.bin");
  const data = Buffer.from("replacement payload");
  const fileId = "11111111-2222-3333-4444-555555555555";
  const replacementId = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
  const uri = `akb://myvault/coll/proof/file/${fileId}`;
  const canonical = {
    kind: "file", uri, vault: "myvault", collection: "proof",
    name: "original.bin", content_hash: "f".repeat(64), version: "etag-new",
    previous_content_hash: "a".repeat(64), previous_version: "etag-old",
    unchanged: false,
  };
  await writeFile(filePath, data);
  try {
    const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
    const calls = [];
    proxy._http = async (method, path) => {
      calls.push({ method, path });
      if (calls.length === 1) {
        return { text: JSON.stringify({
          kind: "file", uri, replacement_id: replacementId,
          upload_url: "http://transfer.test/replace", mime_type: "application/octet-stream",
          unchanged: false,
        }) };
      }
      return { text: JSON.stringify(canonical) };
    };
    const uploads = [];
    proxy._uploadToS3 = async (...uploadArgs) => { uploads.push(uploadArgs); };

    const result = await proxy._updateFile({
      uri, file_path: filePath,
      expected_content_hash: "a".repeat(64), expected_version: "etag-old",
    });

    assert.deepEqual(result, canonical);
    assert.deepEqual(uploads, [[
      "http://transfer.test/replace", filePath, data.length, "application/octet-stream",
    ]]);
    assert.equal(calls.length, 2);
    const initiate = new URL(`http://test${calls[0].path}`);
    const confirm = new URL(`http://test${calls[1].path}`);
    assert.match(initiate.pathname, new RegExp(`/files/myvault/${fileId}/replace$`));
    assert.match(confirm.pathname, new RegExp(`/replace/${replacementId}/confirm$`));
    for (const requestUrl of [initiate, confirm]) {
      assert.equal(requestUrl.searchParams.get("content_hash"), await proxy._sha256File(filePath));
      assert.equal(requestUrl.searchParams.get("expected_content_hash"), "a".repeat(64));
      assert.equal(requestUrl.searchParams.get("expected_version"), "etag-old");
    }
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

itAsync("_putImage rejects unsupported image types before network access", async () => {
  const directory = await mkdtemp(join(tmpdir(), "akb-mcp-image-type-"));
  const imagePath = join(directory, "vector.svg");
  await writeFile(imagePath, "<svg/>");
  try {
    const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
    let networkTouched = false;
    proxy._http = async () => {
      networkTouched = true;
      throw new Error("unexpected network call");
    };
    await assert.rejects(
      () => proxy._putImage({ vault: "myvault", file_path: imagePath }),
      /PNG, JPEG, GIF, or WebP/,
    );
    assert.equal(networkTouched, false);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

itAsync("_updateFile skips transfer when the backend reports identical content", async () => {
  const directory = await mkdtemp(join(tmpdir(), "akb-mcp-replace-skip-"));
  const filePath = join(directory, "same.bin");
  const uri = "akb://myvault/file/11111111-2222-3333-4444-555555555555";
  await writeFile(filePath, Buffer.from("already current"));
  try {
    const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
    let calls = 0;
    const unchanged = { kind: "file", uri, content_hash: "a".repeat(64), unchanged: true };
    proxy._http = async () => {
      calls++;
      return { text: JSON.stringify(unchanged) };
    };
    proxy._uploadToS3 = async () => { throw new Error("upload should have been skipped"); };

    assert.deepEqual(await proxy._updateFile({ uri, file_path: filePath }), unchanged);
    assert.equal(calls, 1);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

itAsync("_discardImage normalizes case variants of canonical asset URLs", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  const assetId = "11111111-2222-4333-8444-555555555555";
  const calls = [];
  proxy._http = async (method, path) => {
    calls.push({ method, path });
    return { text: '{"discarded":true}' };
  };

  const result = await proxy._discardImage({
    vault: "myvault",
    url: `/api/assets/${assetId}`,
  });
  assert.deepEqual(calls, [{
    method: "DELETE",
    path: `/api/v1/assets/myvault/${assetId}`,
  }]);
  assert.equal(result.discarded, true);

  await assert.rejects(
    () => proxy._discardImage({ vault: "myvault", url: `https://example.com/${assetId}` }),
    /Invalid document image URL/,
  );
  calls.length = 0;
  await proxy._discardImage({
    vault: "myvault",
    url: `/API/assets/${assetId.toUpperCase()}`,
  });
  assert.deepEqual(calls, [{
    method: "DELETE",
    path: `/api/v1/assets/myvault/${assetId}`,
  }]);
});

itAsync("_discardImage does not claim a backend no-op deleted anything", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  const assetId = "11111111-2222-4333-8444-555555555555";
  proxy._http = async () => ({ text: '{"discarded":false}' });

  const result = await proxy._discardImage({
    vault: "myvault",
    url: `/api/assets/${assetId}`,
  });

  assert.equal(result.discarded, false);
});

itAsync("_discardImage tolerates an empty legacy response", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  const assetId = "11111111-2222-4333-8444-555555555555";
  proxy._http = async () => ({ text: "  \n" });

  const result = await proxy._discardImage({
    vault: "myvault",
    url: `/api/assets/${assetId}`,
  });

  assert.equal(result.discarded, null);
});

itAsync("_http honors the per-call probe timeout", async () => {
  const server = createServer(() => {});
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const address = server.address();
    assert.equal(typeof address, "object");
    const proxy = new AKBProxy({
      url: `http://127.0.0.1:${address.port}/mcp`,
      pat: "test",
    });
    const started = Date.now();

    await assert.rejects(
      () => proxy._http("GET", "/hang", null, {}, { timeoutMs: 30 }),
      /Request timeout/,
    );

    assert.ok(Date.now() - started < 1000, "probe must not inherit the 300s default");
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});

itAsync("_discardImage surfaces backend lookup failures", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  const assetId = "11111111-2222-4333-8444-555555555555";
  proxy._http = async () => {
    const error = new Error("HTTP 404: Asset not found");
    error.statusCode = 404;
    throw error;
  };

  await assert.rejects(
    () => proxy._discardImage({
      vault: "missing-vault",
      url: `/api/assets/${assetId}`,
    }),
    /HTTP 404/,
  );
});

itAsync("proxy-local first write returns the vault guide before side effects", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  const guide = {
    vault: "myvault", version: "abc123", reason: "first_touch",
    body: "# Owner guide", truncated: false,
  };
  proxy._fileToolSkillPreflight = async () => guide;
  proxy._putImage = async () => {
    throw new Error("write must not run before the guide is applied");
  };

  const response = await proxy._handleFileTool(7, {
    name: "akb_put_image",
    arguments: { vault: "myvault", file_path: "/not-read-before-preflight.png" },
  });
  const body = JSON.parse(response.result.content[0].text);
  assert.equal(body.code, "vault_skill_required");
  assert.equal(body.retryable, true);
  assert.deepEqual(body.vault_skill, guide);
});

itAsync("proxy-local exact retry acknowledges the guide before writing", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  const guide = {
    vault: "myvault", version: "abc123", reason: "first_touch",
    body: "# Owner guide", truncated: false,
  };
  let helpCalls = 0;
  proxy._ensureBackend = async () => true;
  proxy._rpc = async () => ({
    content: [{
      type: "text",
      text: JSON.stringify(
        helpCalls++ === 0 ? { help: "guide", vault_skill: guide } : { help: "guide" },
      ),
    }],
  });
  let writes = 0;
  proxy._putImage = async () => {
    writes++;
    return { kind: "document_image", url: "/api/assets/test" };
  };
  const params = {
    name: "akb_put_image",
    arguments: { vault: "myvault", file_path: "/tmp/example.png" },
  };

  proxy._startBackendMonitor = () => {};
  await proxy._initialize(0, {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "legacy-test", version: "1" },
  });

  const first = await proxy._handle({
    jsonrpc: "2.0", id: 1, method: "tools/call", params,
  });
  const firstBody = JSON.parse(first.result.content[0].text);
  assert.equal(firstBody.code, "vault_skill_required");
  assert.equal(typeof firstBody.vault_skill.ack_token, "string");
  assert.equal(writes, 0);

  const second = await proxy._handle({
    jsonrpc: "2.0", id: 2, method: "tools/call", params,
  });
  const secondBody = JSON.parse(second.result.content[0].text);
  assert.equal(secondBody.kind, "document_image");
  assert.equal(writes, 1);
});

itAsync("proxy-local read attaches the guide to the successful result", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  const guide = {
    vault: "myvault", version: "abc123", reason: "first_touch",
    body: "# Owner guide", truncated: false,
  };
  proxy._fileToolSkillPreflight = async () => guide;
  proxy._getFile = async () => ({ kind: "file", save_to: "/tmp/a.bin" });

  const response = await proxy._handleFileTool(8, {
    name: "akb_get_file",
    arguments: {
      uri: "akb://myvault/file/11111111-2222-3333-4444-555555555555",
      save_to: "/tmp/a.bin",
    },
  });
  const body = JSON.parse(response.result.content[0].text);
  assert.equal(body.kind, "file");
  assert.deepEqual(body.vault_skill, guide);
});

// ── Summary ──────────────────────────────────────────────────────

await Promise.all(pending);
console.log("");
console.log(`  Passed: ${pass}   Failed: ${fail}`);
process.exit(fail > 0 ? 1 : 0);
