import { useState } from "react";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DocumentContextPanel } from "../document-context-panel";
import { Dialog, DialogContent, DialogTitle } from "../ui/dialog";

const contextLabels = ["Document info", "Table of contents", "Relations", "Version history"];

function ContextHarness() {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<"info" | "outline" | "relations" | "history">("info");
  const [actions, setActions] = useState(0);
  const [editing, setEditing] = useState(false);
  return <div data-testid="reader-layout">
    <main id="document-reading-canvas"><h2 id="article-heading">Article heading</h2><button type="button" onClick={() => setActions(n => n + 1)}>Article action</button><output aria-label="Actions performed">{actions}</output></main>
    <DocumentContextPanel open={open} onOpenChange={setOpen} view={view} onViewChange={setView}>
      <p>{view} content</p>
      <button type="button" onClick={() => setEditing(true)}>Edit properties</button>
      {view === "outline" && <a href="#article-heading">Jump to article heading</a>}
    </DocumentContextPanel>
    <Dialog open={editing} onOpenChange={setEditing}>
      <DialogContent aria-describedby={undefined}>
        <DialogTitle>Edit details</DialogTitle>
        <input aria-label="Summary" />
      </DialogContent>
    </Dialog>
  </div>;
}

function measureReader(initialWidth: number) {
  let width = initialWidth;
  let resize = () => {};
  const originalBounds = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.dataset.testid !== "reader-layout") return originalBounds.call(this);
    return { width, height: 800, x: 0, y: 0, top: 0, bottom: 800, left: 0, right: width, toJSON: () => ({}) };
  });
  vi.stubGlobal("ResizeObserver", class {
    callback: () => void;
    constructor(callback: () => void) { this.callback = callback; }
    observe(element: HTMLElement) { if (element.dataset.testid === "reader-layout") resize = this.callback; }
    unobserve() {}
    disconnect() {}
  });
  return (nextWidth: number) => act(() => { width = nextWidth; resize(); });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("DocumentContextPanel", () => {
  it("switches floating views from one permanent edge group and toggles the active view closed", async () => {
    const user = userEvent.setup();
    measureReader(1088);
    render(<ContextHarness />);
    const edge = screen.getByRole("group", { name: "Document context" });
    expect(within(edge).getAllByRole("button").map(button => button.getAttribute("aria-label"))).toEqual(contextLabels);
    for (const button of within(edge).getAllByRole("button")) {
      expect(button).toHaveAttribute("aria-expanded", "false");
      expect(button.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    }
    const info = within(edge).getByRole("button", { name: "Document info" });
    await user.click(info);
    const panel = screen.getByRole("complementary", { name: "Document info" });
    expect(panel).toHaveAttribute("data-mode", "floating");
    expect(panel.parentElement).toBe(screen.getByTestId("reader-layout"));
    expect(info).toHaveAttribute("aria-controls", panel.id);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getAllByRole("group", { name: "Document context" })).toHaveLength(1);
    expect(within(panel).getByRole("button", { name: "Close document panel" })).toHaveFocus();

    const relations = within(edge).getByRole("button", { name: "Relations" });
    await user.click(relations);
    expect(screen.getByRole("complementary", { name: "Relations" })).toHaveTextContent("relations content");
    expect(relations).toHaveAttribute("aria-expanded", "true");
    expect(info).toHaveAttribute("aria-expanded", "false");
    await user.click(relations);
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    await waitFor(() => expect(relations).toHaveFocus());
    expect(relations).toHaveAttribute("aria-expanded", "false");
  });

  it.each([600, 1200])("focuses the local heading without changing route history at %ipx", async width => {
    const user = userEvent.setup();
    measureReader(width);
    render(<ContextHarness />);
    const heading = screen.getByRole("heading", { name: "Article heading" });
    const originalUrl = window.location.href;
    const originalHistory = window.history.state;
    await user.click(screen.getByRole("button", { name: "Table of contents" }));
    const panel = screen.getByRole(width >= 768 ? "complementary" : "dialog", { name: "On this page" });
    await user.click(within(panel).getByRole("link", { name: "Jump to article heading" }));
    expect(panel).not.toBeInTheDocument();
    await waitFor(() => expect(heading).toHaveFocus());
    expect(window.location.href).toBe(originalUrl);
    expect(window.history.state).toEqual(originalHistory);
    await user.click(screen.getByRole("button", { name: "Article action" }));
    expect(heading).not.toHaveAttribute("tabindex");
  });

  it("dismisses on outside click without swallowing the article action or stealing its focus", async () => {
    const user = userEvent.setup();
    measureReader(1200);
    render(<ContextHarness />);
    const history = screen.getByRole("button", { name: "Version history" });
    await user.click(history);
    await user.click(screen.getByRole("button", { name: "Article action" }));
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Actions performed")).toHaveTextContent("1");
    expect(screen.getByRole("button", { name: "Article action" })).toHaveFocus();
    await user.click(history);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    await waitFor(() => expect(history).toHaveFocus());
  });

  it("dismisses when keyboard focus leaves for the article without trapping or resetting focus", async () => {
    const user = userEvent.setup();
    measureReader(1200);
    render(<ContextHarness />);
    const info = screen.getByRole("button", { name: "Document info" });
    await user.click(info);
    const panel = screen.getByRole("complementary", { name: "Document info" });
    const close = within(panel).getByRole("button", { name: "Close document panel" });
    expect(close).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Article action" })).toHaveFocus();
    expect(panel).not.toBeInTheDocument();
  });

  it("keeps the inspector and a sibling properties dialog open while its draft is edited", async () => {
    const user = userEvent.setup();
    measureReader(1200);
    render(<ContextHarness />);
    await user.click(screen.getByRole("button", { name: "Document info" }));
    const panel = screen.getByRole("complementary", { name: "Document info" });
    const edit = within(panel).getByRole("button", { name: "Edit properties" });
    await user.click(edit);
    await user.type(screen.getByRole("textbox", { name: "Summary" }), "Unsaved summary");
    expect(panel).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("Unsaved summary");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Edit details" })).not.toBeInTheDocument();
    expect(panel).toBeVisible();
  });

  it("uses its own context controls in an overlay and returns focus to the selected edge trigger", async () => {
    const user = userEvent.setup();
    measureReader(600);
    render(<ContextHarness />);
    const edge = screen.getByRole("group", { name: "Document context" });
    const info = within(edge).getByRole("button", { name: "Document info" });
    const history = within(edge).getByRole("button", { name: "Version history" });
    await user.click(info);
    const panel = screen.getByRole("dialog", { name: "Document info" });
    expect(panel).toHaveAttribute("data-mode", "overlay");
    expect(panel).toHaveAttribute("aria-modal", "true");
    expect(screen.getByTestId("reader-layout")).not.toContainElement(panel);
    expect(screen.queryByRole("button", { name: "Article action" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("group", { name: "Document context" })).toHaveLength(1);
    expect(within(panel).getAllByRole("button").map(button => button.getAttribute("aria-label")))
      .toEqual(["Close document panel", ...contextLabels, null]);
    expect(within(panel).getByRole("button", { name: "Close document panel" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(within(panel).getByRole("button", { name: "Edit properties" })).toHaveFocus();
    await user.tab();
    expect(within(panel).getByRole("button", { name: "Close document panel" })).toHaveFocus();

    await user.click(within(panel).getByRole("button", { name: "Version history" }));
    expect(screen.getByRole("dialog", { name: "Version history" })).toHaveTextContent("history content");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(history).toHaveFocus());
    await user.click(info);
    await user.click(screen.getByRole("button", { name: "Close document panel" }));
    await waitFor(() => expect(info).toHaveFocus());
  });

  it("chooses the mode from the measured reader width as the container changes", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("innerWidth", 2560);
    const resizeReader = measureReader(600);
    render(<ContextHarness />);
    await user.click(screen.getByRole("button", { name: "Table of contents" }));
    expect(screen.getByRole("dialog", { name: "On this page" })).toHaveAttribute("data-mode", "overlay");
    resizeReader(1200);
    expect(await screen.findByRole("complementary", { name: "On this page" })).toHaveAttribute("data-mode", "floating");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Article action" })).toBeVisible();
    resizeReader(600);
    expect(await screen.findByRole("dialog", { name: "On this page" })).toHaveAttribute("data-mode", "overlay");
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  });
});
