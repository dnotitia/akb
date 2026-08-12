import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MarkdownEditor } from "@/components/markdown-editor";
import { EDITOR_IMAGE_MAX_BYTES, validateEditorImage } from "@/lib/image-assets";

const apiMocks = vi.hoisted(() => ({
  uploadAsset: vi.fn(),
  discardAsset: vi.fn(),
  getAssetBlob: vi.fn(),
  publicationAssetUrl: vi.fn(),
  refreshPublicationViewGrant: vi.fn(),
  ApiError: class ApiError extends Error {
    status = 400;
  },
}));

vi.mock("@/lib/api", () => apiMocks);

const ASSET_ID = "6d04dc8a-0302-4a85-a314-e7485ff5a610";

describe("MarkdownEditor image insertion", () => {
  beforeEach(() => {
    apiMocks.uploadAsset.mockReset();
    apiMocks.discardAsset.mockReset();
    apiMocks.discardAsset.mockResolvedValue(undefined);
    apiMocks.getAssetBlob.mockReset();
    apiMocks.getAssetBlob.mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:editor-image"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
  });

  it("rejects unsupported and oversized files before upload", () => {
    expect(validateEditorImage(new File(["svg"], "vector.svg", { type: "image/svg+xml" }))).toMatch(/PNG/);
    expect(
      validateEditorImage(
        new File([new Uint8Array(EDITOR_IMAGE_MAX_BYTES + 1)], "large.png", {
          type: "image/png",
        }),
      ),
    ).toMatch(/10 MB/);
    expect(validateEditorImage(new File(["png"], "ok.png", { type: "image/png" }))).toBeNull();
  });

  it("rejects the whole invalid batch without offering an impossible partial retry", async () => {
    const { container } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );
    const valid = new File(["png"], "first.png", { type: "image/png" });
    const invalid = new File(["svg"], "vector.svg", { type: "image/svg+xml" });

    fireEvent.change(container.querySelector('input[type="file"]')!, {
      target: { files: [valid, invalid] },
    });

    expect(await screen.findByText(/Choose a PNG/)).toBeVisible();
    expect(screen.getByText(/No images were uploaded/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/Choose a PNG/)).toBeNull();
    expect(apiMocks.uploadAsset).not.toHaveBeenCalled();
  });

  it("uploads a picked image and serializes its private asset URL", async () => {
    apiMocks.uploadAsset.mockResolvedValue({
      id: ASSET_ID,
      url: `/api/assets/${ASSET_ID}`,
      name: "diagram.png",
      mime_type: "image/png",
      size_bytes: 5,
    });
    const onChange = vi.fn();
    const { container } = render(
      <MarkdownEditor value="Start here" vault="team" onChange={onChange} />,
    );
    const file = new File(["image"], "diagram.png", { type: "image/png" });
    const picker = container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(picker).not.toBeNull();

    fireEvent.change(picker!, { target: { files: [file] } });

    await waitFor(() => expect(apiMocks.uploadAsset).toHaveBeenCalledWith("team", file, expect.any(AbortSignal)));
    await waitFor(() =>
      expect(onChange.mock.calls.some(
        ([markdown, assetIds]) =>
          markdown.includes(`![diagram](/api/assets/${ASSET_ID})`) &&
          assetIds.length === 1 &&
          assetIds[0] === ASSET_ID,
      )).toBe(true),
    );
    expect(await screen.findByRole("img", { name: "diagram" })).toHaveAttribute(
      "src",
      "blob:editor-image",
    );
  });

  it("deletes an existing image node and removes its markdown link", async () => {
    const onChange = vi.fn();
    render(
      <MarkdownEditor
        value={`Before deletion\n\n![diagram](/api/assets/${ASSET_ID})`}
        vault="team"
        onChange={onChange}
      />,
    );

    await screen.findByRole("img", { name: "diagram" });
    fireEvent.click(screen.getByRole("button", { name: "Remove image: diagram" }));

    await waitFor(() => expect(screen.queryByRole("img", { name: "diagram" })).toBeNull());
    await waitFor(() =>
      expect(
        onChange.mock.calls.some(
          ([markdown, assetIds]) =>
            markdown.includes("Before deletion") &&
            !markdown.includes(`/api/assets/${ASSET_ID}`) &&
            assetIds.length === 0,
        ),
      ).toBe(true),
    );
    // An existing image may already be referenced by Git history, so removing
    // its current link must not physically delete the retained bytes.
    expect(apiMocks.discardAsset).not.toHaveBeenCalled();
  });

  it("uses the exact document revision when resolving an existing image", async () => {
    render(
      <MarkdownEditor
        value={`![diagram](/api/assets/${ASSET_ID})`}
        vault="team"
        document="notes/weekly.md"
        commit="aaaaaaaaaaaa"
        onChange={vi.fn()}
      />,
    );

    await screen.findByRole("img", { name: "diagram" });
    expect(apiMocks.getAssetBlob).toHaveBeenCalledWith(
      ASSET_ID,
      "team",
      expect.any(AbortSignal),
      { document: "notes/weekly.md", commit: "aaaaaaaaaaaa" },
    );
  });

  it("discards a newly uploaded image when its node is removed before save", async () => {
    apiMocks.uploadAsset.mockResolvedValue({
      id: ASSET_ID,
      url: `/api/assets/${ASSET_ID}`,
      name: "diagram.png",
      mime_type: "image/png",
      size_bytes: 5,
    });
    const { container, unmount } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );
    const file = new File(["image"], "diagram.png", { type: "image/png" });
    const picker = container.querySelector<HTMLInputElement>('input[type="file"]');

    fireEvent.change(picker!, { target: { files: [file] } });
    await screen.findByRole("img", { name: "diagram" });
    fireEvent.click(screen.getByRole("button", { name: "Remove image: diagram" }));

    // Physical cleanup is deferred until the edit session ends so Save and
    // Plate's Undo continue to reference the same asset.
    expect(apiMocks.discardAsset).not.toHaveBeenCalled();
    unmount();
    await waitFor(() =>
      expect(apiMocks.discardAsset).toHaveBeenCalledWith("team", ASSET_ID),
    );
  });

  it("does not discard uploads when a document save owns the unmount", async () => {
    apiMocks.uploadAsset.mockResolvedValue({
      id: ASSET_ID,
      url: `/api/assets/${ASSET_ID}`,
      name: "diagram.png",
      mime_type: "image/png",
      size_bytes: 5,
    });
    const { container, rerender, unmount } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );
    const file = new File(["image"], "diagram.png", { type: "image/png" });
    fireEvent.change(container.querySelector('input[type="file"]')!, {
      target: { files: [file] },
    });
    await screen.findByRole("img", { name: "diagram" });

    rerender(
      <MarkdownEditor
        value="Draft"
        vault="team"
        onChange={vi.fn()}
        preserveUploadsOnUnmount
      />,
    );
    unmount();

    await Promise.resolve();
    expect(apiMocks.discardAsset).not.toHaveBeenCalled();
  });

  it("forgets uploaded cleanup candidates after a successful save", async () => {
    apiMocks.uploadAsset.mockResolvedValue({
      id: ASSET_ID,
      url: `/api/assets/${ASSET_ID}`,
      name: "diagram.png",
      mime_type: "image/png",
      size_bytes: 5,
    });
    const { container, rerender, unmount } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );
    fireEvent.change(container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["image"], "diagram.png", { type: "image/png" })] },
    });
    await screen.findByRole("img", { name: "diagram" });

    rerender(
      <MarkdownEditor
        value="Draft"
        vault="team"
        onChange={vi.fn()}
        claimedAssetIds={[ASSET_ID]}
      />,
    );
    unmount();

    await Promise.resolve();
    expect(apiMocks.discardAsset).not.toHaveBeenCalled();
  });

  it("retries the failed image and every remaining file in a batch", async () => {
    const ids = [
      "4f18f05e-d1cf-44ce-a39e-74737fb88e7c",
      "e06049c1-6bb7-4c08-b649-3d8faf79c69f",
      "b17fc0f8-e100-4f5c-b688-996324896bc3",
    ];
    const result = (id: string, name: string) => ({
      id,
      url: `/api/assets/${id}`,
      name,
      mime_type: "image/png",
      size_bytes: 5,
    });
    apiMocks.uploadAsset
      .mockResolvedValueOnce(result(ids[0], "one.png"))
      .mockRejectedValueOnce(new Error("temporary upload failure"))
      .mockResolvedValueOnce(result(ids[1], "two.png"))
      .mockResolvedValueOnce(result(ids[2], "three.png"));
    const files = ["one.png", "two.png", "three.png"].map(
      (name) => new File([name], name, { type: "image/png" }),
    );
    const { container } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );

    fireEvent.change(container.querySelector('input[type="file"]')!, {
      target: { files },
    });
    expect(await screen.findByText(/2 images remain/)).toBeVisible();
    expect(apiMocks.uploadAsset).toHaveBeenCalledTimes(2);

    expect(screen.getByText("Image upload failed")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(apiMocks.uploadAsset).toHaveBeenCalledTimes(4));
    expect(apiMocks.uploadAsset.mock.calls.map((call) => call[1])).toEqual([
      files[0],
      files[1],
      files[1],
      files[2],
    ]);
  });

  it("prevents default file-drop navigation and retains a batch dropped during upload", async () => {
    const firstId = "4f18f05e-d1cf-44ce-a39e-74737fb88e7c";
    const secondId = "e06049c1-6bb7-4c08-b649-3d8faf79c69f";
    let finishFirst!: (value: {
      id: string;
      url: string;
      name: string;
      mime_type: string;
      size_bytes: number;
    }) => void;
    apiMocks.uploadAsset
      .mockImplementationOnce(
        () => new Promise((resolve) => {
          finishFirst = resolve;
        }),
      )
      .mockResolvedValueOnce({
        id: secondId,
        url: `/api/assets/${secondId}`,
        name: "second.png",
        mime_type: "image/png",
        size_bytes: 5,
      });
    const first = new File(["first"], "first.png", { type: "image/png" });
    const second = new File(["second"], "second.png", { type: "image/png" });
    const { container } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );

    fireEvent.change(container.querySelector('input[type="file"]')!, {
      target: { files: [first] },
    });
    await waitFor(() => expect(apiMocks.uploadAsset).toHaveBeenCalledTimes(1));
    const dropResult = fireEvent.drop(container.querySelector('[contenteditable="true"]')!, {
      dataTransfer: { files: [second] },
    });
    expect(dropResult).toBe(false);

    finishFirst({
      id: firstId,
      url: `/api/assets/${firstId}`,
      name: "first.png",
      mime_type: "image/png",
      size_bytes: 5,
    });
    expect(await screen.findByText(/previous image batch finished/i)).toBeVisible();
    expect(screen.getByText("Images waiting to upload")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(apiMocks.uploadAsset).toHaveBeenCalledTimes(2));
    expect(apiMocks.uploadAsset.mock.calls[1][1]).toBe(second);
  });

  it("uploads a copied image even when the clipboard also carries HTML", async () => {
    apiMocks.uploadAsset.mockResolvedValue({
      id: ASSET_ID,
      url: `/api/assets/${ASSET_ID}`,
      name: "sheet.png",
      mime_type: "image/png",
      size_bytes: 5,
    });
    const { container } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );
    const file = new File(["image"], "sheet.png", { type: "image/png" });
    const editor = container.querySelector('[contenteditable="true"]');

    const paste = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(paste, "clipboardData", {
      value: {
        files: [file],
        getData: (type: string) =>
          type === "text/html" ? "<img src='copied.png'>" : "Copied image",
      },
    });
    fireEvent(editor!, paste);

    await waitFor(() => expect(apiMocks.uploadAsset).toHaveBeenCalledWith(
      "team",
      file,
      expect.any(AbortSignal),
    ));
  });

  it("uploads an image copied from a file manager with a plain filename flavor", async () => {
    apiMocks.uploadAsset.mockResolvedValue({
      id: ASSET_ID,
      url: `/api/assets/${ASSET_ID}`,
      name: "finder.png",
      mime_type: "image/png",
      size_bytes: 5,
    });
    const { container } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );
    const file = new File(["image"], "finder.png", { type: "image/png" });
    const editor = container.querySelector('[contenteditable="true"]');

    const allowed = fireEvent.paste(editor!, {
      clipboardData: {
        files: [file],
        getData: (type: string) => type === "text/plain" ? "finder.png" : "",
      },
    });

    expect(allowed).toBe(false);
    await waitFor(() => expect(apiMocks.uploadAsset).toHaveBeenCalledWith(
      "team",
      file,
      expect.any(AbortSignal),
    ));
  });

  it("preserves unrelated plain text when a clipboard also exposes an image file", () => {
    const { container } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );
    const file = new File(["image"], "finder.png", { type: "image/png" });
    const editor = container.querySelector('[contenteditable="true"]');

    const allowed = fireEvent.paste(editor!, {
      clipboardData: {
        files: [file],
        getData: (type: string) => type === "text/plain" ? "Keep this caption" : "",
      },
    });

    expect(allowed).toBe(true);
    expect(apiMocks.uploadAsset).not.toHaveBeenCalled();
  });

  it("leaves mixed rich-text clipboard content to the normal paste path", () => {
    const { container } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );
    const file = new File(["image"], "sheet.png", { type: "image/png" });
    const editor = container.querySelector('[contenteditable="true"]');

    const allowed = fireEvent.paste(editor!, {
      clipboardData: {
        files: [file],
        getData: (type: string) =>
          type === "text/html"
            ? "<table><tr><td>Copied cell</td></tr></table>"
            : "Copied cell",
      },
    });

    expect(allowed).toBe(true);
    expect(apiMocks.uploadAsset).not.toHaveBeenCalled();
  });

  it("discards an uploaded image omitted from the accepted markdown", async () => {
    apiMocks.uploadAsset.mockResolvedValue({
      id: ASSET_ID,
      url: `/api/assets/${ASSET_ID}`,
      name: "diagram.png",
      mime_type: "image/png",
      size_bytes: 5,
    });
    const { container, rerender } = render(
      <MarkdownEditor value="Draft" vault="team" onChange={vi.fn()} />,
    );
    fireEvent.change(container.querySelector('input[type="file"]')!, {
      target: { files: [new File(["image"], "diagram.png", { type: "image/png" })] },
    });
    await screen.findByRole("img", { name: "diagram" });

    rerender(
      <MarkdownEditor
        value="Draft"
        vault="team"
        onChange={vi.fn()}
        claimedAssetIds={[]}
      />,
    );

    await waitFor(() => expect(apiMocks.discardAsset).toHaveBeenCalledWith("team", ASSET_ID));
  });
});
