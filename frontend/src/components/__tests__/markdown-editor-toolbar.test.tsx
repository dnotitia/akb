import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MarkdownEditor } from "@/components/markdown-editor";
import { normalizeEditorLinkUrl } from "@/lib/editor-link";
import * as api from "@/lib/api";

describe("MarkdownEditor formatting toolbar", () => {
  it("uses one tab stop and arrow keys to move through toolbar controls", async () => {
    const user = userEvent.setup();
    render(<MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />);

    await user.tab();
    expect(screen.getByRole("button", { name: "WYSIWYG" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Source" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Paragraph" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("button", { name: "Heading 1" })).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("button", { name: "Insert image" })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(screen.getByRole("button", { name: "Paragraph" })).toHaveFocus();
  });

  it("applies the shared bold command to the selected AKB document text", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <MarkdownEditor
        value="Draft"
        vault="team"
        ariaLabel="Document content"
        onChange={onChange}
      />,
    );

    const editor = screen.getByRole("textbox", { name: "Document content" });
    editor.focus();
    await user.keyboard("{Control>}a{/Control}");
    await user.click(screen.getByRole("button", { name: "Bold" }));

    await waitFor(() => {
      const latest = onChange.mock.calls.at(-1)?.[0] as string | undefined;
      expect(latest).toContain("**Draft**");
    });
  });

  it("edits Markdown Source through the shared surface and tracks referenced attachments", async () => {
    const user = userEvent.setup();
    const previousAssetId = "00000000-0000-4000-8000-000000000001";
    const nextAssetId = "00000000-0000-4000-8000-000000000002";
    const onChange = vi.fn();
    render(
      <MarkdownEditor
        value={`![Before](/api/assets/${previousAssetId})`}
        vault="team"
        ariaLabel="Document content"
        onChange={onChange}
      />,
    );

    await screen.findByRole("textbox", { name: "Document content" });
    await user.click(screen.getByRole("button", { name: "Source" }));

    const source = screen.getByRole("textbox", { name: "Document content" });
    expect(source).toHaveValue(`![Before](/api/assets/${previousAssetId})`);
    expect(screen.queryByRole("toolbar", { name: "Text formatting" })).not.toBeInTheDocument();

    fireEvent.change(source, {
      target: { value: `![After](/api/assets/${nextAssetId})` },
    });
    await waitFor(() => expect(source).toHaveValue(`![After](/api/assets/${nextAssetId})`));
    await waitFor(() =>
      expect(onChange).toHaveBeenLastCalledWith(`![After](/api/assets/${nextAssetId})`, [nextAssetId]),
    );

    await user.click(screen.getByRole("button", { name: "WYSIWYG" }));
    expect(screen.getByRole("textbox", { name: "Document content" }).querySelector("img")).toHaveAttribute(
      "data-markdown-target",
      `/api/assets/${nextAssetId}`,
    );
  });

  it("opens Source with the current unsaved WYSIWYG draft when the editor owns its value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <MarkdownEditor
        value="Draft"
        vault="team"
        ariaLabel="Document content"
        onChange={onChange}
      />,
    );

    const editor = await screen.findByRole("textbox", { name: "Document content" });
    editor.focus();
    await user.keyboard("{Control>}a{/Control}");
    await user.click(screen.getByRole("button", { name: "Bold" }));
    await user.click(screen.getByRole("button", { name: "Source" }));

    expect(screen.getByRole("textbox", { name: "Document content" })).toHaveValue(
      "**Draft**",
    );
    expect(onChange).toHaveBeenCalled();
  });

  it("applies a dynamic readOnly change to the Source field", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={onChange} />,
    );

    await user.click(await screen.findByRole("button", { name: "Source" }));
    const source = screen.getByRole("textbox", { name: "Markdown source" });
    rerender(<MarkdownEditor value="Draft" vault="team" readOnly onChange={onChange} />);

    expect(source).toHaveProperty("readOnly", true);
    await user.type(source, " should stay unchanged");
    expect(source).toHaveValue("Draft");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("loads multiline code through the shared Tiptap code block", () => {
    const onChange = vi.fn();
    render(
      <MarkdownEditor
        value={"```\nline one\nline two\n```"}
        vault="team"
        ariaLabel="Document content"
        onChange={onChange}
      />,
    );

    const editor = screen.getByRole("textbox", { name: "Document content" });
    const codeBlock = editor.querySelector("pre");
    expect(codeBlock).not.toBeNull();
    expect(codeBlock).toHaveTextContent(/line one\s+line two/);
  });

  it("shows recoverable validation for unsafe link destinations", async () => {
    const user = userEvent.setup();
    render(
      <MarkdownEditor
        value="Markdown docs"
        vault="team"
        ariaLabel="Document content"
        onChange={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Insert link" }));
    await user.type(screen.getByLabelText("URL"), "javascript:alert(1)");
    await user.click(screen.getByRole("button", { name: "Insert link" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      /http\(s\), email, phone/i,
    );
    await waitFor(() => expect(screen.getByLabelText("URL")).toHaveFocus());
  });

  it("connects the shared search UI to the current vault through the AKB adapter", async () => {
    const user = userEvent.setup();
    const response: Awaited<ReturnType<typeof api.searchDocs>> = {
      query: "diagram",
      total: 0,
      returned: 0,
      total_matches: 0,
      results: [],
    };
    const searchDocs = vi.spyOn(api, "searchDocs").mockResolvedValue(response);

    try {
      render(<MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />);
      await user.click(screen.getByRole("button", { name: "Insert link" }));
      const search = screen.getByRole("combobox", { name: "Search Vault resources" });
      fireEvent.change(search, { target: { value: "diagram" } });

      expect(await screen.findByText("No matching documents or files found.")).toBeVisible();
      expect(searchDocs).toHaveBeenCalledWith(
        "diagram",
        "team",
        20,
        {},
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
      await user.click(screen.getByRole("button", { name: "Cancel" }));
    } finally {
      searchDocs.mockRestore();
    }
  });

  it("normalizes common safe links and rejects active-content URLs", () => {
    expect(normalizeEditorLinkUrl("markdown.example.org")).toBe(
      "https://markdown.example.org",
    );
    expect(normalizeEditorLinkUrl("/vault/team")).toBe("/vault/team");
    expect(normalizeEditorLinkUrl("mailto:owner@example.com")).toBe(
      "mailto:owner@example.com",
    );
    expect(normalizeEditorLinkUrl("akb://team/coll/notes/doc/guide.md/")).toBe(
      "akb://team/coll/notes/doc/guide.md",
    );
    expect(normalizeEditorLinkUrl("javascript:alert(1)")).toBeNull();
  });

  it.each([
    ["heading", "## Terminal heading"],
    ["blockquote", "> Terminal quote"],
    ["code", "```\nterminal code\n```"],
    ["table", "| A | B |\n| --- | --- |\n| 1 | 2 |"],
  ])(
    "does not persist the editor-only trailing paragraph after a %s",
    async (_name, value) => {
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
    },
  );
});
