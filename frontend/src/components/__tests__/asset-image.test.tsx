import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AssetImage } from "@/components/asset-image";
import { MarkdownRender } from "@/components/markdown-render";
import { assetIdFromUrl } from "@/lib/image-assets";

const apiMocks = vi.hoisted(() => ({
  getAssetBlob: vi.fn(),
  publicationAssetUrl: vi.fn(),
  refreshPublicationViewGrant: vi.fn(),
}));

vi.mock("@/lib/api", () => apiMocks);

const ASSET_ID = "6d04dc8a-0302-4a85-a314-e7485ff5a610";
const ASSET_URL = `/api/assets/${ASSET_ID}`;

describe("AssetImage", () => {
  const createObjectURL = vi.fn(() => "blob:private-image");
  const revokeObjectURL = vi.fn();

  beforeEach(() => {
    apiMocks.getAssetBlob.mockReset();
    apiMocks.publicationAssetUrl.mockReset();
    apiMocks.refreshPublicationViewGrant.mockReset();
    createObjectURL.mockClear();
    revokeObjectURL.mockClear();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("recognizes only canonical private asset URLs", () => {
    expect(assetIdFromUrl(ASSET_URL)).toBe(ASSET_ID);
    expect(assetIdFromUrl(ASSET_URL.toUpperCase())).toBeNull();
    expect(assetIdFromUrl(`/api/assets/not-a-uuid`)).toBeNull();
    expect(assetIdFromUrl(`https://example.test/api/assets/${ASSET_ID}`)).toBeNull();
  });

  it("preserves protocol-relative images without sending a referrer", () => {
    render(
      <MarkdownRender markdown="![Imported diagram](//cdn.example.test/diagram.png)" />,
    );

    const image = screen.getByRole("img", { name: "Imported diagram" });
    expect(image).toHaveAttribute("src", "//cdn.example.test/diagram.png");
    expect(image).toHaveAttribute("referrerpolicy", "no-referrer");
  });

  it("loads authenticated assets through a protected blob request", async () => {
    apiMocks.getAssetBlob.mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    const { unmount } = render(
      <AssetImage
        src={ASSET_URL}
        alt="Architecture diagram"
        assetContext={{ mode: "authenticated", vault: "team" }}
      />,
    );

    expect(screen.getByRole("status", { name: "Loading image: Architecture diagram" })).toBeVisible();
    const image = await screen.findByRole("img", { name: "Architecture diagram" });
    expect(image).toHaveAttribute("src", "blob:private-image");
    expect(apiMocks.getAssetBlob).toHaveBeenCalledWith(
      ASSET_ID,
      "team",
      expect.any(AbortSignal),
      undefined,
    );

    unmount();
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:private-image"));
  });

  it("passes the exact historical document context to the byte request", async () => {
    apiMocks.getAssetBlob.mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    render(
      <AssetImage
        src={ASSET_URL}
        alt="Old diagram"
        assetContext={{
          mode: "authenticated",
          vault: "team",
          document: "notes/weekly.md",
          commit: "abcdef123456", // pragma: allowlist secret — synthetic Git commit
        }}
      />,
    );

    await screen.findByRole("img", { name: "Old diagram" });
    expect(apiMocks.getAssetBlob).toHaveBeenCalledWith(
      ASSET_ID,
      "team",
      expect.any(AbortSignal),
      {
        document: "notes/weekly.md",
        commit: "abcdef123456", // pragma: allowlist secret — synthetic Git commit
      },
    );
  });

  it("never reuses a resolved blob while a different asset is loading", async () => {
    const secondId = "0d5028f7-cd47-42c5-8ed8-121742d085ec";
    let resolveSecond: ((blob: Blob) => void) | undefined;
    apiMocks.getAssetBlob
      .mockResolvedValueOnce(new Blob(["first"], { type: "image/png" }))
      .mockImplementationOnce(
        () => new Promise<Blob>((resolve) => { resolveSecond = resolve; }),
      );
    createObjectURL
      .mockReturnValueOnce("blob:first-image")
      .mockReturnValueOnce("blob:second-image");

    const { rerender } = render(
      <AssetImage
        src={ASSET_URL}
        alt="Diagram"
        assetContext={{ mode: "authenticated", vault: "team" }}
      />,
    );
    expect(await screen.findByRole("img", { name: "Diagram" })).toHaveAttribute(
      "src",
      "blob:first-image",
    );

    rerender(
      <AssetImage
        src={`/api/assets/${secondId}`}
        alt="Diagram"
        assetContext={{ mode: "authenticated", vault: "team" }}
      />,
    );
    expect(screen.queryByRole("img", { name: "Diagram" })).toBeNull();
    expect(screen.getByRole("status", { name: "Loading image: Diagram" })).toBeVisible();

    resolveSecond?.(new Blob(["second"], { type: "image/png" }));
    expect(await screen.findByRole("img", { name: "Diagram" })).toHaveAttribute(
      "src",
      "blob:second-image",
    );
  });

  it("renders publication assets with their grant-bearing native URL", () => {
    apiMocks.publicationAssetUrl.mockReturnValue("/api/v1/public/slug/assets/id?grant=grant");

    render(
      <AssetImage
        src={ASSET_URL}
        alt="Published diagram"
        assetContext={{ mode: "publication", slug: "slug" }}
      />,
    );

    expect(screen.getByRole("img", { name: "Published diagram" })).toHaveAttribute(
      "src",
      "/api/v1/public/slug/assets/id?grant=grant",
    );
    expect(apiMocks.publicationAssetUrl).toHaveBeenCalledWith("slug", ASSET_ID);
    expect(apiMocks.getAssetBlob).not.toHaveBeenCalled();
  });

  it("rotates an expired publication grant and retries the lazy image", async () => {
    let finishRefresh: ((grant: string) => void) | undefined;
    apiMocks.publicationAssetUrl
      .mockReturnValueOnce("/api/v1/public/slug/assets/id?grant=expired")
      .mockReturnValue("/api/v1/public/slug/assets/id?grant=renewed");
    apiMocks.refreshPublicationViewGrant.mockImplementation(
      () => new Promise<string>((resolve) => { finishRefresh = resolve; }),
    );

    render(
      <AssetImage
        src={ASSET_URL}
        alt="Late diagram"
        assetContext={{ mode: "publication", slug: "slug" }}
      />,
    );
    const image = screen.getByRole("img", { name: "Late diagram" });
    fireEvent.error(image);
    fireEvent.error(image);

    await waitFor(() => expect(apiMocks.refreshPublicationViewGrant).toHaveBeenCalledTimes(1));
    expect(apiMocks.refreshPublicationViewGrant).toHaveBeenCalledWith("slug");
    finishRefresh?.("renewed");
    await waitFor(() => expect(screen.getByRole("img", { name: "Late diagram" })).toHaveAttribute(
      "src",
      "/api/v1/public/slug/assets/id?grant=renewed",
    ));
  });

  it("does not refetch when a parent recreates an equivalent asset context", async () => {
    apiMocks.getAssetBlob.mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    const markdown = `![Architecture](${ASSET_URL})`;
    const { rerender } = render(
      <MarkdownRender
        markdown={markdown}
        assetContext={{ mode: "authenticated", vault: "team" }}
      />,
    );
    await screen.findByRole("img", { name: "Architecture" });

    rerender(
      <MarkdownRender
        markdown={markdown}
        assetContext={{ mode: "authenticated", vault: "team" }}
      />,
    );

    await waitFor(() => expect(apiMocks.getAssetBlob).toHaveBeenCalledTimes(1));
  });
});
