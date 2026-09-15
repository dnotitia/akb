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

  it("does not guess a table target when no cell is selected", () => {
    render(
      <MarkdownEditor
        value={TABLE_MARKDOWN}
        vault="team"
        ariaLabel="Document content"
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Continue below" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete table" })).toBeDisabled();
    expect(
      screen.getByRole("textbox", { name: "Document content" }).lastElementChild?.tagName,
    ).toBe("P");
  });

  it("keeps row and column commands disabled outside a selected cell", () => {
    render(<MarkdownEditor value={TABLE_MARKDOWN} vault="team" onChange={vi.fn()} />);

    const table = screen.getByRole("table", { name: "Editable table" });
    const actions = within(table).getByRole("toolbar", { name: "Table actions" });
    expect(within(table).getAllByRole("row")).toHaveLength(2);
    expect(within(table).getAllByRole("columnheader")).toHaveLength(2);
    expect(
      within(actions).getByRole("button", { name: "Add row after selected row" }),
    ).toBeDisabled();
    expect(
      within(actions).getByRole("button", { name: "Add column right of selected column" }),
    ).toBeDisabled();
    expect(within(actions).getByRole("button", { name: "Remove selected row" })).toBeDisabled();
    expect(within(actions).getByRole("button", { name: "Remove selected column" })).toBeDisabled();
  });

  it("does not delete the table when there is no valid selected cell", async () => {
    const onChange = vi.fn();
    render(
      <MarkdownEditor
        value={TABLE_MARKDOWN}
        vault="team"
        ariaLabel="Document content"
        onChange={onChange}
      />,
    );

    await waitFor(() => expect(onChange).toHaveBeenCalled());
    const initialChangeCount = onChange.mock.calls.length;
    expect(screen.getByRole("button", { name: "Delete table" })).toBeDisabled();
    expect(screen.getByRole("table", { name: "Editable table" })).toBeInTheDocument();
    expect(onChange).toHaveBeenCalledTimes(initialChangeCount);
  });

  it("does not expose editing actions in read-only mode", () => {
    render(<MarkdownEditor value={TABLE_MARKDOWN} vault="team" readOnly />);

    expect(screen.getByRole("table", { name: "Table" })).toBeVisible();
    expect(screen.queryByRole("toolbar", { name: "Table actions" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete table" })).toBeNull();
  });

  it("uses the shared table insertion control in the AKB author surface", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <MarkdownEditor
        value="before"
        vault="team"
        ariaLabel="Document content"
        onChange={onChange}
      />,
    );

    expect(screen.getAllByRole("button", { name: "Insert table" })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Insert table" }));

    expect(screen.getByRole("table", { name: "Editable table" })).toBeVisible();
    await waitFor(() =>
      expect(
        onChange.mock.calls.some(([markdown]) =>
          markdown.includes("| --- | --- | --- |"),
        ),
      ).toBe(true),
    );
  });
});
