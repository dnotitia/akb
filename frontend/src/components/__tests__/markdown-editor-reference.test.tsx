import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  discardAsset: vi.fn(),
  getAssetBlob: vi.fn(),
  getAttachmentMetadata: vi.fn(),
  getAttachmentRetentionPolicy: vi.fn(),
  getDocument: vi.fn(),
  getVaultFileDownloadUrl: vi.fn(),
  searchDocs: vi.fn(),
  uploadAsset: vi.fn(),
}));

vi.mock("@/lib/api", () => apiMocks);

import { MarkdownEditor } from "@/components/markdown-editor";

describe("AKB markdown editor @ references", () => {
  it("searches only accessible documents/files and stores canonical links", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    apiMocks.searchDocs.mockResolvedValue({
      degraded: false,
      results: [
        {
          matched_section: "A durable guide",
          title: "Guide",
          uri: "akb://team/coll/notes/doc/guide.md",
        },
        {
          matched_section: "A checked-in file",
          title: "README",
          uri: "akb://team/file/readme.md",
        },
        {
          title: "Ignored external result",
          uri: "https://example.com/result",
        },
      ],
    });

    function Controlled() {
      const [value, setValue] = useState("");
      return (
        <MarkdownEditor
          value={value}
          vault="team"
          document="notes/current.md"
          commit="abc123"
          onChange={(next, assetIds) => {
            onChange(next, assetIds);
            setValue(next);
          }}
        />
      );
    }

    render(<Controlled />);
    const editor = await screen.findByRole("textbox");
    editor.focus();
    await user.keyboard("@");

    const menu = await screen.findByTestId("markdown-reference-menu");
    await waitFor(() => expect(menu.querySelectorAll('[role="option"]')).toHaveLength(2));
    expect(menu.querySelector('[data-reference-section="person"]')).not.toBeInTheDocument();
    expect(menu.querySelector('[data-reference-section="issue"]')).not.toBeInTheDocument();
    expect(menu).toHaveTextContent("Guide");
    expect(menu).toHaveTextContent("README");
    expect(apiMocks.searchDocs).toHaveBeenCalledWith(
      "",
      "team",
      20,
      {},
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );

    await user.click(menu.querySelector('[data-reference-kind="document"]')!);
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    expect(onChange.mock.calls.at(-1)?.[0]).toContain(
      "[Guide](akb://team/coll/notes/doc/guide.md)",
    );
    expect(onChange.mock.calls.at(-1)?.[0]).not.toContain("https://example.com/result");
  });

  it("does not open the reference lifecycle from Source or read-only WYSIWYG", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <MarkdownEditor value="" vault="team" onChange={vi.fn()} />,
    );

    await user.click(await screen.findByRole("button", { name: "Source" }));
    const source = screen.getByRole("textbox", { name: "Markdown source" });
    await user.type(source, "@");
    expect(screen.queryByTestId("markdown-reference-menu")).not.toBeInTheDocument();

    rerender(<MarkdownEditor value="" vault="team" readOnly onChange={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "WYSIWYG" }));
    const readOnlyEditor = screen.getByRole("textbox");
    readOnlyEditor.focus();
    await user.keyboard("@");
    expect(screen.queryByTestId("markdown-reference-menu")).not.toBeInTheDocument();
  });
});
