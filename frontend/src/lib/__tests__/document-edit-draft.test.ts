import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearDocumentEditDraft,
  documentEditDraftStorageKey,
  loadDocumentEditDraft,
  saveDocumentEditDraft,
  type DocumentEditDraftInput,
} from "@/lib/document-draft";

const USER = "user-1";
const VAULT = "team";
const DOCUMENT = "akb://team/coll/notes/doc/hello.md";

function draft(overrides: Partial<DocumentEditDraftInput> = {}): DocumentEditDraftInput {
  return {
    draftId: "draft-1",
    tabId: "tab-1",
    userId: USER,
    vault: VAULT,
    document: DOCUMENT,
    baseCommit: "base-commit",
    baseTitle: "Original title",
    baseBody: "Original body",
    title: "Local title",
    body: "Local body",
    assetIds: ["asset-1"],
    editorVersion: "0.1.0",
    markdownProfile: "preserve",
    ...overrides,
  };
}

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("existing-document draft storage", () => {
  it("isolates drafts by user, vault, document, and tab without overwriting another tab", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-09T00:00:00.000Z"));
    try {
      expect(saveDocumentEditDraft(draft())).toBe(true);
      expect(
        saveDocumentEditDraft(
          draft({ draftId: "draft-2", tabId: "tab-2", title: "Other tab" }),
        ),
      ).toBe(true);

      expect(loadDocumentEditDraft(USER, VAULT, DOCUMENT, "tab-1")).toMatchObject({
        status: "restored",
        draft: { draftId: "draft-1", title: "Local title" },
      });
      expect(loadDocumentEditDraft(USER, VAULT, DOCUMENT, "tab-2")).toMatchObject({
        status: "restored",
        draft: { draftId: "draft-2", title: "Other tab" },
      });
      expect(window.localStorage.length).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not mix a document draft across user, vault, or document identity", () => {
    expect(saveDocumentEditDraft(draft())).toBe(true);

    expect(loadDocumentEditDraft("other-user", VAULT, DOCUMENT, "tab-1").status).toBe("none");
    expect(loadDocumentEditDraft(USER, "other-vault", DOCUMENT, "tab-1").status).toBe("none");
    expect(loadDocumentEditDraft(USER, VAULT, "akb://team/doc/other.md", "tab-1").status).toBe("none");
  });

  it("returns an expired draft for recovery without deleting its original text", () => {
    const stored = {
      ...draft(),
      version: 1,
      kind: "document-edit",
      updatedAt: "2026-09-01T00:00:00.000Z",
      expiresAt: "2026-09-01T01:00:00.000Z",
    };
    window.localStorage.setItem(
      documentEditDraftStorageKey(USER, VAULT, DOCUMENT, "tab-1", "draft-1"),
      JSON.stringify(stored),
    );

    const result = loadDocumentEditDraft(
      USER,
      VAULT,
      DOCUMENT,
      "tab-1",
      new Date("2026-09-02T00:00:00.000Z"),
    );
    expect(result).toMatchObject({ status: "expired", draft: { body: "Local body" } });
    expect(window.localStorage.getItem(documentEditDraftStorageKey(USER, VAULT, DOCUMENT, "tab-1", "draft-1"))).toContain("Local body");
  });

  it("preserves incompatible records for copying instead of migrating or discarding them", () => {
    const key = documentEditDraftStorageKey(USER, VAULT, DOCUMENT, "tab-1", "old-draft");
    window.localStorage.setItem(
      key,
      JSON.stringify({ version: 99, title: "Old title", body: "Old body" }),
    );

    expect(loadDocumentEditDraft(USER, VAULT, DOCUMENT, "tab-1")).toMatchObject({
      status: "incompatible",
      draft: { copyTitle: "Old title", copyBody: "Old body" },
    });
    expect(window.localStorage.getItem(key)).toContain("Old body");
  });

  it("reports local-storage write failure instead of claiming persistence", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementationOnce(() => {
      throw new Error("quota exceeded");
    });

    expect(saveDocumentEditDraft(draft())).toBe(false);
    expect(window.localStorage.length).toBe(0);
  });

  it("clears only the selected draft record", () => {
    const first = draft();
    const second = draft({ draftId: "draft-2", tabId: "tab-2" });
    saveDocumentEditDraft(first);
    saveDocumentEditDraft(second);

    clearDocumentEditDraft(first);

    expect(window.localStorage.getItem(documentEditDraftStorageKey(USER, VAULT, DOCUMENT, "tab-1", "draft-1"))).toBeNull();
    expect(window.localStorage.getItem(documentEditDraftStorageKey(USER, VAULT, DOCUMENT, "tab-2", "draft-2"))).not.toBeNull();
  });
});
