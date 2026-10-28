import { describe, expect, it } from "vitest";
import { readingDocumentHref, readingDocumentKey, readingReference } from "@/lib/reading-workspace";

describe("reading document identity and link boundary", () => {
  it("uses vault/path/version identity, independent from view, title and fragment", () => {
    const original = { vault: "a", path: "folder/한글.md", version: "abc" };
    expect(readingDocumentKey(original)).toBe(readingDocumentKey({ ...original }));
    expect(readingDocumentKey(original)).not.toBe(readingDocumentKey({ ...original, vault: "b" }));
    expect(readingDocumentKey(original)).not.toBe(readingDocumentKey({ ...original, version: "def" }));
    const href = readingDocumentHref("a", original.path, "?commit=abc#section");
    expect(readingReference(href, "http://localhost:3000")).toMatchObject({ ...original, href });
  });
  it.each(["https://external.example/vault/a/doc/a.md", "javascript:alert(1)", "/vault/a/table/t", "/vault/a/file/f", "/vault/a/doc/a.md?view=edit", "/vault/a/doc/a.md?view=diff&commit=abc", "/vault/a/doc/%XX"])("keeps %s outside document preview interception", href => {
    expect(readingReference(href, "http://localhost:3000")).toBeNull();
  });
});
