// frontend/src/components/graph/__tests__/GraphSidebar.test.tsx
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GraphSidebar } from "../GraphSidebar";
import { DEFAULT_VIEW, type GraphView } from "../graph-types";
import { searchDocs } from "@/lib/api";

// ---------- API mock (hoisted by Vitest) ----------
vi.mock("@/lib/api", () => ({
  searchDocs: vi.fn(),
}));
const mockedSearchDocs = vi.mocked(searchDocs);
// Alias for existing tests
const searchDocsMock = mockedSearchDocs;

afterEach(cleanup);
beforeEach(() => {
  localStorage.clear();
  searchDocsMock.mockReset();
});

function setup(view: Partial<GraphView> = {}) {
  const onChange = vi.fn();
  const onNavigate = vi.fn();
  render(
    <GraphSidebar
      vault="akb"
      view={{ ...DEFAULT_VIEW, ...view }}
      currentUrl="?"
      onChange={onChange}
      onNavigate={onNavigate}
    />,
  );
  return { onChange, onNavigate };
}

describe("GraphSidebar · types", () => {
  it("toggles a node type off", async () => {
    const u = userEvent.setup();
    const { onChange } = setup();
    await u.click(screen.getByRole("button", { name: /toggle document/i }));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next: GraphView = onChange.mock.calls[0][0];
    expect(next.types.has("document")).toBe(false);
    expect(next.types.has("table")).toBe(true);
  });
});

describe("GraphSidebar · depth", () => {
  it("is disabled when no entry is set", () => {
    setup();
    const radio = screen.getByRole("radio", { name: /depth 3/i });
    expect(radio).toBeDisabled();
  });

  it("emits depth change when entry is set", async () => {
    const u = userEvent.setup();
    const { onChange } = setup({ entry: "d-1", depth: 2 });
    await u.click(screen.getByRole("radio", { name: /depth 3/i }));
    expect(onChange.mock.calls[0][0].depth).toBe(3);
  });
});

describe("GraphSidebar · saved + recent", () => {
  it("saves a named view and lists it", async () => {
    const u = userEvent.setup();
    setup({ entry: "d-1" });
    await u.click(screen.getByRole("button", { name: /save view/i }));
    const input = await screen.findByPlaceholderText(/name this view/i);
    await u.type(input, "roadmap{Enter}");
    expect(screen.getByText("roadmap")).toBeTruthy();
  });

  it("navigates when a saved view is clicked", async () => {
    const u = userEvent.setup();
    const { onNavigate } = setup({ entry: "d-1" });
    await u.click(screen.getByRole("button", { name: /save view/i }));
    const input = await screen.findByPlaceholderText(/name this view/i);
    await u.type(input, "roadmap{Enter}");
    await u.click(screen.getByText("roadmap"));
    expect(onNavigate).toHaveBeenCalledWith(expect.stringContaining("entry=d-1"));
  });

  it("renders recent entries from localStorage", () => {
    localStorage.setItem(
      "akb-graph-recent:akb",
      JSON.stringify([{ doc_id: "d-9", title: "Niner" }]),
    );
    setup();
    expect(screen.getByText("Niner")).toBeTruthy();
  });
});

describe("GraphSidebar · entry search", () => {
  beforeEach(() => {
    searchDocsMock.mockReset();
  });

  it("debounces, lists hits, and commits the chosen entry", async () => {
    searchDocsMock.mockResolvedValue({
      query: "road",
      total: 1,
      results: [
        { doc_id: "d-1", title: "Roadmap", resource_type: "document" },
      ],
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onChange = vi.fn();
    render(
      <GraphSidebar
        vault="akb"
        view={DEFAULT_VIEW}
        currentUrl="?"
        onChange={onChange}
        onNavigate={() => {}}
      />,
    );
    const input = screen.getByPlaceholderText(/search documents/i);
    // Use fireEvent to set value directly and avoid userEvent fake-timer conflicts.
    fireEvent.change(input, { target: { value: "road" } });
    await vi.advanceTimersByTimeAsync(300);
    // Now the hit should be in the DOM.
    const hit = await screen.findByText("Roadmap");
    fireEvent.click(hit);
    expect(onChange).toHaveBeenCalled();
    const lastArg = onChange.mock.calls.at(-1)?.[0];
    expect(lastArg?.entry).toBe("d-1");
    const stored = localStorage.getItem("akb-graph-recent:akb");
    expect(stored).toContain("d-1");
    vi.useRealTimers();
  });
});

describe("GraphSidebar · entry search", () => {
  it("debounces, lists hits, commits the chosen entry, and pushes to recent", async () => {
    mockedSearchDocs.mockResolvedValue({
      query: "road",
      total: 1,
      results: [{ doc_id: "d-1", title: "Roadmap", resource_type: "document" }],
    });
    const u = userEvent.setup();
    const onChange = vi.fn();
    render(
      <GraphSidebar
        vault="akb"
        view={DEFAULT_VIEW}
        currentUrl="?"
        onChange={onChange}
        onNavigate={() => {}}
      />,
    );
    const input = screen.getByPlaceholderText(/search documents/i);
    await u.type(input, "road");
    // Wait for debounce (300ms) + microtask flush.
    const hit = await screen.findByText("Roadmap", undefined, { timeout: 1500 });
    await u.click(hit);
    expect(mockedSearchDocs).toHaveBeenCalledWith("road", "akb", 8);
    const lastArg = onChange.mock.calls.at(-1)?.[0];
    expect(lastArg?.entry).toBe("d-1");
    const stored = localStorage.getItem("akb-graph-recent:akb");
    expect(stored).toContain("d-1");
  });
});
