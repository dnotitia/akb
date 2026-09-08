// Opt-in usage record: `AKB_MCP_USAGE_LOG=<path>` appends one JSON line
// per tools/call. Unset, the proxy must behave exactly as before — no
// file, no throw, nothing to notice.
//
// Run with: node packages/akb-mcp-client/test/usage-log.test.mjs

import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AKBProxy } from "../lib/proxy.mjs";

let pass = 0;
let fail = 0;
// Sequential on purpose: these cases set and unset AKB_MCP_USAGE_LOG, which
// is process-global. Running them concurrently would have one test's env
// decide another test's outcome.
const tests = [];
function itAsync(name, fn) {
  tests.push([name, fn]);
}

const ORIGINAL_LOG = process.env.AKB_MCP_USAGE_LOG;

function backedBy(text) {
  const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
  proxy._ensureBackend = async () => true;
  proxy._rpc = async () => ({ content: [{ type: "text", text }] });
  return proxy;
}

function searchCall(id) {
  return {
    jsonrpc: "2.0",
    id,
    method: "tools/call",
    params: { name: "akb_search", arguments: { query: "product-api seam" } },
  };
}

async function withLogPath(fn) {
  const directory = await mkdtemp(join(tmpdir(), "akb-mcp-usage-"));
  const path = join(directory, "usage.jsonl");
  process.env.AKB_MCP_USAGE_LOG = path;
  try {
    return await fn(path);
  } finally {
    if (ORIGINAL_LOG === undefined) delete process.env.AKB_MCP_USAGE_LOG;
    else process.env.AKB_MCP_USAGE_LOG = ORIGINAL_LOG;
    await rm(directory, { recursive: true, force: true });
  }
}

async function readLines(path) {
  const raw = await readFile(path, "utf8");
  return raw.split("\n").filter((line) => line.length > 0).map((line) => JSON.parse(line));
}

// ── off by default ───────────────────────────────────────────────

itAsync("writes nothing when AKB_MCP_USAGE_LOG is unset", async () => {
  const directory = await mkdtemp(join(tmpdir(), "akb-mcp-usage-off-"));
  const path = join(directory, "usage.jsonl");
  const previous = process.env.AKB_MCP_USAGE_LOG;
  delete process.env.AKB_MCP_USAGE_LOG;
  try {
    const proxy = backedBy(JSON.stringify({ kind: "search", results: [] }));
    const response = await proxy._handle(searchCall(1));

    assert.equal(response.result.content[0].type, "text");
    assert.equal(existsSync(path), false, "no usage file may be created");
  } finally {
    if (previous !== undefined) process.env.AKB_MCP_USAGE_LOG = previous;
    await rm(directory, { recursive: true, force: true });
  }
});

// ── one line per call when on ────────────────────────────────────

itAsync("appends one record per tools/call with the documented fields", async () => {
  await withLogPath(async (path) => {
    const body = JSON.stringify({ kind: "search", results: [{ uri: "akb://v/doc/a.md" }] });
    const proxy = backedBy(body);

    const response = await proxy._handle(searchCall(1));
    const [record] = await readLines(path);

    assert.deepEqual(
      Object.keys(record).sort(),
      ["error", "latency_ms", "result_bytes", "text_chars", "tool", "ts"],
    );
    assert.equal(record.tool, "akb_search");
    assert.equal(record.text_chars, body.length);
    assert.equal(record.error, false);
    assert.equal(
      record.result_bytes,
      Buffer.byteLength(JSON.stringify(response.result), "utf8"),
    );
    assert.ok(record.latency_ms >= 0);
    assert.ok(!Number.isNaN(Date.parse(record.ts)), "ts must be a timestamp");
  });
});

itAsync("sums text_chars across every text block, in characters", async () => {
  await withLogPath(async (path) => {
    const parts = ["첫 번째 블록입니다.", "second block", "세 번째 블록"];
    const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
    proxy._ensureBackend = async () => true;
    proxy._rpc = async () => ({
      content: [
        { type: "text", text: parts[0] },
        { type: "image", data: "ignored-non-text-block" },
        { type: "text", text: parts[1] },
        { type: "text", text: parts[2] },
      ],
    });

    await proxy._handle(searchCall(1));

    const [record] = await readLines(path);
    assert.equal(record.text_chars, parts.join("").length);
    // Bytes and characters part company on Korean text, which is the point
    // of carrying both.
    assert.ok(record.result_bytes > record.text_chars);
  });
});

itAsync("records concurrent calls as whole lines, one per call", async () => {
  await withLogPath(async (path) => {
    const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
    proxy._ensureBackend = async () => true;
    proxy._rpc = async () => {
      await new Promise((resolve) => setTimeout(resolve, 1));
      return { content: [{ type: "text", text: "x".repeat(500) }] };
    };

    await Promise.all(
      Array.from({ length: 12 }, (_, i) => proxy._handle(searchCall(i + 1))),
    );

    const records = await readLines(path);
    assert.equal(records.length, 12, "one line per call, none interleaved");
    assert.ok(records.every((r) => r.tool === "akb_search" && r.text_chars === 500));
  });
});

// ── failures are measured too ────────────────────────────────────

