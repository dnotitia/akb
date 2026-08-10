import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MarkdownEditor } from "@/components/markdown-editor";
import { EDITOR_IMAGE_MAX_BYTES, validateEditorImage } from "@/lib/image-assets";

const apiMocks = vi.hoisted(() => ({
  uploadAsset: vi.fn(),
  discardAsset: vi.fn(),
  getAssetBlob: vi.fn(),
  publicationAssetUrl: vi.fn(),
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
      expect(onChange.mock.calls.some(([markdown]) => markdown.includes(`![diagram](/api/assets/${ASSET_ID})`))).toBe(true),
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
          ([markdown]) =>
            markdown.includes("Before deletion") && !markdown.includes(`/api/assets/${ASSET_ID}`),
        ),
      ).toBe(true),
    );
    // An existing image may already be referenced by Git history, so removing
    // its current link must not physically delete the retained bytes.
    expect(apiMocks.discardAsset).not.toHaveBeenCalled();
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

    // Physical cleanup is deferred until the edit session ends. Deleting here
    // would race Save and make Plate's Undo restore a broken URL.
    expect(apiMocks.discardAsset).not.toHaveBeenCalled();
    unmount();
    await waitFor(() =>
      expect(apiMocks.discardAsset).toHaveBeenCalledWith("team", ASSET_ID),
    );
  });
});
