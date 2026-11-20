import { describe, expect, it } from "vitest";
import { closeReadingTabs, readingTabCloseKeys, restoreReadingTabs } from "@/lib/reading-tab-actions";
import { emptyReadingSession, READING_CLOSED_LIMIT, writeReadingSession, READING_SESSION_PREFIX, type ReadingSession } from "@/lib/reading-session";
import { readingDocumentKey } from "@/lib/reading-workspace";

const doc = (index: number) => ({ vault: "team", path: `${index}.md`, title: `Private title ${index}`, href: `/vault/team/doc/${index}.md` });
const key = (index: number) => readingDocumentKey(doc(index));
const session = (count = 4): ReadingSession => ({ ...emptyReadingSession(), tabs: Array.from({ length: count }, (_, index) => doc(index)), active: key(1) });

describe("closing and restoring reading tabs", () => {
  it("uses the selected tab and visual order for other/left/right scopes", () => {
    const tabs = [doc(2), doc(0), doc(3), doc(1)];
    expect(readingTabCloseKeys(tabs, "others", key(0))).toEqual([key(2), key(3), key(1)]);
    expect(readingTabCloseKeys(tabs, "left", key(0))).toEqual([key(2)]);
    expect(readingTabCloseKeys(tabs, "right", key(0))).toEqual([key(3), key(1)]);
    expect(readingTabCloseKeys(tabs, "left", "missing")).toEqual([]);
    expect(readingTabCloseKeys(tabs, "all", "")).toEqual(tabs.map(readingDocumentKey));
  });

  it("restores all 30 tabs, their versions, active document, side pane and positions beyond the recent-history limit", () => {
    const before = session(30);
    before.tabs[8] = { ...before.tabs[8], version: "abcdef1", href: before.tabs[8].href + "?commit=abcdef1" };
    before.companion = before.tabs[8];
    before.tabs.forEach((tab, index) => before.positions.set(readingDocumentKey(tab) + ":rendered", index * 100));
    before.positions.set(readingDocumentKey(before.tabs[8]) + ":side", 812);
    const closed = closeReadingTabs(before, before.tabs.map(readingDocumentKey), "batch", "owner:1", true);
    expect(closed.tabs).toEqual([]); expect(closed.active).toBeNull(); expect(closed.companion).toBeNull();
    expect(closed.closed).toHaveLength(READING_CLOSED_LIMIT);
    const restored = restoreReadingTabs(closed)!;
    expect(restored.tabs).toEqual(before.tabs); expect(restored.active).toBe(before.active);
    expect(restored.companion).toEqual(before.companion); expect(restored.positions).toEqual(before.positions);
    expect(restored.undo).toBeNull(); expect(restored.closed).toEqual([]);
    expect(before.tabs).toHaveLength(30);
  });

  it("preserves documents opened afterwards and does not reopen unrelated later closures", () => {
    const before = session();
    const closed = closeReadingTabs(before, [key(0), key(1)], "batch", "owner:1");
    closed.tabs = [doc(3), doc(99)]; // The kept tab 2 was subsequently closed.
    closed.positions.set(key(3) + ":rendered", 911);
    const restored = restoreReadingTabs(closed)!;
    expect(restored.tabs.map(readingDocumentKey)).toEqual([key(0), key(1), key(3), key(99)]);
    expect(restored.positions.get(key(3) + ":rendered")).toBe(911);
  });

  it("refuses an undo that would evict newly opened tabs", () => {
    const before = session(30);
    const closed = closeReadingTabs(before, before.tabs.map(readingDocumentKey), "batch", "owner:1", true);
    closed.tabs = [doc(99)];
    expect(restoreReadingTabs(closed)).toBeNull();
    expect(closed.tabs).toEqual([doc(99)]); expect(closed.undo?.keys).toHaveLength(30);
  });

  it("does not serialize the in-memory undo titles or snapshots", () => {
    const closed = closeReadingTabs(session(), [key(0)], "batch", "owner:1");
    writeReadingSession("tab-action-test", closed);
    const stored = localStorage.getItem(READING_SESSION_PREFIX + "tab-action-test")!;
    expect(stored).not.toContain("Private title"); expect(stored).not.toContain('"undo"');
    localStorage.removeItem(READING_SESSION_PREFIX + "tab-action-test");
  });
});