itAsync("records a call that threw, with error true and no payload", async () => {
  await withLogPath(async (path) => {
    const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
    proxy._forward = async () => {
      throw new Error("backend unreachable");
    };

    await assert.rejects(() => proxy._handle(searchCall(1)), /backend unreachable/);

    const [record] = await readLines(path);
    assert.equal(record.tool, "akb_search");
    assert.equal(record.error, true);
    assert.equal(record.result_bytes, 0);
    assert.ok(record.latency_ms >= 0);
  });
});

itAsync("marks an isError result as an error without losing its size", async () => {
  await withLogPath(async (path) => {
    const body = JSON.stringify({ error: "vault not found", code: "not_found" });
    const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
    proxy._ensureBackend = async () => true;
    proxy._rpc = async () => ({ content: [{ type: "text", text: body }], isError: true });

    await proxy._handle(searchCall(1));

    const [record] = await readLines(path);
    assert.equal(record.error, true);
    assert.equal(record.text_chars, body.length);
    assert.ok(record.result_bytes > 0);
  });
});

// ── what the record must not contain ─────────────────────────────

itAsync("never writes the call arguments", async () => {
  await withLogPath(async (path) => {
    const secret = "canary-8f3a1c-not-for-the-log"; // pragma: allowlist secret
    const proxy = backedBy(JSON.stringify({ kind: "search", results: [] }));

    await proxy._handle({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: "akb_search",
        arguments: { query: secret, vault: secret, file_path: `/tmp/${secret}` },
      },
    });

    const raw = await readFile(path, "utf8");
    assert.ok(!raw.includes(secret), "arguments must never reach the usage log");
    const [record] = await readLines(path);
    assert.equal(record.tool, "akb_search");
  });
});

itAsync("appends one line per call, not one per session", async () => {
  await withLogPath(async (path) => {
    const proxy = backedBy(JSON.stringify({ kind: "search", results: [] }));

    await proxy._handle(searchCall(1));
    await proxy._handle(searchCall(2));
    await proxy._handle(searchCall(3));

    const records = await readLines(path);
    assert.equal(records.length, 3);
    assert.ok(records.every((r) => r.tool === "akb_search"));
  });
});

itAsync("records a bigger payload as more bytes", async () => {
  await withLogPath(async (path) => {
    const small = backedBy(JSON.stringify({ sections: ["a"] }));
    const large = backedBy(JSON.stringify({ sections: Array(50).fill("a".repeat(200)) }));

    await small._handle(searchCall(1));
    await large._handle(searchCall(2));

    const [first, second] = await readLines(path);
    assert.ok(second.result_bytes > first.result_bytes * 10);
    assert.ok(second.text_chars > first.text_chars);
  });
});

itAsync("measures a proxy-local file tool too", async () => {
  await withLogPath(async (path) => {
    const proxy = new AKBProxy({ url: "http://akb.test/mcp", pat: "test" });
    proxy._fileToolSkillPreflight = async () => null;
    proxy._getFile = async () => ({ kind: "file", save_to: "/tmp/a.bin" });

    await proxy._handle({
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: "akb_get_file",
        arguments: {
          uri: "akb://myvault/file/11111111-2222-3333-4444-555555555555",
          save_to: "/tmp/a.bin",
        },
      },
    });

    const [record] = await readLines(path);
    assert.equal(record.tool, "akb_get_file");
    assert.ok(record.result_bytes > 0);
  });
});

itAsync("does not record a non-tools/call request", async () => {
  await withLogPath(async (path) => {
    const proxy = backedBy("{}");

    await proxy._handle({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} });

    assert.equal(existsSync(path), false, "only tools/call is measured");
  });
});

// ── never in the way ─────────────────────────────────────────────

itAsync("an unwritable path warns once and never fails the call", async () => {
  const previous = process.env.AKB_MCP_USAGE_LOG;
  process.env.AKB_MCP_USAGE_LOG = join(tmpdir(), "akb-mcp-usage-missing-dir", "x.jsonl");
  const written = [];
  const originalWrite = process.stderr.write.bind(process.stderr);
  process.stderr.write = (chunk) => { written.push(String(chunk)); return true; };
  try {
    const proxy = backedBy(JSON.stringify({ kind: "search", results: [] }));

    const first = await proxy._handle(searchCall(1));
    const second = await proxy._handle(searchCall(2));

    assert.equal(first.result.content[0].type, "text");
    assert.equal(second.result.content[0].type, "text");
    assert.equal(
      written.filter((line) => line.includes("AKB_MCP_USAGE_LOG")).length,
      1,
      "a broken sink warns once, not once per call",
    );
  } finally {
    process.stderr.write = originalWrite;
    if (previous === undefined) delete process.env.AKB_MCP_USAGE_LOG;
    else process.env.AKB_MCP_USAGE_LOG = previous;
  }
});

// ── Summary ──────────────────────────────────────────────────────

for (const [name, fn] of tests) {
  try {
    await fn();
    pass++;
    console.log(`  ✓ ${name}`);
  } catch (e) {
    fail++;
    console.log(`  ✗ ${name}: ${e.message}`);
  }
}
console.log("");
console.log(`  Passed: ${pass}   Failed: ${fail}`);
process.exit(fail > 0 ? 1 : 0);
