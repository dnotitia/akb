import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GraphCanvas } from "../GraphCanvas";
import { GraphListView } from "../GraphListView";
import type { GraphNode } from "../graph-types";

const nodes: GraphNode[] = [
  { uri: "akb://fixture/doc/alpha.md", name: "Alpha", kind: "document" },
  { uri: "akb://fixture/file/beta", name: "Beta", kind: "file" },
];

function GraphFallbackFixture() {
  const [list, setList] = useState(false);
  return list ? <GraphListView nodes={nodes} edges={[]} onSelect={() => {}} /> :
    <GraphCanvas nodes={nodes} edges={[]} pinned={new Set()} onSelect={() => {}}
      onExpand={() => {}} onContextMenu={() => {}} onUseList={() => setList(true)} />;
}

afterEach(() => vi.restoreAllMocks());

describe("GraphCanvas WebGL fallback", () => {
  it("explains unavailable WebGL and lets the user browse the loaded resources in List", async () => {
    // Let the actual Three.js constructor encounter the browser capability
    // failure. The real engine's successful path runs in the browser suite.
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
    const reportError = console.error;
    vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
      if (args.some(value => typeof value === "string" && value.includes("Error creating WebGL context"))) return;
      reportError(...args);
    });
    render(<GraphFallbackFixture />);
    expect(screen.getByRole("alert")).toHaveTextContent("3D requires WebGL 2");
    expect(screen.getByRole("alert")).toHaveTextContent("every loaded resource in List");
    expect(screen.getByRole("img")).toHaveAccessibleName(/3D knowledge graph: 2 resources, 0 relationships/);
    await userEvent.click(screen.getByRole("button", { name: "Use List" }));
    expect(screen.getByRole("region", { name: "Relationship index" })).toBeVisible();
    expect(screen.getByRole("button", { name: /Alpha/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Beta/ })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
