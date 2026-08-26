import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  readRecentDocumentViews,
  recordRecentDocumentView,
} from "@/lib/recent-document-views";

beforeEach(() => {
  localStorage.clear();
  vi.useRealTimers();
});

describe("recent document views", () => {
  it("keeps histories user-scoped and moves a revisited document to the front", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-25T10:00:00Z"));
    recordRecentDocumentView("alice", {
      vault: "team",
      path: "notes/one.md",
      title: "One",
      type: "note",
    });
    vi.setSystemTime(new Date("2026-08-25T11:00:00Z"));
    recordRecentDocumentView("alice", {
      vault: "team",
      path: "notes/two.md",
      title: "Two",
      type: "note",
    });
    vi.setSystemTime(new Date("2026-08-25T12:00:00Z"));
    recordRecentDocumentView("alice", {
      vault: "team",
      path: "notes/one.md",
      title: "One revised",
      type: "report",
    });
    recordRecentDocumentView("bob", {
      vault: "private",
      path: "secret.md",
      title: "Secret",
    });

    expect(readRecentDocumentViews("alice")).toEqual([
      expect.objectContaining({ path: "notes/one.md", title: "One revised", type: "report" }),
      expect.objectContaining({ path: "notes/two.md", title: "Two" }),
    ]);
    expect(readRecentDocumentViews("bob")).toHaveLength(1);
  });

  it("degrades malformed or unavailable client state to an empty history", () => {
    localStorage.setItem("akb.recentDocumentViews.v1:alice", "not-json");
    expect(readRecentDocumentViews("alice")).toEqual([]);
    expect(readRecentDocumentViews("missing-user")).toEqual([]);
  });
});
