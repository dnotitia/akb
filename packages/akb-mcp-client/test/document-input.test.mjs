import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { AKBProxy } from "../lib/proxy.mjs";

const modernMeta = {
  "io.modelcontextprotocol/protocolVersion": "2026-07-28",
  "io.modelcontextprotocol/clientCapabilities": {},
  "io.modelcontextprotocol/clientInfo": { name: "document-input-test", version: "1" },
};

function request(name, args, generation) {
  return {
    jsonrpc: "2.0", id: 1, method: "tools/call",
    params: { name, arguments: args, ...(generation === "modern" ? { _meta: modernMeta } : {}) },
  };
}

for (const generation of ["legacy", "modern"]) {
  for (const name of ["akb_put", "akb_update"]) {
    test(`${generation} ${name}: rejects invalid body inputs before file reads or forwarding`, async () => {
      const proxy = new AKBProxy({ url: "http://127.0.0.1:1", pat: "test" });
      proxy._resolveFileToContent = () => assert.fail("must not read a file");
      proxy._forward = async () => assert.fail("must not forward invalid input");
      const cases = [
        ...["", null, false, 0, [], {}].map((file) => [ { file }, "/file" ]),
        ...["", null, "body", false, 0, [], {}].map((content) => [
          { file: "/not-read.md", content }, "/content",
        ]),
        ...[false, 0, [], {}].map((content) => [{ content }, "/content"]),
        ...(name === "akb_put" ? [[{}, "/content"], [{ content: null }, "/content"]] : []),
      ];
      for (const [input, field] of cases) {
        const response = await proxy._handle(request(name, {
          vault: "v", title: "title", uri: "akb://v/doc/note.md", ...input,
        }, generation));
        assert.equal(response.result.isError, true);
        const payload = JSON.parse(response.result.content[0].text);
        assert.equal(payload.code, "invalid_argument");
        assert.equal(payload.details.field, field);
        assert.equal(JSON.stringify(payload).includes("/not-read.md"), false);
      }
    });
  }

  test(`${generation}: metadata, null, empty body and original-read tokens survive forwarding`, async () => {
    const proxy = new AKBProxy({ url: "http://127.0.0.1:1", pat: "test" });
    proxy._resolveFileToContent = () => assert.fail("must not read a file");
    const forwarded = [];
    proxy._forward = async (msg) => {
      forwarded.push(msg.params.arguments);
      return { jsonrpc: "2.0", id: msg.id, result: { content: [] } };
    };
    const inputs = [
      { uri: "akb://v/doc/note.md", title: "renamed", summary: "", domain: "", tags: [] },
      { uri: "akb://v/doc/note.md", content: null, summary: "keep body" },
      { uri: "akb://v/doc/note.md", content: "", expected_commit: "a".repeat(40), expected_content_hash: "b".repeat(64) },
      { uri: "akb://v/doc/note.md", title: "pinned metadata", expected_commit: "a".repeat(40), _vault_skill_ack: "test-ack" },
    ];
    for (const input of inputs) await proxy._handle(request("akb_update", input, generation));
    assert.deepEqual(forwarded, inputs);
  });

  test(`${generation}: file creation and empty-file replacement preserve metadata and both predicates`, async () => {
    const directory = await mkdtemp(join(tmpdir(), "akb-document-input-"));
    try {
      const file = join(directory, "draft.md");
      const proxy = new AKBProxy({ url: "http://127.0.0.1:1", pat: "test" });
      const forwarded = [];
      proxy._forward = async (msg) => {
        forwarded.push(msg.params.arguments);
        return { jsonrpc: "2.0", id: msg.id, result: { content: [] } };
      };
      await writeFile(file, "# 한글 draft\n");
      await proxy._handle(request("akb_put", { vault: "v", title: "title", file }, generation));
      assert.deepEqual(forwarded[0], { vault: "v", title: "title", content: "# 한글 draft\n" });
      await writeFile(file, "");
      const predicates = { expected_commit: "a".repeat(40), expected_content_hash: "b".repeat(64) };
      await proxy._handle(request("akb_update", {
        uri: "akb://v/doc/note.md", file, tags: [], ...predicates,
      }, generation));
      assert.deepEqual(forwarded[1], { uri: "akb://v/doc/note.md", content: "", tags: [], ...predicates });
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  });
}
