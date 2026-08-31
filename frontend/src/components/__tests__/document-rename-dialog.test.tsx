import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DocumentRenameDialog } from "@/components/document-rename-dialog";

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    detail: unknown;
    constructor(message: string, status: number, detail: unknown) {
      super(message);
      this.status = status;
      this.detail = detail;
    }
  },
  browseVault: vi.fn(),
  updateDocument: vi.fn(),
}));

import { browseVault, updateDocument } from "@/lib/api";

const browseVaultMock = browseVault as unknown as ReturnType<typeof vi.fn>;
const updateDocumentMock = updateDocument as unknown as ReturnType<typeof vi.fn>;

function renderDialog(onRenamed = vi.fn(), onOpenDocument = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    onRenamed,
    onOpenDocument,
    ...render(
      <QueryClientProvider client={queryClient}>
        <DocumentRenameDialog
          open
          onOpenChange={vi.fn()}
          vault="engineering"
          docId="notes/current.md"
          path="notes/current.md"
          title="Current title"
          onRenamed={onRenamed}
          onOpenDocument={onOpenDocument}
        />
      </QueryClientProvider>,
    ),
  };
}

beforeEach(() => {
  browseVaultMock.mockReset();
  updateDocumentMock.mockReset();
  browseVaultMock.mockResolvedValue({
    items: [
      { type: "document", name: "Existing title", path: "notes/existing.md" },
    ],
  });
  updateDocumentMock.mockResolvedValue({ path: "notes/current.md" });
});

afterEach(() => cleanup());

describe("DocumentRenameDialog", () => {
  it("requires an explicit action before using an exact sibling title", async () => {
    const user = userEvent.setup();
    const { onRenamed } = renderDialog();

    const title = screen.getByLabelText(/document title/i);
    await user.clear(title);
    await user.type(title, "Existing title");

    expect(await screen.findByRole("alert")).toHaveTextContent("already exists here");
    expect(screen.getByRole("button", { name: "Rename" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Use duplicate title" }));

    await waitFor(() =>
      expect(updateDocumentMock).toHaveBeenCalledWith(
        "engineering",
        "notes/current.md",
        {
          title: "Existing title",
          title_conflict_policy: "allow",
        },
      ),
    );
    expect(onRenamed).toHaveBeenCalledWith("Existing title");
  });

  it("does not treat a case-visible title variation as an exact conflict", async () => {
    const user = userEvent.setup();
    renderDialog();

    const title = screen.getByLabelText(/document title/i);
    await user.clear(title);
    await user.type(title, "Existing Title");
    expect(screen.queryByText(/already exists here/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Rename" }));

    await waitFor(() =>
      expect(updateDocumentMock).toHaveBeenCalledWith(
        "engineering",
        "notes/current.md",
        {
          title: "Existing Title",
          title_conflict_policy: "reject",
        },
      ),
    );
  });
});
