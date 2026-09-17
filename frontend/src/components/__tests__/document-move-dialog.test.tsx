import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DocumentMoveDialog } from "@/components/document-move-dialog";

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

function renderDialog(
  onMoved = vi.fn(),
  onOpenChange = vi.fn(),
  onOpenDocument = vi.fn(),
) {
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
          onOpenDocument={onOpenDocument}
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
    uri: "akb://engineering/coll/archive/doc/api-contract.md",
    vault: "engineering",
    path: "archive/api-contract.md",
    commit_hash: "abc1234",
    current_commit: "abc1234",
    action: "moved",
  });
});

afterEach(() => cleanup());

describe("DocumentMoveDialog", () => {
  it("moves between Collections without exposing a file-name editor", async () => {
    const user = userEvent.setup();
    const { onMoved, onOpenChange } = renderDialog();

    expect(screen.getByText("API contract")).toBeInTheDocument();
    expect(screen.queryByLabelText("File name")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /change file name/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move document" })).toBeDisabled();

    const collection = await screen.findByLabelText("Target collection");
    await user.click(collection);
    expect(screen.queryByRole("menuitemradio", { name: /overview/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitemradio", { name: "archive" }));

    await user.type(
      screen.getByLabelText(/commit message/i),
      "Move the approved contract",
    );

    expect(screen.getByText("archive/api-contract.md")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Move document" }));

    await waitFor(() =>
      expect(moveDocumentMock).toHaveBeenCalledWith(
        "engineering",
        "drafts/api-contract.md",
        {
          collection: "archive",
          message: "Move the approved contract",
          title_conflict_policy: "reject",
        },
      ),
    );
    expect(onMoved).toHaveBeenCalledWith(
      expect.objectContaining({ path: "archive/api-contract.md" }),
    );
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("preflights collection collisions and keeps both without exposing the suffix as the title", async () => {
    browseVaultMock.mockResolvedValue({
      items: [
        { type: "collection", path: "archive" },
        { type: "collection", path: "drafts" },
        { type: "document", name: "API contract", path: "archive/api-contract.md" },
      ],
    });
    moveDocumentMock.mockResolvedValue({
      kind: "document_write",
      uri: "akb://engineering/coll/archive/doc/api-contract-2f3df21c.md",
      vault: "engineering",
      path: "archive/api-contract-2f3df21c.md",
      commit_hash: "abc1234",
      current_commit: "abc1234",
      action: "moved",
    });
    const user = userEvent.setup();
    const { onMoved, onOpenChange } = renderDialog();

    await user.click(await screen.findByLabelText("Target collection"));
    await user.click(screen.getByRole("menuitemradio", { name: "archive" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "already exists here",
    );
    expect(screen.getByLabelText("Target collection")).toHaveTextContent("archive");
    expect(screen.getByRole("button", { name: "Move document" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Keep both and move" }));

    await waitFor(() =>
      expect(moveDocumentMock).toHaveBeenCalledWith(
        "engineering",
        "drafts/api-contract.md",
        { collection: "archive", title_conflict_policy: "allow" },
      ),
    );
    expect(onMoved).toHaveBeenCalledWith(
      expect.objectContaining({ path: "archive/api-contract-2f3df21c.md" }),
    );
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps move unavailable until destination Collections can be loaded", async () => {
    browseVaultMock.mockRejectedValue(new Error("browse unavailable"));
    renderDialog();

    expect(
      await screen.findByText(/retry before choosing a destination/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move document" })).toBeDisabled();
    expect(moveDocumentMock).not.toHaveBeenCalled();
  });

  it("allows visibly different titles even when their technical slugs can collide", async () => {
    browseVaultMock.mockResolvedValue({
      items: [
        { type: "collection", path: "archive" },
        { type: "collection", path: "drafts" },
        { type: "document", name: "API Contract", path: "archive/legacy-contract.md" },
      ],
    });
    const user = userEvent.setup();
    renderDialog();

    await user.click(await screen.findByLabelText("Target collection"));
    await user.click(screen.getByRole("menuitemradio", { name: "archive" }));

    expect(screen.queryByText(/already exists here/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move document" })).toBeEnabled();
  });

  it("opens the conflicting document from the preflight notice", async () => {
    browseVaultMock.mockResolvedValue({
      items: [
        { type: "collection", path: "archive" },
        { type: "collection", path: "drafts" },
        { type: "document", name: "API contract", path: "archive/api-contract.md" },
      ],
    });
    const user = userEvent.setup();
    const onOpenDocument = vi.fn();
    const onOpenChange = vi.fn();
    renderDialog(vi.fn(), onOpenChange, onOpenDocument);

    await user.click(await screen.findByLabelText("Target collection"));
    await user.click(screen.getByRole("menuitemradio", { name: "archive" }));
    await user.click(screen.getByRole("button", { name: "Open existing" }));

    expect(onOpenDocument).toHaveBeenCalledWith("archive/api-contract.md");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("turns a server-side title race into the same explicit keep-both flow", async () => {
    moveDocumentMock.mockRejectedValueOnce(
      new ApiError("duplicate title", 409, {
        code: "document_title_conflict",
        details: {
          title: "API contract",
          collection: "archive",
          existing_path: "archive/api-contract.md",
          existing_title: "API contract",
        },
      }),
    );
    const user = userEvent.setup();
    renderDialog();

    await user.click(await screen.findByLabelText("Target collection"));
    await user.click(screen.getByRole("menuitemradio", { name: "archive" }));
    await user.click(screen.getByRole("button", { name: "Move document" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("already exists here");
    await user.click(screen.getByRole("button", { name: "Keep both and move" }));

    await waitFor(() =>
      expect(moveDocumentMock).toHaveBeenLastCalledWith(
        "engineering",
        "drafts/api-contract.md",
        { collection: "archive", title_conflict_policy: "allow" },
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
      "Document move is not available on this server yet.",
    );
  });
});
