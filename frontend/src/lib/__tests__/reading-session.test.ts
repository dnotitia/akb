import { afterEach, describe, expect, it, vi } from "vitest";
import { clearReadingSessions, emptyReadingSession, readReadingSession, writeReadingSession, READING_SESSION_PREFIX } from "@/lib/reading-session";
import { readingDocumentKey } from "@/lib/reading-workspace";

afterEach(() => vi.restoreAllMocks());

describe("reading session storage", () => {
  it("restores ordered canonical identifiers, versions and positions without persisting content or titles", () => {
    const session = emptyReadingSession();
    const doc = { vault: "team", path: "한글/reading.md", title: "Private title", content: "Private body", token: "never-store", version: "abcdef123", href: "/vault/team/doc/reading.md?view=raw" };
    session.tabs = [doc]; session.companion = doc; session.active = readingDocumentKey(doc);
    session.positions.set(readingDocumentKey(doc) + ":rendered", 625);
    session.positions.set(readingDocumentKey(doc) + ":raw", 710);
    session.positions.set(readingDocumentKey(doc) + ":side", 380);
    writeReadingSession("owner", session);
    const stored = localStorage.getItem(READING_SESSION_PREFIX + "owner")!;
    for (const secret of ["Private title", "Private body", "never-store", '"href"']) expect(stored).not.toContain(secret);
    const restored = readReadingSession("owner");
    expect(restored.tabs[0]).toMatchObject({ vault: "team", path: "한글/reading.md", title: "", version: "abcdef123" });
    expect(restored.tabs[0].href).toContain("view=raw");
    expect(restored.active).toBe(readingDocumentKey(doc));
    expect(restored.positions.get(readingDocumentKey(doc) + ":side")).toBe(380);
    expect(readReadingSession("another-owner").tabs).toEqual([]);
  });
  it("rejects corrupt, unknown, external and traversal entries and bounds restoration", () => {
    const key = READING_SESSION_PREFIX + "owner";
    localStorage.setItem(key, "broken"); expect(readReadingSession("owner").tabs).toEqual([]);
    localStorage.setItem(key, JSON.stringify({ schema: 2, tabs: [], closed: [] })); expect(readReadingSession("owner").tabs).toEqual([]);
    const bad = [{ uri: "https://example.invalid/secret" }, { uri: "akb://team/doc/../secret" }, { uri: "akb://team/doc/a.md", version: "not-a-revision" }];
    localStorage.setItem(key, JSON.stringify({ schema: 1, tabs: [...bad, ...Array.from({ length: 40 }, (_, index) => ({ uri: `akb://team/doc/${index}.md`, top: -1, rawTop: 1e100 }))], closed: [] }));
    const restored = readReadingSession("owner");
    expect(restored.tabs.length).toBeLessThanOrEqual(30);
    expect(restored.tabs.every(doc => /^\d+\.md$/.test(doc.path))).toBe(true);
    expect([...restored.positions.values()].every(top => top >= 0 && top <= 10_000_000)).toBe(true);
  });
  it("does not restore an editing URL or require browser storage", () => {
    const session = emptyReadingSession(); session.tabs = [{ vault: "v", path: "a.md", title: "Title", href: "/vault/v/doc/a.md?view=edit" }];
    writeReadingSession("owner", session); expect(readReadingSession("owner").tabs[0].href).not.toContain("edit");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(readReadingSession("owner").tabs).toEqual([]);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("full"); });
    expect(() => writeReadingSession("owner", session)).not.toThrow();
  });
  it("clears reading identifiers on explicit sign-out without clearing unrelated preferences", () => {
    localStorage.setItem("akb:reader-preferences:v1", "keep");
    writeReadingSession("owner", emptyReadingSession()); clearReadingSessions();
    expect(localStorage.getItem(READING_SESSION_PREFIX + "owner")).toBeNull();
    expect(localStorage.getItem("akb:reader-preferences:v1")).toBe("keep");
    localStorage.removeItem("akb:reader-preferences:v1");
  });
});
