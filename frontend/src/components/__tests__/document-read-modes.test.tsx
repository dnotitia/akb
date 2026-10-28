import { useRef, useState } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DocumentCopyButton, DocumentReadModes, type DocumentCopyHandle } from "../document-reading-controls";

function Modes() {
  const [view, setView] = useState<"rendered" | "raw">("rendered");
  return <><DocumentReadModes view={view} onChange={setView} idPrefix="reader" contentId="reader-content" />
    <div id="reader-content">{view}</div><button>Next action</button></>;
}

function CopyFromMenu({ content, kind, onCopied }: { content: string; kind?: "markdown" | "link"; onCopied?: (label: string) => void }) {
  const actionRef = useRef<DocumentCopyHandle>(null);
  const returnFocusRef = useRef<HTMLButtonElement>(null);
  return <><button ref={returnFocusRef} onClick={() => actionRef.current?.copy()}>More actions</button>
    <DocumentCopyButton content={content} kind={kind} onCopied={onCopied} actionRef={actionRef} returnFocusRef={returnFocusRef} hidden /></>;
}

describe("DocumentReadModes", () => {
  it("selects one reading mode and moves focus with radio-group keyboard controls", async () => {
    const user = userEvent.setup();
    render(<Modes />);
    const rendered = screen.getByRole("radio", { name: "Preview" });
    const raw = screen.getByRole("radio", { name: "Raw markdown" });
    expect(screen.getByRole("radiogroup", { name: "Document view mode" })).toBeInTheDocument();
    expect(rendered).toHaveAttribute("aria-checked", "true");
    expect(raw).toHaveAttribute("aria-checked", "false");
    expect(raw).toHaveAttribute("aria-controls", "reader-content");
    await user.tab();
    expect(rendered).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(raw).toHaveFocus();
    expect(raw).toHaveAttribute("aria-checked", "true");
    expect(rendered).toHaveAttribute("aria-checked", "false");
    expect(raw).toHaveAttribute("tabIndex", "0");
    expect(rendered).toHaveAttribute("tabIndex", "-1");
    expect(document.getElementById("reader-content")).toHaveTextContent("raw");
    await user.keyboard("{ArrowRight}");
    expect(rendered).toHaveFocus();
    expect(rendered).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{ArrowLeft}");
    expect(raw).toHaveFocus();
    expect(raw).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{ArrowUp}");
    expect(rendered).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{ArrowDown}");
    expect(raw).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{Home}");
    expect(rendered).toHaveFocus();
    expect(rendered).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{End}");
    expect(raw).toHaveFocus();
    expect(raw).toHaveAttribute("aria-checked", "true");
    await user.tab();
    expect(screen.getByRole("button", { name: "Next action" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(raw).toHaveFocus();
  });

  it("keeps the current mode selected when its icon is activated again", async () => {
    const user = userEvent.setup();
    render(<Modes />);
    const raw = screen.getByRole("radio", { name: "Raw markdown" });
    await user.click(raw);
    expect(raw).toHaveAttribute("aria-checked", "true");
    await user.click(raw);
    await user.keyboard(" ");
    expect(raw).toHaveAttribute("aria-checked", "true");
    expect(screen.getAllByRole("radio", { checked: true })).toHaveLength(1);
    expect(raw).toHaveTextContent("");
  });

  it("does not open a tooltip while entering or switching modes with the keyboard", async () => {
    const user = userEvent.setup();
    render(<Modes />);
    await user.tab();
    expect(screen.getByRole("radio", { name: "Preview" })).toHaveFocus();
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await user.keyboard("{ArrowRight}{ArrowLeft}{End}{Home}");
    expect(screen.getByRole("radio", { name: "Preview" })).toHaveFocus();
    expect(screen.getByRole("radio", { name: "Preview" })).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps deliberate hover help but dismisses it when the mode is selected", async () => {
    const user = userEvent.setup();
    render(<Modes />);
    const raw = screen.getByRole("radio", { name: "Raw markdown" });
    await user.hover(raw);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(await screen.findByRole("tooltip", {}, { timeout: 2000 })).toHaveTextContent("Raw markdown");
    await user.click(raw);
    expect(raw).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("radio", { name: "Preview" })).toHaveFocus();
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("ignores a pending hover timer after keyboard selection until a fresh hover", async () => {
    const user = userEvent.setup();
    render(<Modes />);
    const raw = screen.getByRole("radio", { name: "Raw markdown" });
    await user.hover(raw);
    await user.tab();
    await user.keyboard("{ArrowRight}");
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 700)); });
    expect(raw).toHaveFocus();
    expect(raw).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await user.unhover(raw);
    await user.hover(raw);
    expect(await screen.findByRole("tooltip", {}, { timeout: 2000 })).toHaveTextContent("Raw markdown");
  });

  it("offers the complete markdown for manual copying when clipboard access fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValueOnce(new Error("Clipboard unavailable"));
    const content = "# Complete markdown\n\nA paragraph to copy.";
    render(<DocumentCopyButton content={content} />);
    const trigger = screen.getByRole("button", { name: "Copy markdown" });
    await user.click(trigger);
    expect(await screen.findByRole("dialog", { name: "Copy markdown" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Markdown to copy" })).toHaveValue(content);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("copies through a persistent hidden control when invoked by an overflow action", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValueOnce(undefined);
    const onCopied = vi.fn();
    const content = "# Source requested from overflow";
    render(<CopyFromMenu content={content} onCopied={onCopied} />);
    expect(screen.queryByRole("button", { name: "Copy markdown" })).not.toBeInTheDocument();
    expect(document.querySelector(".document-copy-button")).toHaveAttribute("hidden");
    await user.click(screen.getByRole("button", { name: "More actions" }));
    expect(writeText).toHaveBeenCalledWith(content);
    expect(onCopied).toHaveBeenCalledWith("Markdown copied");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps manual copying available for hidden actions and returns focus to overflow", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValueOnce(new Error("Clipboard unavailable"));
    const content = "# Manual source\n\nThe whole document remains selectable.";
    render(<CopyFromMenu content={content} />);
    const trigger = screen.getByRole("button", { name: "More actions" });
    await user.click(trigger);
    expect(await screen.findByRole("dialog", { name: "Copy markdown" })).toBeInTheDocument();
    const source = screen.getByRole("textbox", { name: "Markdown to copy" });
    expect(source).toHaveValue(content);
    expect(source).toHaveFocus();
    expect((source as HTMLTextAreaElement).selectionStart).toBe(0);
    expect((source as HTMLTextAreaElement).selectionEnd).toBe(content.length);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("offers the document link for manual copying when a hidden link action is blocked", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValueOnce(new Error("Clipboard unavailable"));
    const content = "akb://vault/notes/document.md";
    const onCopied = vi.fn();
    render(<CopyFromMenu content={content} kind="link" onCopied={onCopied} />);
    const trigger = screen.getByRole("button", { name: "More actions" });
    await user.click(trigger);
    expect(await screen.findByRole("dialog", { name: "Copy document link" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Document link to copy" })).toHaveValue(content);
    expect(screen.queryByRole("dialog", { name: "Copy markdown" })).not.toBeInTheDocument();
    expect(onCopied).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
