import { describe, expect, it } from "vitest";
import { effectiveFileMime, filePreviewKind } from "./file-preview";

describe("filePreviewKind", () => {
  it.each([
    ["application/yaml", "text"],
    ["text/yaml", "text"],
    ["text/plain", "text"],
    ["application/json", "json"],
    ["application/pdf", "pdf"],
    ["image/png", "image"],
    ["application/x-archive", "binary"],
  ])("classifies %s as %s", (mime, kind) => {
    expect(filePreviewKind(mime)).toBe(kind);
  });
});

describe("effectiveFileMime", () => {
  it.each([
    ["", "settings.yaml", "application/yaml"],
    ["application/octet-stream", "settings.yml", "application/yaml"],
    ["application/json", "settings.yaml", "application/json"],
    ["text/yaml", "settings.yaml", "text/yaml"],
    ["application/octet-stream", "settings.json", "application/json"],
  ])("resolves %s for %s to %s", (mime, name, expected) => {
    expect(effectiveFileMime(mime, name)).toBe(expected);
  });
});
