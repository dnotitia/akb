import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DocumentMoveDialog } from "@/components/document-move-dialog";
import { previewDocumentSlug } from "@/lib/document-move";

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
  moveDocument: vi.fn(),
}));

import { ApiError, browseVault, moveDocument } from "@/lib/api";

const browseVaultMock = browseVault as unknown as ReturnType<typeof vi.fn>;
const moveDocumentMock = moveDocument as unknown as ReturnType<typeof vi.fn>;

function renderDialog(onMoved = vi.fn(), onOpenChange = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    onMoved,
    onOpenChange,
    ...render(
      <QueryClientProvider client={queryClient}>
        <DocumentMoveDialog
          open
          onOpenChange={onOpenChange}
          vault="engineering"
          path="drafts/api-contract.md"
          title="API contract"
          onMoved={onMoved}
        />
      </QueryClientProvider>,
    ),
  };
}

beforeEach(() => {
  browseVaultMock.mockReset();
  moveDocumentMock.mockReset();
  browseVaultMock.mockResolvedValue({
    items: [
      { type: "collection", path: "archive" },
      { type: "collection", path: "drafts" },
      { type: "collection", path: "projects/akb" },
      { type: "collection", path: "overview" },
    ],
  });
  moveDocumentMock.mockResolvedValue({
    kind: "document_write",
    uri: "akb://engineering/coll/archive/doc/final-contract.md",
    vault: "engineering",
    path: "archive/final-contract.md",
    commit_hash: "abc1234",
    current_commit: "abc1234",
    action: "moved",
  });
});

afterEach(() => cleanup());

describe("DocumentMoveDialog", () => {
  it("reviews the path change and submits only changed destination fields", async () => {
    const user = userEvent.setup();
    const { onMoved, onOpenChange } = renderDialog();

    expect(screen.getAllByText("drafts/api-contract.md")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Move document" })).toBeDisabled();

    const collection = await screen.findByLabelText("Target collection");
    await user.click(collection);
    expect(screen.queryByRole("menuitemradio", { name: /overview/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitemradio", { name: "archive" }));

    const fileName = screen.getByLabelText("File name");
    await user.clear(fileName);
    await user.type(fileName, "Final Contract");
    await user.type(
      screen.getByLabelText(/commit message/i),
      "Move the approved contract",
    );

    expect(screen.getByText("archive/final-contract.md")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Move document" }));

    await waitFor(() =>
      expect(moveDocumentMock).toHaveBeenCalledWith(
        "engineering",
        "drafts/api-contract.md",
        {
          collection: "archive",
          slug: "Final Contract",
          message: "Move the approved contract",
        },
      ),
    );
    expect(onMoved).toHaveBeenCalledWith(
      expect.objectContaining({ path: "archive/final-contract.md" }),
    );
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps the form open with a recoverable collision message", async () => {
    moveDocumentMock.mockRejectedValue(
      new Error("Document already exists at path: archive/api-contract.md"),
    );
    const user = userEvent.setup();
    const { onMoved, onOpenChange } = renderDialog();

    await user.click(await screen.findByLabelText("Target collection"));
    await user.click(screen.getByRole("menuitemradio", { name: "archive" }));
    await user.click(screen.getByRole("button", { name: "Move document" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A document already uses that file name in the selected collection",
    );
    expect(screen.getByLabelText("Target collection")).toHaveTextContent("archive");
    expect(onMoved).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("keeps rename available when only the destination list fails", async () => {
    browseVaultMock.mockRejectedValue(new Error("browse unavailable"));
    const user = userEvent.setup();
    renderDialog();

    expect(
      await screen.findByText(/still rename this document in its current location/i),
    ).toBeInTheDocument();
    const fileName = screen.getByLabelText("File name");
    await user.clear(fileName);
    await user.type(fileName, "API contract final");
    await user.click(screen.getByRole("button", { name: "Move document" }));

    await waitFor(() =>
      expect(moveDocumentMock).toHaveBeenCalledWith(
        "engineering",
        "drafts/api-contract.md",
        { slug: "API contract final" },
      ),
    );
  });

  it("explains when a legacy backend does not provide the move endpoint", async () => {
    moveDocumentMock.mockRejectedValue(
      new ApiError("Not Found", 404, { detail: "Not Found" }),
    );
    const user = userEvent.setup();
    renderDialog();

    await user.click(await screen.findByLabelText("Target collection"));
    await user.click(screen.getByRole("menuitemradio", { name: "archive" }));
    await user.click(screen.getByRole("button", { name: "Move document" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Move or rename is not available on this server yet.",
    );
  });

  it("normalizes Unicode file names for the destination preview", () => {
    expect(previewDocumentSlug("  주간 보고서 V2!  ")).toBe("주간-보고서-v2");
    expect(previewDocumentSlug("***")).toBe("untitled");
  });
});
