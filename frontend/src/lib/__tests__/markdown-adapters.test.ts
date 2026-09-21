import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  copyFileToAttachment: vi.fn(),
  getAssetBlob: vi.fn(),
  getAttachmentMetadata: vi.fn(),
  getDocument: vi.fn(),
  getVaultFileDownloadUrl: vi.fn(),
  publicationAssetUrl: vi.fn(),
  refreshPublicationViewGrant: vi.fn(),
  searchDocs: vi.fn(),
  uploadAsset: vi.fn(),
}));

vi.mock("@/lib/api", () => apiMocks);

import {
  canonicalAkbMarkdownTarget,
  classifyAkbMarkdownTarget,
  createAkbMarkdownAdapters,
  createAkbMarkdownPublicationTargetResolver,
  createAkbMarkdownTargetResolver,
} from "@/lib/markdown-adapters";

const ATTACHMENT = "/api/assets/123e4567-e89b-42d3-a456-426614174000";
const DOCUMENT = "akb://team/coll/notes/doc/guide.md";
const FILE = "akb://team/coll/notes/file/123e4567-e89b-42d3-a456-426614174001";

describe("AKB Markdown target adapter", () => {
  beforeEach(() => {
    Object.values(apiMocks).forEach((mock) => mock.mockReset());
    apiMocks.getAssetBlob.mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:adapter-image"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
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

  it("resolves only accessible same-vault targets and keeps runtime URLs ephemeral", async () => {
    apiMocks.getDocument.mockResolvedValue({ path: "notes/guide.md" });
    apiMocks.getVaultFileDownloadUrl.mockResolvedValue({
      kind: "file",
      download_url: "https://signed.example/file?expires=60",
      expires_in: 60,
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
    const fileResolution = await resolver.resolve(FILE);
    expect(fileResolution).toMatchObject({
      target: FILE,
      kind: "file",
      status: "available",
      runtimeUrl: "https://signed.example/file?expires=60",
    });
    expect(fileResolution.status === "available" && fileResolution.expiresAt).toBeTruthy();
    const attachmentResolution = await resolver.resolve(ATTACHMENT);
    expect(attachmentResolution).toMatchObject({
      target: ATTACHMENT,
      kind: "attachment",
      status: "available",
    });
    expect(attachmentResolution.status === "available" && attachmentResolution.runtimeUrl).toBe(
      "blob:adapter-image",
    );
    expect(apiMocks.getAssetBlob).toHaveBeenCalledWith(
      "123e4567-e89b-42d3-a456-426614174000",
      "team",
      undefined,
      undefined,
    );
    if (attachmentResolution.status === "available") attachmentResolution.release?.();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:adapter-image");
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

  it("refreshes public publication URLs without fetching private bytes", async () => {
    apiMocks.publicationAssetUrl.mockReturnValue("/public/asset?grant=old");
    apiMocks.refreshPublicationViewGrant.mockResolvedValue("new");
    const resolver = createAkbMarkdownPublicationTargetResolver("release");

    const initial = await resolver.resolve(ATTACHMENT);
    expect(initial).toMatchObject({
      target: ATTACHMENT,
      status: "available",
      runtimeUrl: "/public/asset?grant=old",
    });
    if (initial.status !== "available") throw new Error("expected available publication image");
    const refreshed = await initial.refresh?.();
    expect(refreshed).toMatchObject({ status: "available" });
    expect(apiMocks.refreshPublicationViewGrant).toHaveBeenCalledWith("release");
    expect(apiMocks.getAssetBlob).not.toHaveBeenCalled();
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
        { uri: FILE, title: "Diagram", matched_section: "illustration" },
        { uri: "akb://other/coll/private/doc/guide.md", title: "Other vault" },
        { uri: "https://example.test/nope", title: "External" },
        { uri: "https://signed.example/file?expires=60", title: "Runtime URL" },
      ],
    });
    const adapters = createAkbMarkdownAdapters({ vault: "team" });
    const file = new File(["bytes"], "diagram.png", { type: "image/png" });
    const controller = new AbortController();

    await expect(adapters.upload.upload(file)).resolves.toMatchObject({
      kind: "attachment",
      target: ATTACHMENT,
      expiresAt: "2026-09-10T00:00:00.000Z",
    });
    await expect(adapters.search.search("guide", { vault: "team", signal: controller.signal })).resolves.toEqual([
      {
        id: DOCUMENT,
        title: "Guide",
        snippet: "body",
        target: DOCUMENT,
        kind: "document",
      },
      {
        id: FILE,
        title: "Diagram",
        snippet: "illustration",
        target: FILE,
        kind: "file",
      },
    ]);
    expect(apiMocks.uploadAsset.mock.calls[0]?.[0]).toBe("team");
    expect(apiMocks.searchDocs).toHaveBeenCalledWith("guide", "team", 20, {}, { signal: controller.signal });
  });

  it("does not present degraded retrieval as a genuine empty result", async () => {
    apiMocks.searchDocs.mockResolvedValue({ degraded: true, results: [] });
    const adapters = createAkbMarkdownAdapters({ vault: "team" });

    await expect(adapters.search.search("guide", { vault: "team" })).rejects.toThrow(
      "Search results are temporarily unavailable.",
    );
  });

  it("does present the results a degraded retrieval still carries", async () => {
    apiMocks.searchDocs.mockResolvedValue({
      degraded: true,
      results: [
        { uri: DOCUMENT, title: "Guide", matched_section: "body" },
        { uri: "akb://other/coll/private/doc/guide.md", title: "Other vault" },
      ],
    });
    const adapters = createAkbMarkdownAdapters({ vault: "team" });

    await expect(adapters.search.search("guide", { vault: "team" })).resolves.toEqual([
      {
        id: DOCUMENT,
        title: "Guide",
        snippet: "body",
        target: DOCUMENT,
        kind: "document",
      },
    ]);
  });
});
