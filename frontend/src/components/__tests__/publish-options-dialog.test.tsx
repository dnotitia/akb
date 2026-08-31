import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PublishOptionsDialog } from "@/components/publish-options-dialog";

vi.mock("@/lib/api", () => ({
  createPublication: vi.fn(),
  getDocument: vi.fn(),
  listPublications: vi.fn(),
}));

import { createPublication, getDocument, listPublications } from "@/lib/api";

const createPublicationMock = createPublication as unknown as ReturnType<typeof vi.fn>;
const getDocumentMock = getDocument as unknown as ReturnType<typeof vi.fn>;
const listPublicationsMock = listPublications as unknown as ReturnType<typeof vi.fn>;

const FILE_PUBLICATION = {
  slug: "diagram-public",
  share_url: "https://example.test/p/diagram-public",
  resource_type: "file",
  resource_uri: "akb://demo/coll/design/file/11111111-1111-1111-1111-111111111111",
  vault: "demo",
  title: "diagram.png",
  mode: "live",
  expires_at: null,
  max_views: null,
  view_count: 0,
  allow_embed: true,
  section_filter: null,
  password_protected: false,
  created_at: "2026-08-31T00:00:00Z",
  snapshot_at: null,
} as const;

const DOCUMENT_PUBLICATION = {
  ...FILE_PUBLICATION,
  slug: "guide-public",
  share_url: "https://example.test/p/guide-public",
  resource_type: "document",
  resource_uri: "akb://demo/coll/docs/doc/guide.md",
  title: "Guide",
} as const;

beforeEach(() => {
  vi.clearAllMocks();
  getDocumentMock.mockResolvedValue({
    uri: DOCUMENT_PUBLICATION.resource_uri,
    title: "Guide",
    path: "docs/guide.md",
  });
  listPublicationsMock.mockResolvedValue({ publications: [] });
  createPublicationMock.mockResolvedValue(FILE_PUBLICATION);
});

afterEach(cleanup);

describe("PublishOptionsDialog file flow", () => {
  it("publishes a canonical file URI with the shared access controls", async () => {
    const user = userEvent.setup();
    const onPublished = vi.fn();
    const onOpenChange = vi.fn();
    render(
      <PublishOptionsDialog
        open
        onOpenChange={onOpenChange}
        vault="demo"
        resourceType="file"
        resourceUri={FILE_PUBLICATION.resource_uri}
        resourceName="diagram.png"
        onPublished={onPublished}
      />,
    );

    await user.click(screen.getByRole("checkbox", { name: /Require password/i }));
    await user.type(screen.getByLabelText("Publication password"), "strong-passphrase");
    await user.click(screen.getByRole("button", { name: "7 days" }));
    await user.type(screen.getByLabelText("Max views"), "25");
    await user.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => {
      expect(createPublicationMock).toHaveBeenCalledWith("demo", {
        resource_type: "file",
        uri: FILE_PUBLICATION.resource_uri,
        title: "diagram.png",
        password: "strong-passphrase", // pragma: allowlist secret — synthetic test value
        expires_in: "7d",
        max_views: 25,
      });
    });
    expect(onPublished).toHaveBeenCalledWith("diagram-public", FILE_PUBLICATION);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("creates a distinct link when the same file already has a publication", async () => {
    listPublicationsMock.mockResolvedValue({ publications: [FILE_PUBLICATION] });
    const user = userEvent.setup();
    const onPublished = vi.fn();
    render(
      <PublishOptionsDialog
        open
        onOpenChange={vi.fn()}
        vault="demo"
        resourceType="file"
        resourceUri={FILE_PUBLICATION.resource_uri}
        resourceName="diagram.png"
        onPublished={onPublished}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(onPublished).toHaveBeenCalledWith("diagram-public", FILE_PUBLICATION));
    expect(createPublicationMock).toHaveBeenCalledTimes(1);
    expect(listPublicationsMock).not.toHaveBeenCalled();
  });

  it("keeps an invalid access limit inline and blocks submission", async () => {
    const user = userEvent.setup();
    render(
      <PublishOptionsDialog
        open
        onOpenChange={vi.fn()}
        vault="demo"
        resourceType="file"
        resourceUri={FILE_PUBLICATION.resource_uri}
        resourceName="diagram.png"
        onPublished={vi.fn()}
      />,
    );

    await user.type(screen.getByLabelText("Max views"), "0");
    expect(screen.getByText("Enter a positive whole number.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
    expect(createPublicationMock).not.toHaveBeenCalled();
  });
});

describe("PublishOptionsDialog document compatibility", () => {
  it("keeps the document viewer's singular-link reuse contract", async () => {
    listPublicationsMock.mockResolvedValue({ publications: [DOCUMENT_PUBLICATION] });
    const user = userEvent.setup();
    const onPublished = vi.fn();
    render(
      <PublishOptionsDialog
        open
        onOpenChange={vi.fn()}
        vault="demo"
        docId="docs/guide.md"
        onPublished={onPublished}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => {
      expect(onPublished).toHaveBeenCalledWith("guide-public", DOCUMENT_PUBLICATION);
    });
    expect(getDocumentMock).toHaveBeenCalledWith("demo", "docs/guide.md");
    expect(listPublicationsMock).toHaveBeenCalledWith("demo", "document");
    expect(createPublicationMock).not.toHaveBeenCalled();
  });
});
