import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin, ViteDevServer } from "vite";
import {
  FILE_PREVIEW_PUBLICATION,
  FILE_PREVIEW_YAML_TEXT,
} from "@/mocks/file-preview-fixtures";

type Middleware = (
  request: IncomingMessage,
  response: ServerResponse,
  next: (error?: Error) => void,
) => void;

const middlewares: Middleware[] = [];
let restoreDirname: string | undefined;

function serve(method: string, url: string) {
  const headers = new Map<string, string>();
  let body = "";
  const request = {
    method,
    url,
    headers: { host: "127.0.0.1:4173" },
  } as IncomingMessage;
  const response = {
    statusCode: 200,
    setHeader(name: string, value: string | number | readonly string[]) {
      headers.set(name.toLowerCase(), String(value));
    },
    end(chunk?: string | Buffer) {
      body = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : chunk || "";
    },
  } as unknown as ServerResponse;
  const next = vi.fn((error?: Error) => {
    if (error) throw error;
    const index = next.mock.calls.length;
    middlewares[index]?.(request, response, next);
  });

  middlewares[0]?.(request, response, next);
  return { status: response.statusCode, headers, body, next };
}

beforeAll(async () => {
  vi.stubEnv("VITE_AKB_TEST_MODE", "mock");
  vi.stubEnv("AKB_FE_E2E_SCENARIO", "file-preview");
  restoreDirname = (globalThis as typeof globalThis & { __dirname?: string }).__dirname;
  (globalThis as typeof globalThis & { __dirname?: string }).__dirname = process.cwd();

  const { default: defineViteConfig } = await import("../../../vite.config");
  const config = await defineViteConfig({
    command: "serve",
    mode: "development",
    isSsrBuild: false,
    isPreview: false,
  });
  const entries = (config as unknown as { plugins?: unknown[] }).plugins || [];
  const flatten = (items: unknown[]): unknown[] => items.flatMap((item) =>
    Array.isArray(item) ? flatten(item) : item ? [item] : [],
  );
  const plugin = flatten(entries).find((candidate) =>
    typeof candidate === "object" && candidate !== null &&
    "name" in candidate && candidate.name === "akb-mock-control",
  ) as Plugin | undefined;
  if (!plugin || typeof plugin.configureServer !== "function") {
    throw new Error("Vite mock-control plugin was not configured");
  }

  (plugin.configureServer as unknown as (server: ViteDevServer) => void)({
    httpServer: null,
    middlewares: {
      use(handler: Middleware) {
        middlewares.push(handler);
      },
    },
  } as unknown as ViteDevServer);
});

afterAll(() => {
  vi.unstubAllEnvs();
  const global = globalThis as typeof globalThis & { __dirname?: string };
  if (restoreDirname === undefined) Reflect.deleteProperty(global, "__dirname");
  else global.__dirname = restoreDirname;
  middlewares.length = 0;
});

describe("Vite file-preview fixture responses", () => {
  it("serves ordinary YAML preview/download bytes with its stored MIME and filename", () => {
    const response = serve("GET", "/__akb_mock__/file-preview/raw/yaml-application");

    expect(response.next).not.toHaveBeenCalled();
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/yaml");
    expect(response.headers.get("content-disposition")).toBe('attachment; filename="service.yaml"');
    expect(response.body).toBe(FILE_PREVIEW_YAML_TEXT);
  });

  it("serves public YAML raw and download routes as the same source with exact headers", () => {
    const base = "/api/v1/public/" + FILE_PREVIEW_PUBLICATION.slug;
    const raw = serve("GET", base + "/raw");
    const download = serve("GET", base + "/download");

    for (const response of [raw, download]) {
      expect(response.next).not.toHaveBeenCalled();
      expect(response.status).toBe(200);
      expect(response.headers.get("content-type")).toBe("application/yaml");
      expect(response.body).toBe(FILE_PREVIEW_PUBLICATION.raw_text);
    }
    expect(raw.headers.get("content-disposition")).toBeUndefined();
    expect(download.headers.get("content-disposition")).toBe(
      'attachment; filename="' + FILE_PREVIEW_PUBLICATION.name + '"',
    );
  });
});
