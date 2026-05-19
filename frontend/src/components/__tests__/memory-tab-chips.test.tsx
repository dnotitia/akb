import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryTab } from "../memory-tab";

vi.mock("@/lib/api", () => ({
  recallMemories: vi.fn(),
  forgetMemory: vi.fn(),
  forgetCategory: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("MemoryTab chips", () => {
  it("disables chip for category with zero memories", async () => {
    const { recallMemories } = await import("@/lib/api");
    (recallMemories as any).mockResolvedValue({
      memories: [
        { memory_id: "1", category: "work", content: "X", source: "manual", created_at: "2026-05-19T00:00:00Z", updated_at: "2026-05-19T00:00:00Z" },
      ],
    });
    render(<MemoryTab />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^all/i })).toBeTruthy()
    );
    const learning = screen.getByRole("button", { name: /learning/i });
    expect(learning.getAttribute("aria-disabled")).toBe("true");
  });

  it("'all' chip shows total count", async () => {
    const { recallMemories } = await import("@/lib/api");
    (recallMemories as any).mockResolvedValue({
      memories: [
        { memory_id: "1", category: "work", content: "X", source: "manual", created_at: "2026-05-19T00:00:00Z", updated_at: "2026-05-19T00:00:00Z" },
        { memory_id: "2", category: "learning", content: "Y", source: "manual", created_at: "2026-05-19T00:00:00Z", updated_at: "2026-05-19T00:00:00Z" },
      ],
    });
    render(<MemoryTab />);
    await waitFor(() => {
      const all = screen.getByRole("button", { name: /^all/i });
      expect(all.textContent).toMatch(/2/);
    });
  });

  it("renders source badge (AUTO / MANUAL) next to category", async () => {
    const { recallMemories } = await import("@/lib/api");
    (recallMemories as any).mockResolvedValue({
      memories: [
        { memory_id: "1", category: "work", content: "auto", source: "session_auto", created_at: "2026-05-19T00:00:00Z", updated_at: "2026-05-19T00:00:00Z" },
        { memory_id: "2", category: "learning", content: "manual", source: "manual", created_at: "2026-05-19T00:00:00Z", updated_at: "2026-05-19T00:00:00Z" },
      ],
    });
    render(<MemoryTab />);
    await waitFor(() => expect(screen.getByText("auto")).toBeTruthy());
    expect(screen.getAllByText(/AUTO/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/MANUAL/i).length).toBeGreaterThanOrEqual(1);
  });
});
