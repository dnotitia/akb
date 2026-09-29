import { describe, expect, it } from "vitest";
import {
  changedDocumentDetails,
  documentDetailsEqual,
  documentDetailsFrom,
  isDocumentDetailsValues,
  type DocumentDetailsValues,
} from "@/lib/document-details";

const base: DocumentDetailsValues = {
  summary: "Server summary",
  type: "future-type",
  domain: "engineering",
  tags: ["a", "b"],
  status: "future-status",
};

describe("document details", () => {
  it("hydrates optional metadata without substituting document type or status defaults", () => {
    expect(documentDetailsFrom({ summary: null, domain: null, tags: null })).toEqual({
      summary: "", type: "", domain: "", tags: [], status: "",
    });
    expect(documentDetailsFrom(base)).toEqual(base);
    const hydrated = documentDetailsFrom(base);
    hydrated.tags.push("local");
    expect(base.tags).toEqual(["a", "b"]);
  });

  it("preserves raw server strings when hydrating", () => {
    expect(documentDetailsFrom({ ...base, summary: "  summary  ", domain: " domain " })).toMatchObject({
      summary: "  summary  ", domain: " domain ",
    });
  });

  it("treats text trimming and empty serialization as equal without rewriting server values", () => {
    expect(documentDetailsEqual(base, { ...base, summary: " Server summary ", domain: " engineering " })).toBe(true);
    expect(documentDetailsEqual(documentDetailsFrom({}), documentDetailsFrom({ summary: null, domain: "  " }))).toBe(true);
    expect(changedDocumentDetails({ ...base, summary: " Server summary ", domain: " engineering " }, base)).toEqual({});
  });

  it("compares type, status, and complete tag arrays without lossy joining or coercion", () => {
    expect(documentDetailsEqual(base, { ...base, type: "note" })).toBe(false);
    expect(documentDetailsEqual(base, { ...base, status: "draft" })).toBe(false);
    expect(documentDetailsEqual(base, { ...base, tags: ["b", "a"] })).toBe(false);
    expect(documentDetailsEqual({ ...base, tags: ["a\0b"] }, base)).toBe(false);
  });

  it("recognizes the server's Unicode normalization without rewriting local snapshots", () => {
    const local = { ...base, summary: "cafe\u0301", domain: "cafe\u0301", tags: ["cafe\u0301"] };
    const saved = { ...base, summary: "café", domain: "café", tags: ["café"] };

    expect(documentDetailsEqual(local, saved)).toBe(true);
    expect(changedDocumentDetails(local, saved)).toEqual({});
    expect(documentDetailsEqual({ ...base, summary: "a\0b" }, { ...base, summary: "ab" })).toBe(true);
    expect(local.summary).toBe("cafe\u0301");
    expect(local.tags).toEqual(["cafe\u0301"]);
  });

  it("sends only changed metadata and never writes body, title, or unchanged future values", () => {
    expect(changedDocumentDetails({ ...base, summary: "  Revised summary  " }, base)).toEqual({ summary: "Revised summary" });
    expect(changedDocumentDetails({ ...base, domain: " product ", tags: [] }, base)).toEqual({ domain: "product", tags: [] });
    expect(changedDocumentDetails(base, base)).toEqual({});
  });

  it("explicitly clears nullable text fields and changes lifecycle fields only when edited", () => {
    expect(changedDocumentDetails({ ...base, summary: "  ", domain: "", type: "note", status: "active" }, base)).toEqual({
      summary: null, domain: null, type: "note", status: "active",
    });
  });

  it("accepts unknown enum values in stored snapshots but rejects malformed fields", () => {
    expect(isDocumentDetailsValues(base)).toBe(true);
    expect(isDocumentDetailsValues({ ...base, type: "", status: "" })).toBe(true);
    expect(isDocumentDetailsValues(null)).toBe(false);
    expect(isDocumentDetailsValues([])).toBe(false);
    expect(isDocumentDetailsValues({ ...base, tags: [123] })).toBe(false);
    expect(isDocumentDetailsValues({ ...base, summary: null })).toBe(false);
    expect(isDocumentDetailsValues({ summary: "incomplete" })).toBe(false);
  });
});
