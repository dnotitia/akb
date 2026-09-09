import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  copyFileToAttachment: vi.fn(),
  getAssetBlob: vi.fn(),
  getAttachmentMetadata: vi.fn(),
  getDocument: vi.fn(),
  getVaultFileDownloadUrl: vi.fn(),
  searchDocs: vi.fn(),
  uploadAsset: vi.fn(),
}));

vi.mock("@/lib/api", () => apiMocks);

import {
  canonicalAkbMarkdownTarget,
  classifyAkbMarkdownTarget,
  createAkbMarkdownAdapters,
  createAkbMarkdownTargetResolver,
  extractAkbMarkdownLinkTargets,
} from "@/lib/markdown-adapters";

const ATTACHMENT = "/api/assets/123e4567-e89b-42d3-a456-426614174000";
const DOCUMENT = "akb://team/coll/notes/doc/guide.md";
const FILE = "akb://team/coll/notes/file/123e4567-e89b-42d3-a456-426614174001";

describe("AKB Markdown target adapter", () => {
  beforeEach(() => {
    Object.values(apiMocks).forEach((mock) => mock.mockReset());
  });

  it("classifies and canonicalizes the three durable target kinds", () => {
    expect(classifyAkbMarkdownTarget(ATTACHMENT)).toBe("attachment");
    expect(classifyAkbMarkdownTarget(DOCUMENT)).toBe("document");
    expect(classifyAkbMarkdownTarget(FILE)).toBe("file");
    expect(canonicalAkbMarkdownTarget(`${ATTACHMENT}/`)).toBe(ATTACHMENT);
    expect(canonicalAkbMarkdownTarget(DOCUMENT)).toBe(DOCUMENT);
    expect(canonicalAkbMarkdownTarget(FILE)).toBe(FILE);
    expect(classifyAkbMarkdownTarget("javascript:alert(1)")).toBeNull();
  });

  it("finds reference-style links without resolving examples inside code", () => {
    expect(extractAkbMarkdownLinkTargets([
      "[Guide][guide]",
      "",
      `[guide]: ${DOCUMENT} "title"`,
      "",
      "[Shortcut]",
      `[Shortcut]: ${FILE}`,
      "",
      "`[ignored](akb://other/doc/nope.md)`",
    ].join("\n"))).toEqual([DOCUMENT, FILE]);
  });

  it("resolves only accessible same-vault targets and keeps runtime URLs ephemeral", async () => {
    apiMocks.getDocument.mockResolvedValue({ path: "notes/guide.md" });
    apiMocks.getVaultFileDownloadUrl.mockResolvedValue({
      kind: "file",
      download_url: "https://signed.example/file?expires=60",
    });
    apiMocks.getAttachmentMetadata.mockResolvedValue({
      kind: "attachment",
      target: ATTACHMENT,
      status: "claimed",
    });
    const resolver = createAkbMarkdownTargetResolver({ vault: "team" });

    await expect(resolver.resolve(DOCUMENT)).resolves.toMatchObject({
      target: DOCUMENT,
      kind: "document",
      status: "available",
      runtimeUrl: "/vault/team/doc/notes%2Fguide.md",
    });
    await expect(resolver.resolve(FILE)).resolves.toMatchObject({
      target: FILE,
      kind: "file",
      status: "available",
      runtimeUrl: "https://signed.example/file?expires=60",
    });
    await expect(resolver.resolve(ATTACHMENT)).resolves.toMatchObject({
      target: ATTACHMENT,
      kind: "attachment",
      status: "available",
    });
    await expect(resolver.resolve("akb://other/doc/private.md")).resolves.toMatchObject({
      target: "akb://other/doc/private.md",
      status: "unavailable",
    });
    expect(apiMocks.getDocument).toHaveBeenCalledWith("team", "notes/guide.md", undefined);
    expect(apiMocks.getAttachmentMetadata).toHaveBeenCalledWith("team", "123e4567-e89b-42d3-a456-426614174000", {
      document: undefined,
      commit: undefined,
    });
  });

  it("returns canonical upload and search values without runtime URLs", async () => {
    apiMocks.uploadAsset.mockResolvedValue({
      id: "123e4567-e89b-42d3-a456-426614174000",
      target: ATTACHMENT,
      url: ATTACHMENT,
      unclaimed_expires_at: "2026-09-10T00:00:00.000Z",
    });
    apiMocks.searchDocs.mockResolvedValue({
      results: [
        { uri: DOCUMENT, title: "Guide", matched_section: "body" },
        { uri: "https://example.test/nope", title: "External" },
      ],
    });
    const adapters = createAkbMarkdownAdapters({ vault: "team" });
    const file = new File(["bytes"], "diagram.png", { type: "image/png" });

    await expect(adapters.upload.upload(file)).resolves.toMatchObject({
      kind: "attachment",
      target: ATTACHMENT,
      expiresAt: "2026-09-10T00:00:00.000Z",
    });
    await expect(adapters.search.search("guide")).resolves.toEqual([
      {
        id: DOCUMENT,
        title: "Guide",
        snippet: "body",
        target: DOCUMENT,
        kind: "document",
      },
    ]);
    expect(apiMocks.uploadAsset.mock.calls[0]?.[0]).toBe("team");
  });
});
