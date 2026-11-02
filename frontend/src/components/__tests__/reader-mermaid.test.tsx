import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ReaderMermaid } from "@/components/reader-mermaid";

const engine = vi.hoisted(() => ({ initialize: vi.fn(), render: vi.fn() }));
vi.mock("mermaid", () => ({ default: engine }));
const block = { index: 0, language: "mermaid", code: "flowchart LR\nA --> B", collapsed: false };
beforeEach(() => {
  vi.clearAllMocks();
  engine.render.mockResolvedValue({ svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 60"><text>A to B</text></svg>' });
  vi.stubGlobal("IntersectionObserver", undefined);
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:diagram") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
});
afterEach(() => vi.unstubAllGlobals());

it("renders on demand under a fixed strict policy and releases generated images", async () => {
  const view = render(<ReaderMermaid block={block} onToggle={() => {}} onEnlarge={() => {}} />);
  expect(await screen.findByRole("img", { name: "Mermaid diagram" })).toHaveAttribute("src", "blob:diagram");
  expect(engine.initialize).toHaveBeenCalledWith(expect.objectContaining({ securityLevel: "strict", startOnLoad: false, htmlLabels: false, maxTextSize: 40_000, maxEdges: 500 }));
  view.unmount(); expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:diagram");
});

it("does not activate external resources or source config directives", () => {
  render(<ReaderMermaid block={{ ...block, code: '%%{init: {securityLevel: "loose"}}%%\nflowchart LR\nA --> B' }} onToggle={() => {}} onEnlarge={() => {}} />);
  expect(screen.getByRole("status")).toHaveTextContent("source remains available");
  expect(engine.render).not.toHaveBeenCalled();
});

it("discards an asynchronous diagram after its document is closed", async () => {
  let finish!: (value: { svg: string }) => void;
  engine.render.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const view = render(<ReaderMermaid block={block} onToggle={() => {}} onEnlarge={() => {}} />);
  await waitFor(() => expect(engine.render).toHaveBeenCalled()); view.unmount();
  await act(async () => { finish({ svg: '<svg xmlns="http://www.w3.org/2000/svg"/>' }); await Promise.resolve(); });
  expect(URL.createObjectURL).not.toHaveBeenCalled();
});
