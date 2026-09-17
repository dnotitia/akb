import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getAssetBlob: vi.fn(),
  getAttachmentMetadata: vi.fn(),
  getDocument: vi.fn(),
  getVaultFileDownloadUrl: vi.fn(),
  refreshPublicationViewGrant: vi.fn(),
  publicationAssetUrl: vi.fn(),
}));

vi.mock("@/lib/api", () => apiMocks);

import { MarkdownRender } from "@/components/markdown-render";

const DOCUMENT = "akb://team/coll/notes/doc/guide.md";
const FILE = "akb://team/file/123e4567-e89b-42d3-a456-426614174001";
const ATTACHMENT = "/api/assets/123e4567-e89b-42d3-a456-426614174000";

beforeEach(() => {
  Object.values(apiMocks).forEach((mock) => mock.mockReset());
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:claimed-attachment"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
});

describe("MarkdownRender resource targets", () => {
  it("uses a runtime route for an available document while retaining the canonical target", async () => {
    apiMocks.getDocument.mockResolvedValue({ path: "notes/guide.md" });

    render(
      <MarkdownRender
        markdown={`[Guide](${DOCUMENT})`}
        assetContext={{ mode: "authenticated", vault: "team" }}
      />,
    );

    await waitFor(() => {
      const link = screen.getByRole("link", { name: "Guide" });
      expect(link).toHaveAttribute("href", "/vault/team/doc/notes%2Fguide.md");
      expect(link).toHaveAttribute("data-markdown-target", DOCUMENT);
      expect(link).toHaveAttribute("data-markdown-resolution", "available");
    });
  });

  it("renders an unavailable file reference without revealing why it failed", async () => {
    apiMocks.getVaultFileDownloadUrl.mockRejectedValue(new Error("not found"));

    render(
      <MarkdownRender
        markdown={`[Download](${FILE})`}
        assetContext={{ mode: "authenticated", vault: "team" }}
      />,
    );

    await waitFor(() => {
      const link = screen.getByRole("link", { name: "Download" });
      expect(link).toHaveAttribute("href", "#");
      expect(link).toHaveAttribute("aria-disabled", "true");
      expect(link).toHaveAttribute("title", "Reference unavailable");
      expect(link).toHaveAttribute("data-markdown-target", FILE);
    });
  });

  it("renders a claimed attachment from the authenticated byte path", async () => {
    apiMocks.getAssetBlob.mockResolvedValue(new Blob(["valid fixture bytes"], { type: "image/png" }));

    render(
      <MarkdownRender
        markdown={`![Fixture attachment](${ATTACHMENT})`}
        assetContext={{ mode: "authenticated", vault: "team" }}
      />,
    );

    const image = await screen.findByRole("img", { name: "Fixture attachment" });
    expect(image).toHaveAttribute("src", "blob:claimed-attachment");
    expect(image).toHaveAttribute("data-markdown-target", ATTACHMENT);
  });
});
