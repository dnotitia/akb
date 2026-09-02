import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { HistoryList, type HistoryEntry } from "../history-list";

const entries: HistoryEntry[] = [
  {
    hash: "abcdef123456", // pragma: allowlist secret — synthetic Git commit
    author: "user-1",
    author_name: "Kim",
    message: "Initial document",
    date: "2026-05-19T00:00:00Z",
  },
  {
    hash: "bcdefa234567", // pragma: allowlist secret — synthetic Git commit
    author: "user-2",
    message: "Update architecture",
    date: "2026-05-19T01:00:00Z",
  },
];

describe("HistoryList", () => {
  it("renders typed document lineage with readable messages and authors", () => {
    render(<HistoryList entries={entries} />);

    expect(screen.getByText("Initial document")).toBeInTheDocument();
    expect(screen.getByText("Kim")).toBeInTheDocument();
    expect(screen.getByText("abcdef1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /view changes/i })).toBeNull();
  });

  it("opens the complete version from the primary row action", () => {
    const onSelect = vi.fn();
    render(<HistoryList entries={entries} onSelect={onSelect} />);

    fireEvent.click(screen.getByRole("button", { name: /view version abcdef1/i }));
    expect(onSelect).toHaveBeenCalledWith(entries[0].hash);
  });

  it("keeps the Changes action visible and passes its trigger for focus restoration", () => {
    const onCompare = vi.fn();
    render(<HistoryList entries={entries} onCompare={onCompare} />);

    const trigger = screen.getByRole("button", { name: /view changes in version abcdef1/i });
    fireEvent.click(trigger);
    expect(onCompare).toHaveBeenCalledWith(entries[0].hash, trigger);
  });

  it("marks a prefix-matched comparison without relying on color alone", () => {
    render(
      <HistoryList
        entries={entries}
        onCompare={vi.fn()}
        selectedHash="bcdefa2"
        diffHash="bcdefa2"
      />,
    );

    const changes = screen.getByRole("button", {
      name: /view changes in version bcdefa2/i,
    });
    expect(changes).toHaveAttribute("aria-pressed", "true");
    expect(changes).toHaveTextContent("Changes");
  });

  it("renders a clear empty state", () => {
    render(<HistoryList entries={[]} />);
    expect(screen.getByText(/no versions yet/i)).toBeInTheDocument();
  });
});
