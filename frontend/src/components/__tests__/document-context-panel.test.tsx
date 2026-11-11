import { useRef, useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DocumentContextPanel, type DocumentContextView } from "../document-context-panel";
import { Dialog, DialogContent, DialogTitle } from "../ui/dialog";

afterEach(cleanup);

function Harness({ initialView = "info" }: { initialView?: DocumentContextView }) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<DocumentContextView>(initialView);
  const [editing, setEditing] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  return <>
    <button ref={trigger} onClick={() => setOpen(true)}>Open details</button>
    <button>Article action</button>
    <DocumentContextPanel id="document-info" open={open} onOpenChange={setOpen} view={view} onViewChange={setView} triggerRef={trigger}
      info={<><p>Document properties content</p><button onClick={() => setEditing(true)}>Edit properties</button></>}
      history={<p>Version history content</p>} relations={<p>Connection management content</p>} />
    <Dialog open={editing} onOpenChange={setEditing}><DialogContent aria-describedby={undefined}><DialogTitle>Edit properties</DialogTitle><input aria-label="Summary" /></DialogContent></Dialog>
  </>;
}

describe("document information", () => {
  it("separates Properties and History without mixing reading navigation or connections into Details", async () => {
    const user = userEvent.setup(); render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Open details" });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Details" });
    expect(within(dialog).getByRole("tab", { name: "Properties" })).toHaveFocus();
    expect(within(dialog).getByText("Document properties content")).toBeVisible();
    expect(screen.queryByText("Version history content")).not.toBeInTheDocument();
    expect(screen.queryByText("Connection management content")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("On this page")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("tab", { name: "Version history" }));
    expect(within(dialog).getByText("Version history content")).toBeVisible();
    expect(screen.queryByText("Document properties content")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Close details" }));
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("supports manual keyboard activation of information tabs", async () => {
    const user = userEvent.setup(); render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open details" }));
    await user.keyboard("{ArrowRight}");
    const history = screen.getByRole("tab", { name: "Version history" });
    expect(history).toHaveFocus(); expect(history).toHaveAttribute("aria-selected", "false");
    await user.keyboard("{Enter}");
    expect(history).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Version history content")).toBeVisible();
  });

  it("closes Details before its enclosing document preview on Escape", async () => {
    const user = userEvent.setup(); const closePreview = vi.fn();
    render(<Dialog open><DialogContent aria-describedby={undefined} onEscapeKeyDown={event => { event.preventDefault(); closePreview(); }}><DialogTitle>Document preview</DialogTitle><Harness /></DialogContent></Dialog>);
    await user.click(screen.getByRole("button", { name: "Open details" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Details" })).not.toBeInTheDocument();
    expect(closePreview).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Document preview" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Open details" })).toHaveFocus());
    await user.keyboard("{Escape}"); expect(closePreview).toHaveBeenCalledOnce();
  });

  it("keeps a supporting editor mounted and restores its opener across viewport changes", async () => {
    const user = userEvent.setup(); render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open details" }));
    const edit = screen.getByRole("button", { name: "Edit properties" });
    await user.click(edit);
    const input = screen.getByRole("textbox", { name: "Summary" });
    await user.type(input, "Preserved draft");
    fireEvent(window, new Event("resize"));
    expect(screen.getByRole("textbox", { name: "Summary" })).toBe(input);
    expect(input).toHaveValue("Preserved draft");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Details" })).toBeVisible();
    await waitFor(() => expect(edit).toHaveFocus());
  });

  it("opens connection management as its own surface without document-information tabs", async () => {
    const user = userEvent.setup(); render(<Harness initialView="relations" />);
    await user.click(screen.getByRole("button", { name: "Open details" }));
    const dialog = screen.getByRole("dialog", { name: "Connections" });
    expect(within(dialog).getByText("Connection management content")).toBeVisible();
    expect(within(dialog).queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.queryByText("Document properties content")).not.toBeInTheDocument();
  });

  it("waits for the requested history control before restoring comparison focus", async () => {
    const user = userEvent.setup(); const finished = vi.fn();
    function HistoryHarness() {
      const opener = useRef<HTMLButtonElement>(null); const target = useRef<HTMLButtonElement>(null);
      const [ready, setReady] = useState(false);
      return <><button ref={opener}>Details opener</button><DocumentContextPanel open onOpenChange={() => {}} view="history" onViewChange={() => {}} triggerRef={opener} info={null} relations={null}
        focusTargetRef={target} onFocusTarget={finished} history={ready ? <button ref={target}>Compare selected revision</button> : <button onClick={() => setReady(true)}>Finish loading history</button>} /></>;
    }
    render(<HistoryHarness />);
    expect(finished).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Finish loading history" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Compare selected revision" })).toHaveFocus();
      expect(finished).toHaveBeenCalledOnce();
    });
  });
});
