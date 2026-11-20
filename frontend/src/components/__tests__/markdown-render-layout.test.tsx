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

    const viewer = screen.getByRole("document", { name: "Document body" });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    const checkboxes = within(viewer).getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    expect(checkboxes[0]).toHaveAccessibleName(/Parent task/i);
    expect(checkboxes[1]).toBeChecked();
    expect(checkboxes[0]).toBeDisabled();
  });
  it("places UI metadata below an authored heading without changing its ID or repeating the title", async () => {
    render(<MarkdownRender markdown={"# Stable heading\n\nAuthored paragraph."} documentTitle="Stable heading"
      leadingMetadata={<div role="group" aria-label="Metadata">Author Ada · Draft</div>} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Stable heading" })).toHaveAttribute("id", "stable-heading"));
    const title = screen.getByRole("heading", { name: "Stable heading" });
    const metadata = screen.getByRole("group", { name: "Metadata" });
    expect(screen.getAllByRole("heading", { name: "Stable heading" })).toHaveLength(1);
    expect(title.compareDocumentPosition(metadata) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(metadata.compareDocumentPosition(screen.getByText("Authored paragraph.")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("adds a title before metadata for a document starting with a code block", async () => {
    render(<MarkdownRender markdown={"```text\n# This is source, not a heading\n```\n\nAuthored paragraph."} documentTitle="Canonical title"
      leadingMetadata={<div>Author Ada</div>} />);
    expect(await screen.findByRole("heading", { name: "Canonical title" })).toBeInTheDocument();
    expect(screen.getByText("# This is source, not a heading")).toBeInTheDocument();
    expect(screen.getByText("Author Ada")).toBeInTheDocument();
  });

  it("keeps the canonical title before a different body heading and preserves authored anchors and links", async () => {
    const { container, rerender } = render(<MarkdownRender markdown={"# Original heading\n\n[Read more][guide]\n\n# Original heading\n\n[guide]: https://example.com/guide"}
      documentTitle="Renamed document" documentTitleLevel={1}
      leadingMetadata={<div role="group" aria-label="Metadata">Author Ada</div>} />);
    const title = await screen.findByRole("heading", { name: "Renamed document", level: 1 });
    const bodyHeading = container.querySelector("#original-heading")!;
    const metadata = screen.getByRole("group", { name: "Metadata" });
    expect(title.compareDocumentPosition(metadata) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(metadata.compareDocumentPosition(bodyHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(bodyHeading.tagName).toBe("H2");
    expect(container.querySelector("#original-heading-2")).toHaveTextContent("Original heading");
    expect(screen.getByRole("link", { name: "Read more" })).toHaveAttribute("href", "https://example.com/guide");
    rerender(<MarkdownRender markdown={"# Original heading\n\n[Read more][guide]\n\n# Original heading\n\n[guide]: https://example.com/guide"}
      documentTitle="Original heading" documentTitleLevel={1} leadingMetadata={<div>Author Ada</div>} />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Original heading", level: 1 })).toHaveAttribute("id", "original-heading"));
    expect(screen.queryByText("Renamed document")).not.toBeInTheDocument();
    expect(container.querySelector("#original-heading-2")?.tagName).toBe("H2");
    expect(container.querySelectorAll("h1")).toHaveLength(1);
  });

  it("keeps a matching H2 as a body section rather than treating it as the document title", async () => {
    render(<MarkdownRender markdown={"## Details\n\nAuthored paragraph."} documentTitle="Details" documentTitleLevel={1} />);
    expect(await screen.findByRole("heading", { name: "Details", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Details", level: 2 })).toHaveAttribute("id", "details");
  });

  it("keeps one page heading and a continuous section hierarchy when the leading H1 serves as the title", async () => {
    const { container } = render(<MarkdownRender documentTitle="Guide" documentTitleLevel={1}
      markdown={"# Guide\n\n## Setup\n\n### Prerequisites\n\n# Another chapter\n\n## Details"} />);
    await screen.findByRole("heading", { name: "Guide", level: 1 });
    expect(Array.from(container.querySelectorAll("h1,h2,h3")).map(heading => heading.tagName)).toEqual(["H1", "H2", "H3", "H2", "H3"]);
    expect(container.querySelector("#another-chapter")).toHaveTextContent("Another chapter");
  });

  it("omits a UI summary that repeats the opening paragraph while retaining the authored paragraph", async () => {
    render(<MarkdownRender markdown={"Title\n=====\n\nOpening paragraph."} documentTitle="Title" summary="Opening paragraph."
      leadingMetadata={<span>Author Ada</span>} />);
    expect(await screen.findByRole("heading", { name: "Title" })).toHaveAttribute("id", "title");
    expect(screen.getAllByText("Opening paragraph.")).toHaveLength(1);
  });

});
