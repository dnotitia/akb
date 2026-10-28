import { render, screen, within, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MarkdownRender } from "@/components/markdown-render";

describe("MarkdownRender layout", () => {
  it("gives tables a centered reading-measure wrapper with horizontal overflow", () => {
    render(
      <MarkdownRender
        markdown={[
          "| Name | Owner | Status |",
          "| --- | --- | --- |",
          "| Search | Platform | Active |",
        ].join("\n")}
        className="document-reading-flow"
      />,
    );

    const table = screen.getByRole("table");
    expect(table).toHaveClass("w-max", "min-w-full");
    expect(table.parentElement).toHaveClass("akb-md-table", "overflow-x-auto");
  });

  it("uses the shared block renderer for code and nested task items", async () => {
    render(
      <MarkdownRender
        markdown={[
          "# Heading",
          "",
          "> Quote",
          "",
          "- [ ] Parent task",
          "  - [x] Child task",
          "",
          "```typescript",
          "const answer: number = 42",
          "```",
        ].join("\n")}
      />,
    );

    const code = await screen.findByRole("region", { name: "Scrollable typescript code block" });
    expect(code).toHaveAttribute("data-markdown-code", "true");
    expect(code).toHaveAttribute("tabindex", "0");
    expect(code.querySelector(".hljs-keyword")).toHaveTextContent("const");

    const checkboxes = within(screen.getByRole("textbox")).getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    expect(checkboxes[0]).toHaveAccessibleName(/Parent task/i);
    expect(checkboxes[1]).toBeChecked();
    expect(checkboxes[0]).toBeDisabled();
  });
  it("places UI metadata below an authored heading without changing its ID or repeating the title", async () => {
    render(<MarkdownRender markdown={"# Stable heading\n\nAuthored paragraph."} fallbackTitle="Stable heading"
      leadingMetadata={<div role="group" aria-label="Metadata">Author Ada · Draft</div>} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Stable heading" })).toHaveAttribute("id", "stable-heading"));
    const title = screen.getByRole("heading", { name: "Stable heading" });
    const metadata = screen.getByRole("group", { name: "Metadata" });
    expect(screen.getAllByRole("heading", { name: "Stable heading" })).toHaveLength(1);
    expect(title.compareDocumentPosition(metadata) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(metadata.compareDocumentPosition(screen.getByText("Authored paragraph.")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("adds a title before metadata for a document starting with a code block", async () => {
    render(<MarkdownRender markdown={"```text\n# This is source, not a heading\n```\n\nAuthored paragraph."} fallbackTitle="Canonical title"
      leadingMetadata={<div>Author Ada</div>} />);
    expect(await screen.findByRole("heading", { name: "Canonical title" })).toBeInTheDocument();
    expect(screen.getByText("# This is source, not a heading")).toBeInTheDocument();
    expect(screen.getByText("Author Ada")).toBeInTheDocument();
  });

  it("omits a UI summary that repeats the opening paragraph while retaining the authored paragraph", async () => {
    render(<MarkdownRender markdown={"Title\n=====\n\nOpening paragraph."} fallbackTitle="Title" summary="Opening paragraph."
      leadingMetadata={<span>Author Ada</span>} />);
    expect(await screen.findByRole("heading", { name: "Title" })).toHaveAttribute("id", "title");
    expect(screen.getAllByText("Opening paragraph.")).toHaveLength(1);
  });

});
