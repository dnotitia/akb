import parseDiff from "parse-diff";

import type { DocumentDiff } from "@/lib/api";

export const DOCUMENT_DIFF_MAX_BYTES = 2 * 1024 * 1024;
export const DOCUMENT_DIFF_MAX_LINES = 20_000;
export const DOCUMENT_DIFF_VIRTUALIZE_THRESHOLD = 50;

export type DocumentDiffLineKind = "hunk" | "context" | "added" | "removed";

export interface DocumentDiffLine {
  id: string;
  kind: DocumentDiffLineKind;
  content: string;
  oldLine: number | null;
  newLine: number | null;
  hunkIndex: number;
}
export interface DocumentDiffHunk {
  index: number;
  header: string;
  rowIndex: number;
  oldStart: number;
  newStart: number;
}

export interface ParsedDocumentDiff {
  rows: DocumentDiffLine[];
  hunks: DocumentDiffHunk[];
  additions: number;
  deletions: number;
}

export interface DocumentDiffGuard {
  byteCount: number;
  lineCount: number;
  tooLarge: boolean;
}

export function inspectDocumentDiffPatch(patch: string): DocumentDiffGuard {
  const byteCount = new TextEncoder().encode(patch).byteLength;
  let lineCount = patch.length === 0 ? 0 : 1;
  for (let index = 0; index < patch.length; index += 1) {
    if (patch.charCodeAt(index) === 10) lineCount += 1;
  }
  return {
    byteCount,
    lineCount,
    tooLarge:
      byteCount > DOCUMENT_DIFF_MAX_BYTES ||
      lineCount > DOCUMENT_DIFF_MAX_LINES,
  };
}

/**
 * Native create/delete revisions intentionally return a compact stream of
 * `+` or `-` lines without Git headers. `parse-diff` expects a complete
 * unified patch, so add synthetic headers for parsing only. The untouched
 * response remains the source for Copy.
 */
export function normalizeDocumentDiffPatch(diff: DocumentDiff): string {
  const patch = diff.diff.replace(/\r\n?/g, "\n");
  if (!patch || /^@@\s/m.test(patch) || /^---\s/m.test(patch)) return patch;

  const lines = patch.split("\n");
  const safeFile = diff.file || "document.md";
  if (diff.type === "added" && lines.every((line) => line.startsWith("+"))) {
    return [
      "--- /dev/null",
      `+++ b/${safeFile}`,
      `@@ -0,0 +1,${lines.length} @@`,
      patch,
    ].join("\n");
  }
  if (diff.type === "deleted" && lines.every((line) => line.startsWith("-"))) {
    return [
      `--- a/${safeFile}`,
      "+++ /dev/null",
      `@@ -1,${lines.length} +0,0 @@`,
      patch,
    ].join("\n");
  }
  return patch;
}

export function parseDocumentDiffPatch(diff: DocumentDiff): ParsedDocumentDiff {
  const normalized = normalizeDocumentDiffPatch(diff);
  if (!normalized) {
    return { rows: [], hunks: [], additions: 0, deletions: 0 };
  }

  const files = parseDiff(normalized);
  const rows: DocumentDiffLine[] = [];
  const hunks: DocumentDiffHunk[] = [];
  let additions = 0;
  let deletions = 0;

  for (const file of files) {
    additions += file.additions;
    deletions += file.deletions;
    for (const chunk of file.chunks) {
      const hunkIndex = hunks.length;
      hunks.push({
        index: hunkIndex,
        header: chunk.content,
        rowIndex: rows.length,
        oldStart: chunk.oldStart,
        newStart: chunk.newStart,
      });
      rows.push({
        id: `hunk-${hunkIndex}`,
        kind: "hunk",
        content: chunk.content,
        oldLine: null,
        newLine: null,
        hunkIndex,
      });

      for (const change of chunk.changes) {
        const kind: DocumentDiffLineKind =
          change.type === "add"
            ? "added"
            : change.type === "del"
              ? "removed"
              : "context";
        const oldLine =
          change.type === "normal" ? change.ln1 : change.type === "del" ? change.ln : null;
        const newLine =
          change.type === "normal" ? change.ln2 : change.type === "add" ? change.ln : null;
        rows.push({
          id: `${hunkIndex}-${rows.length}-${kind}`,
          kind,
          content: change.content.slice(1),
          oldLine,
          newLine,
          hunkIndex,
        });
      }
    }
  }

  if (rows.length === 0 && diff.diff.trim()) {
    throw new Error("The server returned a diff format this client cannot display.");
  }

  return { rows, hunks, additions, deletions };
}
