import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DocumentCreateDialog } from "@/components/document-create-dialog";

vi.mock("@/hooks/use-vault-tree", () => ({
  useVaultTree: () => ({
    tree: [
      {
        kind: "collection",
        name: "notes",
        path: "notes",
        children: [],
      },
    ],
    loading: false,
    refreshing: false,
    showingPreviousScope: false,
    unsupported: false,
    error: "",
    refetch: vi.fn(),
  }),
}));

afterEach(() => {
  localStorage.clear();
});

describe("document create slash menu", () => {
  it("consumes Escape in the slash menu before the parent discard dialog", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();

    render(
      <DocumentCreateDialog
        open
        vault="fixture"
        initialCollection="notes"
        onOpenChange={onOpenChange}
        onCreated={vi.fn()}
      />,
    );

    // The shared editor is lazy-loaded. Allow its cold module import to finish
    // under the full-suite workload before exercising slash-menu behavior.
    await screen.findByTestId("markdown-editor", {}, { timeout: 5_000 });
    const editor = await waitFor(() => {
      const element = document.querySelector<HTMLElement>(
        '#document-create-body [contenteditable="true"]',
      );
      if (!element) throw new Error("Markdown editor is not ready");
      return element;
    });
    await user.click(editor);
    await user.keyboard("/quot");
    await screen.findByTestId("slash-command-menu");

    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(screen.queryByTestId("slash-command-menu")).not.toBeInTheDocument();
    });
    expect(screen.queryByRole("dialog", { name: "Discard this draft?" })).not.toBeInTheDocument();
    expect(editor).toHaveTextContent("/quot");
    expect(onOpenChange).not.toHaveBeenCalled();
  });
});
