import { render, screen } from "@testing-library/react";
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
});
