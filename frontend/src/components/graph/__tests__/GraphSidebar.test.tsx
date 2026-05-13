// frontend/src/components/graph/__tests__/GraphSidebar.test.tsx
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GraphSidebar } from "../GraphSidebar";
import { DEFAULT_VIEW, type GraphView } from "../graph-types";

afterEach(cleanup);
beforeEach(() => localStorage.clear());

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
