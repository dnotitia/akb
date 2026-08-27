import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DocumentCreateDialog } from "@/components/document-create-dialog";

const putDocument = vi.fn();
const discardAsset = vi.fn();
const ASSET_ID = "123e4567-e89b-42d3-a456-426614174000";

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {},
  putDocument: (...args: unknown[]) => putDocument(...args),
  discardAsset: (...args: unknown[]) => discardAsset(...args),
}));

vi.mock("@/hooks/use-vault-tree", () => ({
  useVaultTree: () => ({ tree: [] }),
}));

vi.mock("@/contexts/vault-refresh-context", () => ({
  useVaultRefresh: () => ({ refetchTree: vi.fn(), refetchVaults: vi.fn() }),
}));

vi.mock("@/components/markdown-editor", () => ({
  default: ({
    value,
    onChange,
    onUploadingChange,
  }: {
    value: string;
    onChange: (body: string, ids: string[]) => void;
    onUploadingChange?: (uploading: boolean) => void;
  }) => (
    <>
      <textarea
        aria-label="Document body"
        value={value}
        onChange={(event) => onChange(event.target.value, [])}
      />
      <input
        aria-label="Local image"
        type="file"
        accept="image/*"
        onChange={(event) => {
          if (!event.currentTarget.files?.length) return;
          onUploadingChange?.(true);
          onChange(`![Local image](/api/assets/${ASSET_ID})`, [ASSET_ID]);
          onUploadingChange?.(false);
        }}
      />
    </>
  ),
}));

function renderPage(initialCollection = "overview") {
  const onOpenChange = vi.fn();
  const onCreated = vi.fn();
  const result = render(
    <MemoryRouter>
      <DocumentCreateDialog
        open
        vault="my-v"
        initialCollection={initialCollection}
        onOpenChange={onOpenChange}
        onCreated={onCreated}
      />
    </MemoryRouter>,
  );
  return { ...result, onOpenChange, onCreated };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("DocumentCreateDialog reserved collection feedback", () => {
  it("rejects overview before submit and enables create after a valid replacement", async () => {
    const user = userEvent.setup();
    renderPage();

    const collection = screen.getByLabelText(/^collection/i);
    const create = screen.getByRole("button", { name: /create document/i });
    expect(await screen.findByText(/system collection reserved/i)).toBeInTheDocument();
    expect(collection).toHaveAttribute("aria-invalid", "true");
    expect(create).toBeDisabled();
    expect(putDocument).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText(/^title/i), "A note");
    await user.type(await screen.findByLabelText(/document body/i), "Body");
    await user.clear(collection);
    await user.type(collection, "notes");

    expect(screen.queryByText(/system collection reserved/i)).toBeNull();
    expect(screen.getByText(/new collection/i)).toBeInTheDocument();
    expect(collection).not.toHaveAttribute("aria-invalid");
    expect(create).toBeEnabled();
  });

  it("guards a dirty draft before closing", async () => {
    const user = userEvent.setup();
    const { onOpenChange } = renderPage("notes");

    await user.type(screen.getByLabelText(/^title/i), "Unfinished note");
    await user.click(screen.getByRole("button", { name: /close document composer/i }));

    expect(screen.getByRole("dialog", { name: /discard this draft/i })).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /discard draft/i }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("does not dismiss the authoring workbench from a background interaction", () => {
    const { onOpenChange } = renderPage("notes");

    fireEvent.pointerDown(document.body);

    expect(screen.getByRole("dialog", { name: /new document/i })).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalled();
  });

  it("stays open when focus returns from a native image picker", () => {
    const { onOpenChange } = renderPage("notes");
    const dialog = screen.getByRole("dialog", { name: /new document/i });
    const picker = screen.getByLabelText(/local image/i);

    fireEvent.focusOut(dialog, { relatedTarget: document.body });
    fireEvent.change(picker, {
      target: { files: [new File(["image"], "diagram.png", { type: "image/png" })] },
    });
    fireEvent.pointerDown(document.body);

    expect(screen.getByRole("dialog", { name: /new document/i })).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("still closes a clean workbench through Escape", async () => {
    const user = userEvent.setup();
    const { onOpenChange } = renderPage("notes");

    await user.keyboard("{Escape}");

    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps document properties available through the details inspector", async () => {
    const user = userEvent.setup();
    renderPage("notes");

    const toggle = screen.getByRole("button", { name: /show document details/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);

    expect(screen.getByRole("complementary", { name: /document details/i })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /hide document details/i })).toHaveLength(2);
  });

  it("creates through the existing API contract and returns the document path", async () => {
    const user = userEvent.setup();
    putDocument.mockResolvedValueOnce({ path: "notes/a-note.md" });
    const { onCreated } = renderPage("notes");

    await user.type(screen.getByLabelText(/^title/i), "A note");
    await user.type(screen.getByLabelText(/document body/i), "Knowledge worth keeping");
    await user.click(screen.getByRole("button", { name: /create document/i }));

    await waitFor(() => {
      expect(putDocument).toHaveBeenCalledWith(
        expect.objectContaining({
          vault: "my-v",
          collection: "notes",
          title: "A note",
          content: "Knowledge worth keeping",
          type: "note",
        }),
      );
      expect(onCreated).toHaveBeenCalledWith("notes/a-note.md");
    });
  });

  it("restores an autosaved local draft and clears it on explicit discard", async () => {
    const user = userEvent.setup();
    const first = renderPage("notes");
    await user.type(screen.getByLabelText(/^title/i), "Recovered note");
    await user.type(screen.getByLabelText(/document body/i), "Recovered body");
    await screen.findByText(/draft saved locally/i);
    first.unmount();

    const second = renderPage("notes");
    expect(screen.getByLabelText(/^title/i)).toHaveValue("Recovered note");
    expect(screen.getByText(/local draft restored/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /close document composer/i }));
    await user.click(screen.getByRole("button", { name: /discard draft/i }));
    expect(second.onOpenChange).toHaveBeenCalledWith(false);
    expect(window.localStorage.length).toBe(0);
  });

  it("restores temporary image ids and discards them with the saved draft", async () => {
    const user = userEvent.setup();
    const first = renderPage("notes");
    await user.type(screen.getByLabelText(/^title/i), "Draft with image");
    fireEvent.change(screen.getByLabelText(/local image/i), {
      target: { files: [new File(["image"], "diagram.png", { type: "image/png" })] },
    });
    await screen.findByText(/draft saved locally/i);
    first.unmount();

    const second = renderPage("notes");
    expect(screen.getByLabelText(/document body/i)).toHaveValue(
      `![Local image](/api/assets/${ASSET_ID})`,
    );

    await user.click(screen.getByRole("button", { name: /close document composer/i }));
    await user.click(screen.getByRole("button", { name: /discard draft/i }));
    await waitFor(() => {
      expect(discardAsset).toHaveBeenCalledWith("my-v", ASSET_ID);
      expect(second.onOpenChange).toHaveBeenCalledWith(false);
    });
    expect(window.localStorage.length).toBe(0);
  });
});
