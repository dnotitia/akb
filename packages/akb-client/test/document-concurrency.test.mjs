import assert from "node:assert/strict";
import { test } from "vitest";

import { createClient } from "../src/index.js";

test("document recovery retains the old draft until the caller reapplies intent to the reviewed latest read", async () => {
  const path = "guides/readme.md";
  const originalCommit = "a".repeat(40);
  const latestCommit = "b".repeat(40);
  const originalBody = "# Readme\n\nOwner: TBD\n";
  const concurrentAddition = "\n## Notes\n\nKeep this concurrent addition.\n";
  let current = {
    kind: "document",
    uri: `akb://reef/doc/${path}`,
    vault: "reef",
    path,
    content: originalBody,
    current_commit: originalCommit,
    title: "Readme",
  };
  const calls = [];
  const docs = createClient("https://akb.test/api/v1", {
    fetch: async (input, init) => {
      assert.equal(String(input), `https://akb.test/api/v1/documents/reef/${path}`);
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ method, body });
      if (method === "GET") return Response.json(current);
      assert.equal(method, "PATCH");
      if (body.expected_commit !== current.current_commit) {
        return Response.json({ code: "conflict", message: "Document revision changed" }, { status: 409 });
      }
      current = { ...current, content: body.content, current_commit: "c".repeat(40) };
      return Response.json({ kind: "document_write", uri: current.uri, commit_hash: current.current_commit });
    },
  }).vault("reef").docs;

  const original = (await docs.get(path)).throwOnError().data;
  const oldText = "Owner: TBD";
  const newText = "Owner: Mina";
  const draft = original.content.replace(oldText, newText);
  current = {
    ...current,
    content: `${originalBody}${concurrentAddition}`,
    current_commit: latestCommit,
    title: "Concurrent title",
  };

  const conflicted = await docs.update(path, { content: draft, expectedCommit: original.current_commit });
  assert.equal(conflicted.error?.code, "conflict");
  assert.equal(conflicted.error?.status, 409);
  assert.deepEqual(calls.map(({ method }) => method), ["GET", "PATCH"]);
  assert.equal(current.content, `${originalBody}${concurrentAddition}`);

  const latest = (await docs.get(path)).throwOnError().data;
  // Reading latest is not a merge: both the original base and old draft stay intact.
  assert.equal(original.content, originalBody);
  assert.equal(original.current_commit, originalCommit);
  assert.equal(draft, "# Readme\n\nOwner: Mina\n");
  assert.equal(draft.includes(concurrentAddition), false);
  assert.equal(latest.content.includes(concurrentAddition), true);
  assert.deepEqual(calls.map(({ method }) => method), ["GET", "PATCH", "GET"]);

  // A fresh read must not silently replace an explicitly supplied old condition.
  const stillStale = await docs.update(path, { content: draft, expectedCommit: original.current_commit });
  assert.equal(stillStale.error?.code, "conflict");
  assert.equal(calls.at(-1).body.expected_commit, originalCommit);

  // After reviewing base, draft, and latest, the caller reapplies only its owner change.
  assert.equal(latest.content.split(oldText).length, 2);
  const composedDraft = latest.content.replace(oldText, newText);
  const resolved = await docs.update(path, { content: composedDraft, expectedCommit: latest.current_commit });
  assert.equal(resolved.throwOnError().data.kind, "document_write");
  assert.equal(current.content, `${draft}${concurrentAddition}`);
  assert.equal(current.title, "Concurrent title");
  assert.deepEqual(calls.at(-1).body, { content: composedDraft, expected_commit: latestCommit });
  assert.deepEqual(calls.map(({ method }) => method), ["GET", "PATCH", "GET", "PATCH", "PATCH"]);
});
