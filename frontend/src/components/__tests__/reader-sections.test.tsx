import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { MarkdownRender } from "@/components/markdown-render";
import { DocumentFindBar, DocumentReaderToolsActions } from "@/components/document-reader-tools";
import { ReaderToolsProvider } from "@/contexts/reader-tools-context";

const markdown = '# Document\n\n[Jump](#child)\n\n## Parent\n\nParent text.\n\n### Child\n\nHidden needle.\n\n## Sibling\n\nSibling text.';
function Reader({ text = markdown }: { text?: string }) {
  return <MemoryRouter><ReaderToolsProvider><DocumentReaderToolsActions /><DocumentFindBar /><MarkdownRender markdown={text} searchable className="document-reading-flow" /></ReaderToolsProvider></MemoryRouter>;
}
beforeEach(() => localStorage.clear());

describe("section reading controls", () => {
  it("folds a section without hiding its sibling and reveals it for search", async () => {
    const user = userEvent.setup(); render(<Reader />);
    await user.click(await screen.findByRole("button", { name: "Collapse section: Parent" }));
    expect(screen.getByText("Hidden needle.")).not.toBeVisible();
    expect(screen.getByText("Sibling text.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Find in document" }));
    await user.type(screen.getByRole("searchbox"), "needle");
    await waitFor(() => expect(screen.getByRole("button", { name: "Collapse section: Parent" })).toHaveAttribute("aria-expanded", "true"));
    expect(screen.getByText(/Hidden/)).toBeVisible();
  });

  it("opens collapsed ancestors before following a section link and clears folds on another body", async () => {
    const user = userEvent.setup(); const view = render(<Reader />);
    await user.click(await screen.findByRole("button", { name: "Collapse section: Parent" }));
    await user.click(screen.getByRole("link", { name: "Jump" }));
    await waitFor(() => expect(screen.getByText("Hidden needle.")).toBeVisible());
    await user.click(screen.getByRole("button", { name: "Collapse section: Parent" }));
    view.rerender(<Reader text={markdown + '\n\nChanged.'} />);
    await waitFor(() => expect(screen.getByText("Hidden needle.")).toBeVisible());
    view.rerender(<Reader text={'# Short\n\nEnd.'} />);
    await waitFor(() => expect(screen.getByText("End.")).toBeVisible());
    expect(screen.queryByRole("button", { name: "Collapse section: Parent" })).toBeNull();
  });

  it("shows note previews, returns focus and preserves the original definition destination", async () => {
    const user = userEvent.setup();
    render(<Reader text={'# Notes\n\nA fact[^one].\n\n[^one]: Supporting **evidence**.'} />);
    await user.click(await screen.findByRole("button", { name: "Collapse section: Notes" }));
    await user.click(screen.getByRole("button", { name: "Expand section: Notes" }));
    const reference = await screen.findByRole("doc-noteref", { name: "Footnote 1" });
    await user.click(reference);
    const preview = await screen.findByRole("dialog", { name: "Footnote 1" });
    expect(within(preview).getByText("evidence")).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(reference).toHaveFocus());
    await user.click(reference);
    await user.click(screen.getByRole("button", { name: "Go to footnote" }));
    await waitFor(() => expect(screen.getByRole("doc-endnote", { name: "Footnote 1" })).toHaveFocus());
  });

  it("shares preferences with another reader and a renderer outside the tools provider", async () => {
    const user = userEvent.setup();
    const view = render(<><Reader /><MarkdownRender markdown="An independent preview." className="document-reading-flow" /></>);
    await user.click(screen.getByRole("button", { name: "Reading settings" }));
    await user.click(screen.getByRole("radio", { name: "Wide" }));
    const surfaces = view.container.querySelectorAll('[data-markdown-surface="viewer"]');
    expect(surfaces).toHaveLength(2);
    await waitFor(() => expect([...surfaces].every(surface => (surface as HTMLElement).style.getPropertyValue('--document-reading-measure') === '64rem')).toBe(true));
    expect(screen.getByText("An independent preview.")).toBeVisible();
  });
});
