// frontend/src/components/graph/__tests__/GraphSidebar.test.tsx
import type { ComponentProps } from "react";
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

afterEach(cleanup);
beforeEach(() => {
  localStorage.clear();
  mockedSearchDocs.mockReset();
});

// Props added in the Phase-1 navigation pass — defaulted here so each test only
// sets what it exercises.
const navProps = {
  hubs: [],
  orphanCount: 0,
  hideOrphans: false,
  onToggleOrphans: () => {},
  hiddenCount: 0,
  onUnhideAll: () => {},
  onSelectNode: () => {},
};

function setup(view: Partial<GraphView> = {}) {
  const onChange = vi.fn();
  const onNavigate = vi.fn();
  render(
    <GraphSidebar
      vault="akb"
      view={{ ...DEFAULT_VIEW, ...view }}
      onChange={onChange}
      onNavigate={onNavigate}
      {...navProps}
    />,
  );
  return { onChange, onNavigate };
}

type SidebarProps = ComponentProps<typeof GraphSidebar>;

/** Render with full default props + a typed rerender that re-applies them, for
 *  the prop-driven (hubs / orphans) tests. */
function renderSidebar(overrides: Partial<SidebarProps> = {}) {
  const props: SidebarProps = {
    vault: "akb",
    view: DEFAULT_VIEW,
    onChange: vi.fn(),
    onNavigate: vi.fn(),
    ...navProps,
    ...overrides,
  };
  const utils = render(<GraphSidebar {...props} />);
  return {
    ...utils,
    rerender: (next: Partial<SidebarProps>) =>
      utils.rerender(<GraphSidebar {...props} {...next} />),
  };
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

describe("GraphSidebar · hops", () => {
  it("shows the hops control in both modes (incl. whole-graph)", () => {
    setup(); // no entry
    expect(screen.getByRole("radio", { name: /3 hops/i })).toBeTruthy();
  });

  it("emits a hops change and persists it", async () => {
    const u = userEvent.setup();
    const { onChange } = setup({ entry: "d-1", hops: 2 });
    await u.click(screen.getByRole("radio", { name: /3 hops/i }));
    expect(onChange.mock.calls[0][0].hops).toBe(3);
    expect(localStorage.getItem("akb:graph:hops")).toBe("3");
  });
});

describe("GraphSidebar · orphans + hubs", () => {
  it("shows the orphans toggle only when there are orphans", () => {
    const { rerender } = renderSidebar({ orphanCount: 0 });
    expect(screen.queryByText(/hide orphans/i)).toBeNull();
    rerender({ orphanCount: 3 });
    expect(screen.getByText(/hide orphans/i)).toBeTruthy();
    expect(screen.getByText(/3 unconnected/i)).toBeTruthy();
  });

  it("lists hubs and selects one on click", async () => {
    const u = userEvent.setup();
    const onSelectNode = vi.fn();
    renderSidebar({
      hubs: [{ uri: "akb://akb/doc/a.md", name: "Hub A", kind: "document" }],
      onSelectNode,
    });
    await u.click(screen.getByRole("button", { name: /hub a/i }));
    expect(onSelectNode).toHaveBeenCalledWith("akb://akb/doc/a.md");
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
  it("debounces, lists hits, and commits the chosen entry", async () => {
    mockedSearchDocs.mockResolvedValue({
      query: "road",
      total: 1,
      returned: 1,
      total_matches: 1,
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
        onChange={onChange}
        onNavigate={() => {}}
        {...navProps}
      />,
    );
    const input = screen.getByPlaceholderText(/search to focus/i);
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

  it("debounces, lists hits, commits the chosen entry, and pushes to recent", async () => {
    mockedSearchDocs.mockResolvedValue({
      query: "road",
      total: 1,
      returned: 1,
      total_matches: 1,
      results: [{ doc_id: "d-1", title: "Roadmap", resource_type: "document" }],
    });
    const u = userEvent.setup();
    const onChange = vi.fn();
    render(
      <GraphSidebar
        vault="akb"
        view={DEFAULT_VIEW}
        onChange={onChange}
        onNavigate={() => {}}
        {...navProps}
      />,
    );
    const input = screen.getByPlaceholderText(/search to focus/i);
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
