import { describe, expect, it } from "vitest";
import { planDocumentConflict, type DocumentConflictSnapshot } from "@/lib/document-conflict";
import { documentDetailsFrom } from "@/lib/document-details";

const base: DocumentConflictSnapshot = { label: "Base", commit: "r0", title: "Note", body: "Original", details: documentDetailsFrom({ summary: "Before", tags: ["a"] }) };
const snapshot = (change: Partial<DocumentConflictSnapshot>) => ({ ...base, ...change });

describe("document field conflict planning", () => {
  it("preserves remote body and local title when fields are independent", () => {
    const plan = planDocumentConflict(base, snapshot({ title: "My title" }), snapshot({ body: "Remote body" }));
    expect(plan.conflicts).toEqual([]);
    expect(plan.result).toMatchObject({ title: "My title", body: "Remote body" });
  });
  it("keeps a local body edit alongside remote metadata", () => {
    const latest = snapshot({ details: documentDetailsFrom({ summary: "Remote", tags: ["a"] }) });
    const plan = planDocumentConflict(base, snapshot({ body: "My body" }), latest);
    expect(plan.result).toMatchObject({ body: "My body", details: { summary: "Remote" } });
    expect(plan.conflicts).toEqual([]);
  });
  it("requires review when both sides change the body and starts with latest", () => {
    const plan = planDocumentConflict(base, snapshot({ body: "Mine" }), snapshot({ body: "Theirs" }));
    expect(plan.conflicts).toContain("body");
    expect(plan.result.body).toBe("Theirs");
  });
  it("does not flag identical changes, including clearing the body", () => {
    const plan = planDocumentConflict(base, snapshot({ body: "" }), snapshot({ body: "" }));
    expect(plan.conflicts).toEqual([]);
    expect(plan.result.body).toBe("");
  });
  it("does not union tags or overwrite a conflicting metadata field", () => {
    const plan = planDocumentConflict(base,
      snapshot({ details: { ...base.details!, tags: [], summary: "Mine" } }),
      snapshot({ details: { ...base.details!, tags: ["a", "remote"], summary: "Theirs" } }));
    expect(plan.conflicts).toEqual(expect.arrayContaining(["tags", "summary"]));
    expect(plan.result.details.tags).toEqual(["a", "remote"]);
    expect(plan.result.details.summary).toBe("Theirs");
    expect(base.details!.tags).toEqual(["a"]);
  });
  it("combines separate metadata fields and uses existing normalization", () => {
    const plan = planDocumentConflict(base,
      snapshot({ title: " Note ", details: { ...base.details!, summary: "My summary" } }),
      snapshot({ details: { ...base.details!, domain: "Remote domain" } }));
    expect(plan.conflicts).toEqual([]);
    expect(plan.result.details).toMatchObject({ summary: "My summary", domain: "Remote domain" });
  });
});
