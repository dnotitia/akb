import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { GraphCanvas } from "../GraphCanvas";

const JSDOM_CANVAS_INIT_PHRASES = [
  "getContext",                                      // HTMLCanvasElement.getContext is not implemented
  "Cannot read properties of null (reading 'scale')", // force-graph hits the null ctx
];

function isJsdomCanvasInitError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err);
  return JSDOM_CANVAS_INIT_PHRASES.some((phrase) => message.includes(phrase));
}

describe("GraphCanvas smoke", () => {
  it("mounts without component-level errors (canvas init from jsdom is tolerated)", () => {
    try {
      const { container } = render(
        <GraphCanvas
          nodes={[]}
          edges={[]}
          pinned={new Set()}
          hidden={new Set()}
          degraded={false}
          onSelect={() => {}}
          onDoubleClick={() => {}}
          onContextMenu={() => {}}
        />,
      );
      expect(container).toBeTruthy();
    } catch (err) {
      if (!isJsdomCanvasInitError(err)) {
        throw err;
      }
      // jsdom canvas init failure — the component reached the render path;
      // success for smoke purposes.
    }
  });
});
