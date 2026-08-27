import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MarkdownEditor } from "@/components/markdown-editor";
import { normalizeEditorLinkUrl } from "@/lib/editor-link";

describe("MarkdownEditor formatting toolbar", () => {
  it("uses one tab stop and arrow keys to move through toolbar controls", async () => {
    const user = userEvent.setup();
    render(<MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />);

    await user.tab();
    expect(screen.getByRole("button", { name: "Paragraph" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("button", { name: "Heading 1" })).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("button", { name: "Insert image" })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(screen.getByRole("button", { name: "Paragraph" })).toHaveFocus();
  });

  it("loads multiline code as distinct code lines", () => {
    const onChange = vi.fn();
    render(
      <MarkdownEditor
        value={'```\nline one\nline two\n```'}
        vault="team"
        ariaLabel="Document content"
        onChange={onChange}
      />,
    );

    const editor = screen.getByRole("textbox", { name: "Document content" });
    const codeBlock = editor.querySelector("pre");
    expect(codeBlock).not.toBeNull();
    expect(codeBlock?.children).toHaveLength(2);
    expect(codeBlock?.children[0]).toHaveTextContent("line one");
    expect(codeBlock?.children[1]).toHaveTextContent("line two");
  });

  it("shows recoverable validation for unsafe link destinations", async () => {
    const user = userEvent.setup();
    render(
      <MarkdownEditor
        value="Plate docs"
        vault="team"
        ariaLabel="Document content"
        onChange={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Insert link" }));
    await user.type(screen.getByLabelText("URL"), "javascript:alert(1)");
    await user.click(screen.getByRole("button", { name: "Insert link" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/http\(s\), email, phone/i);
    await waitFor(() => expect(screen.getByLabelText("URL")).toHaveFocus());
  });

  it("normalizes common safe links and rejects active-content URLs", () => {
    expect(normalizeEditorLinkUrl("platejs.org")).toBe("https://platejs.org");
    expect(normalizeEditorLinkUrl("/vault/team")).toBe("/vault/team");
    expect(normalizeEditorLinkUrl("mailto:owner@example.com")).toBe(
      "mailto:owner@example.com",
    );
    expect(normalizeEditorLinkUrl("javascript:alert(1)")).toBeNull();
  });

  it.each([
    ["heading", "## Terminal heading"],
    ["blockquote", "> Terminal quote"],
    ["code", "```\nterminal code\n```"],
    ["table", "| A | B |\n| --- | --- |\n| 1 | 2 |"],
  ])("does not persist the editor-only trailing paragraph after a %s", async (_name, value) => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <MarkdownEditor
        value={value}
        vault="team"
        ariaLabel="Document content"
        onChange={onChange}
      />,
    );
    const editor = screen.getByRole("textbox", { name: "Document content" });
    const trailingParagraph = editor.lastElementChild as HTMLElement;
    expect(trailingParagraph.tagName).toBe("P");
    await user.click(trailingParagraph);
    await user.type(trailingParagraph, "x");
    await user.keyboard("{Backspace}");

    await waitFor(() => expect(onChange).toHaveBeenCalled());
    const latest = onChange.mock.calls.at(-1)?.[0] as string;
    expect(latest).not.toContain("\u200b");
    expect(latest).not.toContain("\ufeff");
  });
});
