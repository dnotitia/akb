import { describe, expect, it } from "vitest";

import type { DocumentDiff } from "@/lib/api";
import {
  DOCUMENT_DIFF_MAX_LINES,
  inspectDocumentDiffPatch,
  normalizeDocumentDiffPatch,
  parseDocumentDiffPatch,
} from "@/lib/document-diff";

function response(overrides: Partial<DocumentDiff> = {}): DocumentDiff {
  return {
    kind: "document_diff",
    file: "notes/guide.md",
    commit: "abcdef123456", // pragma: allowlist secret — synthetic Git commit
    type: "modified",
    diff: "",
    ...overrides,
  };
}

describe("document diff parser", () => {
  it("maps unified changes to aligned old/new line numbers", () => {
    const parsed = parseDocumentDiffPatch(response({
      diff: [
        "--- a/notes/guide.md",
        "+++ b/notes/guide.md",
        "@@ -1,2 +1,2 @@",
        "-Old title",
        "+New title",
        " body",
      ].join("\n"),
    }));

    expect(parsed).toMatchObject({ additions: 1, deletions: 1 });
    expect(parsed.hunks).toHaveLength(1);
    expect(parsed.rows.map((row) => ({
      kind: row.kind,
      oldLine: row.oldLine,
      newLine: row.newLine,
      content: row.content,
    }))).toEqual([
      { kind: "hunk", oldLine: null, newLine: null, content: "@@ -1,2 +1,2 @@" },
      { kind: "removed", oldLine: 1, newLine: null, content: "Old title" },
      { kind: "added", oldLine: null, newLine: 1, content: "New title" },
      { kind: "context", oldLine: 2, newLine: 2, content: "body" },
    ]);
  });

  it("normalizes native headerless create revisions without changing display text", () => {
    const diff = response({
      type: "added",
      diff: "+# Guide\n+\n+Body",
    });

    expect(normalizeDocumentDiffPatch(diff)).toContain("--- /dev/null");
    const parsed = parseDocumentDiffPatch(diff);
    expect(parsed.additions).toBe(3);
    expect(parsed.deletions).toBe(0);
    expect(parsed.rows.filter((row) => row.kind === "added")).toHaveLength(3);
    expect(diff.diff).toBe("+# Guide\n+\n+Body");
  });

  it("normalizes native headerless delete revisions", () => {
    const parsed = parseDocumentDiffPatch(response({
      type: "deleted",
      diff: "-First\n-Second",
    }));

    expect(parsed.additions).toBe(0);
    expect(parsed.deletions).toBe(2);
    expect(parsed.rows.filter((row) => row.kind === "removed")).toHaveLength(2);
  });

  it("rejects a non-empty unsupported patch instead of rendering misleading rows", () => {
    expect(() => parseDocumentDiffPatch(response({ diff: "not a unified diff" }))).toThrow(
      /cannot display/i,
    );
  });

  it("guards the parser before an excessive line count is mounted", () => {
    const patch = Array.from({ length: DOCUMENT_DIFF_MAX_LINES + 1 }, () => "+x").join("\n");
    const guard = inspectDocumentDiffPatch(patch);

    expect(guard.lineCount).toBe(DOCUMENT_DIFF_MAX_LINES + 1);
    expect(guard.tooLarge).toBe(true);
  });
});
