import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { render, screen, cleanup } from "@testing-library/react";

// Capture the simulation methods + props the component drives, by stubbing
// react-force-graph-2d with a ref-forwarding placeholder (jsdom can't run the
// real canvas engine).
const d3Force = vi.fn();
const d3ReheatSimulation = vi.fn();
vi.mock("react-force-graph-2d", () => ({
  __esModule: true,
  default: React.forwardRef(function FGMock(
    _props: Record<string, unknown>,
    ref: React.Ref<unknown>,
  ) {
    React.useImperativeHandle(ref, () => ({
      d3Force,
      d3ReheatSimulation,
      zoomToFit: vi.fn(),
      zoom: vi.fn(() => 1),
      centerAt: vi.fn(),
      pauseAnimation: vi.fn(),
      resumeAnimation: vi.fn(),
    }));
    return React.createElement("div", { "data-testid": "fg" });
  }),
}));

vi.mock("@/hooks/use-theme", () => ({ useTheme: () => ({ theme: "dark" }) }));

import { GraphCanvas } from "../GraphCanvas";

const baseProps = {
  nodes: [],
  edges: [],
  pinned: new Set<string>(),
  hidden: new Set<string>(),
  onSelect: () => {},
  onExpand: () => {},
  onPinNode: () => {},
  onContextMenu: () => {},
};

beforeEach(() => {
  d3Force.mockClear();
  d3ReheatSimulation.mockClear();
});
afterEach(cleanup);

describe("GraphCanvas — cluster wiring", () => {
  it("installs the cluster + collide forces and reheats when clustering is on (default)", () => {
    render(<GraphCanvas {...baseProps} />);

    const clusterCalls = d3Force.mock.calls.filter((c) => c[0] === "cluster");
    const collideCalls = d3Force.mock.calls.filter((c) => c[0] === "collide");
    expect(clusterCalls.length).toBeGreaterThan(0);
    expect(collideCalls.length).toBeGreaterThan(0);
    // most recent installs passed a force FUNCTION (not null)
    expect(typeof clusterCalls[clusterCalls.length - 1][1]).toBe("function");
    expect(typeof collideCalls[collideCalls.length - 1][1]).toBe("function");
    expect(d3ReheatSimulation).toHaveBeenCalled();
  });

  it("keeps canvas controls outside the renderer while retaining its accessible label", () => {
    render(<GraphCanvas {...baseProps} />);
    expect(screen.getByRole("img", { name: /knowledge graph: 0 nodes, 0 edges/i })).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
