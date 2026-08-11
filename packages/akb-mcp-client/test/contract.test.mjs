// Contract test for the proxy's destructure patterns.
// Runs against fixed JSON shapes the backend now returns post-envelope
// adoption (kind/id/items/vault). No real network calls — we verify
// the proxy's parsing assumptions still hold.
//
// Run with: node packages/akb-mcp-client/test/contract.test.mjs

import assert from "node:assert/strict";
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

it("_getFile destructures name + download_url + size_bytes + hash fields", () => {
  const { name: filename, download_url, size_bytes, content_hash, hash_algorithm } = JSON.parse(downloadResp);
  assert.equal(filename, "file.bin");
  assert.match(download_url, /^https:/);
  assert.equal(size_bytes, 4096);
  assert.match(content_hash, /^[0-9a-f]{64}$/);
  assert.equal(hash_algorithm, "sha256");
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

itAsync("_discardImage normalizes case variants of canonical asset URLs", async () => {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  const assetId = "11111111-2222-4333-8444-555555555555";
  const calls = [];
  proxy._http = async (method, path) => {
    calls.push({ method, path });
    return { text: "" };
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

// ── Summary ──────────────────────────────────────────────────────

await Promise.all(pending);
console.log("");
console.log(`  Passed: ${pass}   Failed: ${fail}`);
process.exit(fail > 0 ? 1 : 0);
