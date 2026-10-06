import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { makeServer, apiUrl, http, HttpResponse } from "@/test-msw";
import {
  FILE_PREVIEW_CASES,
  FILE_PREVIEW_PUBLICATION,
  FILE_PREVIEW_YAML_TEXT,
  filePreviewDiscovery,
} from "@/mocks/file-preview-fixtures";

vi.mock("msw/browser", () => ({
  setupWorker: vi.fn(() => ({ start: vi.fn() })),
}));

import { handlers } from "@/mocks/browser";

const server = makeServer(
  http.get("/__akb_mock__/fixture/state", () =>
    HttpResponse.json({ scenario: "file-preview", reset_generation: 0 }),
  ),
  ...handlers,
);

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("browser mock handlers", () => {
  it("serves the guide template used by the vault overview", async () => {
    const response = await fetch(
      new URL(apiUrl("/help/skill-template"), window.location.origin),
    );

    expect(response.status).toBe(200);
    expect(await response.text()).toBe(
      "# {vault} Guide\n\n(Describe what this vault is for.)\n",
    );
  });

  it("serves ordinary and public synthetic YAML files with the expected source", async () => {
    const ordinaryInfo = await fetch(new URL(
      apiUrl("/files/fixture/yaml-application"),
      window.location.origin,
    ));
    expect(ordinaryInfo.status).toBe(200);
    expect(await ordinaryInfo.json()).toMatchObject({
      name: "service.yaml",
      mime_type: "application/yaml",
    });

    const ordinaryAccess = await fetch(new URL(
      apiUrl("/files/fixture/yaml-application/download"),
      window.location.origin,
    ));
    const ordinary = await ordinaryAccess.json() as { download_url: string };
    const ordinaryRaw = await fetch(new URL(ordinary.download_url, window.location.origin));
    expect(ordinaryRaw.headers.get("content-type")).toBe("application/yaml");
    expect(await ordinaryRaw.text()).toBe(FILE_PREVIEW_YAML_TEXT);

    const publicResponse = await fetch(new URL(
      apiUrl("/public/" + FILE_PREVIEW_PUBLICATION.slug),
      window.location.origin,
    ));
    expect(publicResponse.status).toBe(200);
    expect(await publicResponse.json()).toMatchObject({
      resource_type: "file",
      name: FILE_PREVIEW_PUBLICATION.name,
      mime_type: "application/yaml",
    });
    const publicRaw = await fetch(new URL(
      apiUrl("/public/" + FILE_PREVIEW_PUBLICATION.slug + "/raw"),
      window.location.origin,
    ));
    expect(publicRaw.headers.get("content-type")).toBe("application/yaml");
    expect(await publicRaw.text()).toBe(FILE_PREVIEW_PUBLICATION.raw_text);

    const publicDownload = await fetch(new URL(
      apiUrl("/public/" + FILE_PREVIEW_PUBLICATION.slug + "/download"),
      window.location.origin,
    ));
    expect(publicDownload.headers.get("content-type")).toBe("application/yaml");
    expect(publicDownload.headers.get("content-disposition")).toBe(
      'attachment; filename="' + FILE_PREVIEW_PUBLICATION.name + '"',
    );
    expect(await publicDownload.text()).toBe(FILE_PREVIEW_PUBLICATION.raw_text);
  });

  it("serves the mock MIME matrix, including malformed YAML and existing viewer formats", async () => {
    for (const file of FILE_PREVIEW_CASES) {
      const infoResponse = await fetch(new URL(
        apiUrl("/files/fixture/" + file.id),
        window.location.origin,
      ));
      expect(infoResponse.status).toBe(200);
      expect(await infoResponse.json()).toMatchObject({
        name: file.name,
        mime_type: file.mime_type,
      });

      const accessResponse = await fetch(new URL(
        apiUrl("/files/fixture/" + file.id + "/download"),
        window.location.origin,
      ));
      const access = await accessResponse.json() as { download_url: string };
      const rawResponse = await fetch(new URL(access.download_url, window.location.origin));
      expect(rawResponse.status).toBe(200);
      expect(rawResponse.headers.get("content-type")).toBe(
        file.mime_type || "application/octet-stream",
      );
      if (file.raw_text !== null) {
        expect(await rawResponse.text()).toBe(file.raw_text);
      } else {
        expect(rawResponse.headers.get("content-type")).toContain(
          file.mime_type === "image/png" ? "image/png" : "application/pdf",
        );
      }
    }
  });

  it("exposes file preview entry URLs, MIME cases, and expected text through discovery data", () => {
    const discovery = filePreviewDiscovery("https://mock.example");
    expect(discovery.identity.start_url).toBe(
      "https://mock.example/vault/fixture/file/yaml-application",
    );
    expect(discovery.file_preview.ordinary.expected_text).toBe(FILE_PREVIEW_YAML_TEXT);
    expect(discovery.file_preview.public.entry_url).toBe(
      "https://mock.example/p/" + FILE_PREVIEW_PUBLICATION.slug,
    );
    expect(discovery.file_preview.public.expected_text).toBe(FILE_PREVIEW_PUBLICATION.raw_text);
    expect(discovery.file_preview.cases).toHaveLength(FILE_PREVIEW_CASES.length);
    expect(discovery.file_preview.cases).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "yaml-text", mime_type: "text/yaml", preview_kind: "Text" }),
      expect.objectContaining({ id: "yaml-missing", mime_type: null, preview_kind: "Text" }),
      expect.objectContaining({
        id: "yaml-generic",
        mime_type: "application/octet-stream",
        preview_kind: "Text",
      }),
      expect.objectContaining({
        id: "yaml-explicit-json",
        mime_type: "application/json",
        preview_kind: "JSON",
      }),
      expect.objectContaining({ id: "existing-image", preview_kind: "Image" }),
      expect.objectContaining({ id: "existing-pdf", preview_kind: "PDF" }),
    ]));
  });
});
