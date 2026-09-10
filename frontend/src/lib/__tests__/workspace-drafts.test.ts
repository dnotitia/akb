import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearDocumentDraft, clearDocumentEditDraft, documentEditDraftStorageKey,
  documentEditDraftTabId, listWorkspaceDrafts, loadDocumentDraft,
  saveDocumentDraft, saveDocumentEditDraft, WORKSPACE_DRAFTS_EVENT,
  workspaceDraftHref,
  type DocumentEditDraftInput,
} from "@/lib/document-draft";

const create = (userId = "alice") => ({
  userId, vault: "team", title: "New note", collection: "notes", type: "note" as const,
  domain: "", summary: "", tags: [], body: "Private body", assetIds: [],
});
const edit = (extra: Partial<DocumentEditDraftInput> = {}): DocumentEditDraftInput => ({
  userId: "alice", vault: "team", document: "akb://team/doc/note.md", draftId: "1",
  tabId: documentEditDraftTabId(), baseCommit: "abc", baseTitle: "Note", baseBody: "Before",
  title: "Edited note", body: "Private edits", assetIds: [], editorVersion: "0.1.0",
  markdownProfile: "preserve", ...extra,
});

afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("workspace draft metadata", () => {
  it("routes new drafts to the creation bridge and resolves both edit identity formats", () => {
    const common = { vault: "team", title: "Note", updatedAt: "2026-09-09" };
    expect(workspaceDraftHref({ ...common, kind: "new" })).toBe("/vault/team/doc/new");
    expect(workspaceDraftHref({ ...common, kind: "edit", document: "team:notes/note.md" })).toBe("/vault/team/doc/notes%2Fnote.md?view=edit");
    expect(workspaceDraftHref({ ...common, kind: "edit", document: "akb://team/doc/note.md" })).toBe("/vault/team/doc/note.md?view=edit");
    expect(workspaceDraftHref({ ...common, kind: "edit", document: "akb://team/coll/notes/doc/note.md" })).toBe("/vault/team/doc/notes%2Fnote.md?view=edit");
    expect(workspaceDraftHref({ ...common, kind: "edit", document: "akb://other/doc/note.md" })).toBe("/vault/team");
  });
  it("scopes new drafts by account and does not adopt unowned legacy records", () => {
    localStorage.setItem("akb:document-draft:team", JSON.stringify({ ...create(), title: "Legacy" }));
    expect(loadDocumentDraft("alice", "team")).toBeNull();
    saveDocumentDraft(create());
    saveDocumentDraft({ ...create("bob"), title: "Bob's draft" });
    expect(loadDocumentDraft("alice", "team")?.title).toBe("New note");
    expect(listWorkspaceDrafts("bob")).toEqual([expect.objectContaining({ title: "Bob's draft" })]);
    expect(listWorkspaceDrafts("")).toEqual([]);
    clearDocumentDraft("alice", "team");
    expect(loadDocumentDraft("bob", "team")?.title).toBe("Bob's draft");
    expect(localStorage.getItem("akb:document-draft:team")).not.toBeNull();
  });

  it("returns only safe metadata and deduplicates edit drafts using editor tab priority", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-09T00:00:00Z"));
    saveDocumentDraft(create());
    saveDocumentEditDraft(edit());
    vi.setSystemTime(new Date("2026-09-09T00:01:00Z"));
    saveDocumentEditDraft(edit({ draftId: "other", tabId: "other", title: "Other tab" }));
    const entries = listWorkspaceDrafts("alice");
    expect(entries).toHaveLength(2);
    expect(entries.find((entry) => entry.kind === "edit")?.title).toBe("Edited note");
    expect(JSON.stringify(entries)).not.toContain("Private");
    expect(entries.every((entry) => !("body" in entry) && !("assetIds" in entry))).toBe(true);
    vi.setSystemTime(new Date("2026-09-11T00:00:00Z"));
    expect(listWorkspaceDrafts("alice").map((entry) => entry.kind)).toEqual(["new"]);
  });

  it("skips incompatible and malformed records without destroying drafts", () => {
    const value = edit();
    localStorage.setItem(documentEditDraftStorageKey("alice", "team", value.document, value.tabId, "1"), JSON.stringify({ ...value, version: 99 }));
    localStorage.setItem("akb:document-draft:account:alice:%broken", "oops");
    saveDocumentDraft(create());
    expect(listWorkspaceDrafts("alice")).toHaveLength(1);
    expect(localStorage.length).toBe(3);
  });

  it("announces saves and removals in the same tab", () => {
    const listener = vi.fn();
    window.addEventListener(WORKSPACE_DRAFTS_EVENT, listener);
    saveDocumentDraft(create()); clearDocumentDraft("alice", "team");
    saveDocumentEditDraft(edit()); clearDocumentEditDraft(edit());
    expect(listener).toHaveBeenCalledTimes(4);
    window.removeEventListener(WORKSPACE_DRAFTS_EVENT, listener);
  });

  it("degrades safely when storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(loadDocumentDraft("alice", "team")).toBeNull();
    expect(listWorkspaceDrafts("alice")).toEqual([]);
  });
});
