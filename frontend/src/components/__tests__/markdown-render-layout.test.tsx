import { render, screen, within } from "@testing-library/react";
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
});
