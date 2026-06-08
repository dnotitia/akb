import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

// Capture the simulation methods + props the component drives, by stubbing
// react-force-graph-2d with a ref-forwarding placeholder (jsdom can't run the
// real canvas engine).
const d3Force = vi.fn();
const d3ReheatSimulation = vi.fn();
let lastProps: Record<string, unknown> = {};

vi.mock("react-force-graph-2d", () => ({
  __esModule: true,
  default: React.forwardRef(function FGMock(
    props: Record<string, unknown>,
    ref: React.Ref<unknown>,
  ) {
    lastProps = props;
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
  degraded: false,
  onSelect: () => {},
  onContextMenu: () => {},
};

beforeEach(() => {
  d3Force.mockClear();
  d3ReheatSimulation.mockClear();
  lastProps = {};
});
afterEach(cleanup);

describe("GraphCanvas — cluster wiring", () => {
  it("installs the cluster force + reheats + passes the hull render hook when clustering is on (default)", () => {
    render(<GraphCanvas {...baseProps} />);

    const clusterCalls = d3Force.mock.calls.filter((c) => c[0] === "cluster");
    expect(clusterCalls.length).toBeGreaterThan(0);
    // most recent install passed a force FUNCTION (not null)
    expect(typeof clusterCalls[clusterCalls.length - 1][1]).toBe("function");
    expect(d3ReheatSimulation).toHaveBeenCalled();
    expect(typeof lastProps.onRenderFramePre).toBe("function");
  });

  it("toggling clusters off nulls the force and flips aria-pressed", () => {
    render(<GraphCanvas {...baseProps} />);

    const onBtn = screen.getByRole("button", { name: /hide clusters/i });
    expect(onBtn).toHaveAttribute("aria-pressed", "true");

    d3Force.mockClear();
    fireEvent.click(onBtn);

    const clusterCalls = d3Force.mock.calls.filter((c) => c[0] === "cluster");
    expect(clusterCalls.length).toBeGreaterThan(0);
    // every cluster call after toggling off must remove the force (null)
    expect(clusterCalls.every((c) => c[1] === null)).toBe(true);

    expect(screen.getByRole("button", { name: /show clusters/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });
});
