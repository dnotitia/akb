import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getAssetBlob: vi.fn(),
  getAttachmentMetadata: vi.fn(),
  getDocument: vi.fn(),
  getVaultFileDownloadUrl: vi.fn(),
  refreshPublicationViewGrant: vi.fn(),
  publicationAssetUrl: vi.fn(),
  uploadAsset: vi.fn(),
  discardAsset: vi.fn(),
}));

vi.mock("@/lib/api", () => apiMocks);

import { MarkdownEditor } from "@/components/markdown-editor";

const TARGET = "akb://team/coll/notes/doc/guide.md";

describe("MarkdownEditor resource targets", () => {
  it("keeps the canonical link in the editor model while applying a runtime route", async () => {
    apiMocks.getDocument.mockResolvedValue({ path: "notes/guide.md" });
    render(
      <MarkdownEditor
        value={`[Guide](${TARGET})`}
        vault="team"
        onChange={vi.fn()}
      />,
    );

    await waitFor(() => {
      const link = screen.getByRole("link", { name: "Guide" });
      expect(link).toHaveAttribute("href", "/vault/team/doc/notes%2Fguide.md");
      expect(link).toHaveAttribute("data-markdown-target", TARGET);
      expect(link).toHaveAttribute("data-markdown-resolution", "available");
    });
  });

  it("turns inaccessible canonical links into non-navigating placeholders", async () => {
    apiMocks.getDocument.mockRejectedValue(new Error("forbidden"));
    render(
      <MarkdownEditor
        value={`[Private](${TARGET})`}
        vault="team"
        onChange={vi.fn()}
      />,
    );

    await waitFor(() => {
      const link = screen.getByRole("link", { name: "Private" });
      expect(link).toHaveAttribute("href", "#");
      expect(link).toHaveAttribute("aria-disabled", "true");
      expect(link).toHaveAttribute("data-markdown-target", TARGET);
    });
  });
});
