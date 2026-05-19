import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { HistoryList, type HistoryEntry } from "../history-list";

const entries: HistoryEntry[] = [
  { hash: "abc1234567", agent: "kwoo24", subject: "Initial", timestamp: "2026-05-19T00:00:00Z" },
  { hash: "def5678901", agent: "kwoo24", subject: "Update", timestamp: "2026-05-19T01:00:00Z" },
];

describe("HistoryList", () => {
  it("read-only mode (no onSelect) renders rows as div", () => {
    render(<HistoryList entries={entries} />);
    expect(screen.getByText("abc1234")).toBeTruthy();
    // no button per row
    expect(screen.queryByRole("button", { name: /View document at commit/i })).toBeNull();
  });

  it("clickable mode invokes onSelect with the full commit hash", () => {
    const onSelect = vi.fn();
    render(<HistoryList entries={entries} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: /commit abc1234/i }));
    expect(onSelect).toHaveBeenCalledWith("abc1234567");
  });

  it("selectedHash marks matching row with aria-pressed=true", () => {
    const onSelect = vi.fn();
    render(<HistoryList entries={entries} onSelect={onSelect} selectedHash="def5678901" />);
    const buttons = screen.getAllByRole("button");
    expect(buttons[0].getAttribute("aria-pressed")).toBe("false");
    expect(buttons[1].getAttribute("aria-pressed")).toBe("true");
  });

  it("entry without a hash stays unclickable even in onSelect mode", () => {
    const onSelect = vi.fn();
    const withMissing: HistoryEntry[] = [
      ...entries,
      { agent: "unknown", subject: "Orphan", timestamp: "2026-05-19T02:00:00Z" },
    ];
    render(<HistoryList entries={withMissing} onSelect={onSelect} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons.length).toBe(2); // only the two with hashes
  });

  it("empty list renders 'No history yet.'", () => {
    render(<HistoryList entries={[]} />);
    expect(screen.getByText(/no history yet/i)).toBeTruthy();
  });
});
