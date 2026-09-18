import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MarkdownEditor } from "@/components/markdown-editor";

describe("AKB markdown editor slash menu", () => {
  it("connects the shared ten-command menu to the real AKB editor surface", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<MarkdownEditor value="" vault="team" onChange={onChange} />);

    const editor = await screen.findByRole("textbox");
    editor.focus();
    await user.keyboard("/");

    const menu = await screen.findByTestId("slash-command-menu");
    expect(menu.querySelectorAll('[role="option"]')).toHaveLength(10);
    expect(menu).toHaveTextContent("Insert block");

    fireEvent.mouseDown(menu.querySelector('[data-slash-command="table"]')!);
    fireEvent.click(menu.querySelector('[data-slash-command="table"]')!);
    await waitFor(() => expect(editor.querySelectorAll("table tr")).toHaveLength(3));
    expect(onChange.mock.calls.at(-1)?.[0]).toContain("| ");
  });

  it("keeps Source and read-only modes outside the slash lifecycle", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = render(
      <MarkdownEditor value="" vault="team" onChange={onChange} />,
    );

    await user.click(await screen.findByRole("button", { name: "Source" }));
    const source = screen.getByRole("textbox", { name: "Markdown source" });
    await user.type(source, "/");
    expect(screen.queryByTestId("slash-command-menu")).not.toBeInTheDocument();

    rerender(<MarkdownEditor value="" vault="team" readOnly onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "WYSIWYG" }));
    const readOnlyEditor = screen.getByRole("textbox");
    readOnlyEditor.focus();
    fireEvent.keyDown(readOnlyEditor, { key: "/" });
    expect(screen.queryByTestId("slash-command-menu")).not.toBeInTheDocument();
    expect(readOnlyEditor).toHaveAttribute("contenteditable", "false");
  });

  it("closes the connected menu on Escape without changing the draft", async () => {
    const user = userEvent.setup();
    render(<MarkdownEditor value="" vault="team" onChange={vi.fn()} />);
    const editor = await screen.findByRole("textbox");

    editor.focus();
    await user.keyboard("/");
    await screen.findByTestId("slash-command-menu");
    const escape = new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      key: "Escape",
    });
    editor.dispatchEvent(escape);

    await waitFor(() =>
      expect(screen.queryByTestId("slash-command-menu")).not.toBeInTheDocument(),
    );
    expect(editor).toHaveTextContent("/");
    expect(editor).toHaveAttribute("aria-expanded", "false");
  });
});
