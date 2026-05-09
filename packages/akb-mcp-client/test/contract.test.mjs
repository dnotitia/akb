// Contract test for the proxy's destructure patterns.
// Runs against fixed JSON shapes the backend now returns post-envelope
// adoption (kind/id/items/vault). No real network calls — we verify
// the proxy's parsing assumptions still hold.
//
// Run with: node packages/akb-mcp-client/test/contract.test.mjs

import assert from "node:assert/strict";

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

// ── Fixtures: representative backend envelope responses ──────────

const initiateUploadResp = JSON.stringify({
  kind: "file",
  id: "11111111-2222-3333-4444-555555555555",
  vault: "myvault",
  upload_url: "https://s3.example/presigned",
  s3_key: "myvault/abc123_file.bin",
  expires_in: 3600,
});

const downloadResp = JSON.stringify({
  kind: "file",
  id: "11111111-2222-3333-4444-555555555555",
  name: "file.bin",
  download_url: "https://s3.example/presigned-get",
  mime_type: "application/octet-stream",
  size_bytes: 4096,
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

it("_putFile destructures id + upload_url", () => {
  const { id: fileId, upload_url } = JSON.parse(initiateUploadResp);
  assert.equal(typeof fileId, "string");
  assert.equal(fileId.length, 36);
  assert.match(upload_url, /^https:/);
});

it("_getFile destructures name + download_url + size_bytes", () => {
  const { name: filename, download_url, size_bytes } = JSON.parse(downloadResp);
  assert.equal(filename, "file.bin");
  assert.match(download_url, /^https:/);
  assert.equal(size_bytes, 4096);
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
  for (const k of ["kind", "id", "upload_url", "s3_key", "expires_in"]) {
    assert.ok(k in init, `initiate response missing ${k}`);
  }
  const dl = JSON.parse(downloadResp);
  for (const k of ["kind", "id", "name", "download_url", "size_bytes"]) {
    assert.ok(k in dl, `download response missing ${k}`);
  }
});

// ── Summary ──────────────────────────────────────────────────────

console.log("");
console.log(`  Passed: ${pass}   Failed: ${fail}`);
process.exit(fail > 0 ? 1 : 0);
