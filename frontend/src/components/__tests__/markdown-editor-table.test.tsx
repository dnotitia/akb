import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MarkdownEditor } from "@/components/markdown-editor";

const TABLE_MARKDOWN = [
  "| Service | Owner |",
  "| --- | --- |",
  "| Search | Platform |",
].join("\n");

describe("MarkdownEditor table interactions", () => {
  it("keeps a writable paragraph after a terminal table", () => {
    render(
      <MarkdownEditor
        value={TABLE_MARKDOWN}
        vault="team"
        ariaLabel="Document content"
        onChange={vi.fn()}
      />,
    );

    const editor = screen.getByRole("textbox", { name: "Document content" });
    expect(screen.getByRole("table", { name: "Editable table" })).toHaveClass(
      "!table",
      "w-full",
    );
    expect(editor.lastElementChild?.tagName).toBe("P");
  });

  it("moves focus below the table without a pointer-only escape", async () => {
    const user = userEvent.setup();
    render(
      <MarkdownEditor
        value={TABLE_MARKDOWN}
        vault="team"
        ariaLabel="Document content"
        onChange={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Continue below" }));
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Document content" })).toHaveFocus(),
    );
    expect(
      screen.getByRole("textbox", { name: "Document content" }).lastElementChild?.tagName,
    ).toBe("P");
  });

  it("adds rows and columns from the table-local toolbar", async () => {
    const user = userEvent.setup();
    render(
      <MarkdownEditor value={TABLE_MARKDOWN} vault="team" onChange={vi.fn()} />,
    );

    const table = screen.getByRole("table", { name: "Editable table" });
    const tableActions = within(table).getByRole("toolbar", { name: "Table actions" });
    expect(within(table).getAllByRole("row")).toHaveLength(2);
    expect(within(table).getAllByRole("columnheader")).toHaveLength(2);

    await user.click(within(tableActions).getByRole("button", { name: "Row" }));
    expect(
      within(screen.getByRole("table", { name: "Editable table" })).getAllByRole("row"),
    ).toHaveLength(3);

    const updatedTable = screen.getByRole("table", { name: "Editable table" });
    await user.click(
      within(updatedTable).getByRole("button", { name: "Column" }),
    );
    expect(
      within(screen.getByRole("table", { name: "Editable table" }))
        .getAllByRole("row")[0]
        .querySelectorAll("th, td"),
    ).toHaveLength(3);
  });

  it("removes the selected row and column without deleting the whole table", async () => {
    const user = userEvent.setup();
    render(
      <MarkdownEditor value={TABLE_MARKDOWN} vault="team" onChange={vi.fn()} />,
    );

    await user.click(screen.getByText("Search"));
    await user.click(screen.getByRole("button", { name: "Remove row" }));
    expect(
      within(screen.getByRole("table", { name: "Editable table" })).getAllByRole("row"),
    ).toHaveLength(1);

    await user.click(screen.getByText("Search"));
    await user.click(screen.getByRole("button", { name: "Remove column" }));
    const remainingTable = screen.getByRole("table", { name: "Editable table" });
    expect(remainingTable.querySelectorAll("th, td")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Delete table" })).toBeVisible();
  });

  it("deletes the entire table and restores an editable cursor target", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <MarkdownEditor
        value={TABLE_MARKDOWN}
        vault="team"
        ariaLabel="Document content"
        onChange={onChange}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Delete table" }));

    expect(screen.queryByRole("table", { name: "Editable table" })).toBeNull();
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Document content" })).toHaveFocus(),
    );
    await waitFor(() =>
      expect(onChange.mock.calls.some(([markdown]) => !markdown.includes("| Service"))).toBe(true),
    );
    expect(
      screen.getByRole("textbox", { name: "Document content" }).lastElementChild?.tagName,
    ).toBe("P");
  });

  it("does not expose editing actions in read-only mode", () => {
    render(
      <MarkdownEditor value={TABLE_MARKDOWN} vault="team" readOnly />,
    );

    expect(screen.getByRole("table", { name: "Table" })).toBeVisible();
    expect(screen.queryByRole("toolbar", { name: "Table actions" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete table" })).toBeNull();
  });
});
