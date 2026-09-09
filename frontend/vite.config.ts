import type { AddressInfo } from "node:net";
import { defineConfig, type Plugin, type ViteDevServer } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

// Dev proxy target — the local backend started by docker-compose unless an
// isolated repository runtime supplies its per-run backend origin.
const target = process.env.AKB_FRONTEND_BACKEND_URL || "http://localhost:8000";
const cacheDir = process.env.AKB_FRONTEND_CACHE_DIR;
const isHttps = false;
const requestedMockScenario = process.env.CRABBOX_RUNTIME_SCENARIO || "empty";
const mockScenario = requestedMockScenario === "document-edit-recovery"
  ? requestedMockScenario
  : "empty";
let mockResetGeneration = 0;

type MockFixtureDocument = {
  title: string;
  content: string;
  current_commit: string;
  updated_at: string;
};

type MockFixtureAsset = {
  id: string;
  filename: string;
  status: "unclaimed" | "claimed" | "discarded";
};

type MockFixtureState = {
  remote_document: MockFixtureDocument | null;
  refetch_generation: number;
  expire_draft_generation: number;
  faults: { save: number; upload: number };
  assets: MockFixtureAsset[];
  asset_sequence: number;
  commit_sequence: number;
};

const BASE_FIXTURE_DOCUMENT: MockFixtureDocument = {
  title: "Recovery document",
  content: "# Recovery document\n\nThe original body is safe to edit.",
  current_commit: "fixture-base-0001",
  updated_at: "2026-09-08T00:00:00.000Z",
};

let mockFixtureState: MockFixtureState = createMockFixtureState();

function createMockFixtureState(): MockFixtureState {
  return {
    remote_document: null,
    refetch_generation: 0,
    expire_draft_generation: 0,
    faults: { save: 0, upload: 0 },
    assets: [],
    asset_sequence: 0,
    commit_sequence: 1,
  };
}

function resetMockFixtureState() {
  mockFixtureState = createMockFixtureState();
}

function currentFixtureDocument(): MockFixtureDocument {
  return mockFixtureState.remote_document || BASE_FIXTURE_DOCUMENT;
}

function fixtureStateSnapshot() {
  const document = currentFixtureDocument();
  return {
    scenario: mockScenario,
    reset_generation: mockResetGeneration,
    identity: {
      user_id: "u-jylkim",
      vault: "fixture",
      document_path: "notes/recovery.md",
      document_uri: "akb://fixture/coll/notes/doc/recovery.md",
      start_url: "/vault/fixture/doc/notes%2Frecovery.md?view=edit",
      actors: ["editor-a", "editor-b"],
    },
    document,
    refetch_generation: mockFixtureState.refetch_generation,
    expire_draft_generation: mockFixtureState.expire_draft_generation,
    faults: mockFixtureState.faults,
    assets: mockFixtureState.assets,
  };
}

function readJsonBody(request: NodeJS.ReadableStream): Promise<Record<string, unknown>> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer | string) => chunks.push(Buffer.from(chunk)));
    request.on("end", () => {
      if (chunks.length === 0) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>);
      } catch {
        resolve({});
      }
    });
  });
}

function mockDocumentFromBody(body: Record<string, unknown>): MockFixtureDocument {
  mockFixtureState.commit_sequence += 1;
  return {
    title: typeof body.title === "string" ? body.title : currentFixtureDocument().title,
    content: typeof body.content === "string" ? body.content : currentFixtureDocument().content,
    current_commit: `fixture-commit-${String(mockFixtureState.commit_sequence).padStart(4, "0")}`,
    updated_at: new Date().toISOString(),
  };
}

function mockDescriptor(origin: string) {
  const fixture = mockScenario === "document-edit-recovery"
    ? {
        identity: {
          user_id: "u-jylkim",
          vault: "fixture",
          document_path: "notes/recovery.md",
          document_uri: "akb://fixture/coll/notes/doc/recovery.md",
          start_url: `${origin}/vault/fixture/doc/notes%2Frecovery.md?view=edit`,
          actors: ["editor-a", "editor-b"],
        },
        operations: {
          state: { method: "GET", url: `${origin}/__akb_mock__/fixture/state` },
          remote_revision: {
            method: "POST",
            url: `${origin}/__akb_mock__/fixture/remote-revision`,
            body: { actor: "editor-b", title: "Remote revision", content: "Remote body" },
          },
          refetch: {
            method: "POST",
            url: `${origin}/__akb_mock__/fixture/refetch`,
            body: {},
          },
          retryable_save_failure: {
            method: "POST",
            url: `${origin}/__akb_mock__/fixture/failure`,
            body: { operation: "save", times: 1, status: 503 },
          },
          retryable_upload_failure: {
            method: "POST",
            url: `${origin}/__akb_mock__/fixture/failure`,
            body: { operation: "upload", times: 1, status: 503 },
          },
          expire_draft: {
            method: "POST",
            url: `${origin}/__akb_mock__/fixture/expire-draft`,
            body: {},
          },
        },
      }
    : null;
  return {
    schema_version: 2,
    status: "ready",
    mode: "mock",
    scenario: mockScenario,
    services: {
      web: {
        origin,
        health: { method: "GET", url: `${origin}/__akb_mock__/health` },
        discovery: { method: "GET", url: `${origin}/__akb_mock__/discover` },
        reset: {
          method: "POST",
          url: `${origin}/__akb_mock__/reset`,
          body: { scenario: mockScenario },
        },
      },
    },
    access: { login: { method: "browser", url: `${origin}/auth` } },
    mock: {
      worker_url: `${origin}/mockServiceWorker.js`,
      unhandled_api: "error",
      fixture,
    },
  };
}

function mockOriginFromAddress(address: AddressInfo) {
  const host = ["0.0.0.0", "::"].includes(address.address)
    ? "127.0.0.1"
    : address.address;
  const formattedHost = host.includes(":") ? `[${host}]` : host;
  return `http://${formattedHost}:${address.port}`;
}

function publishMockDescriptor(server: ViteDevServer) {
  let published = false;
  const publish = () => {
    if (published) return;
    const address = server.httpServer?.address();
    if (!address || typeof address === "string") return;
    published = true;
    process.stdout.write(`${JSON.stringify(mockDescriptor(mockOriginFromAddress(address)))}\n`);
  };

  if (server.httpServer?.listening) publish();
  else server.httpServer?.once("listening", publish);
}

function mockControlPlugin(): Plugin {
  return {
    name: "akb-mock-control",
    configureServer(server) {
      if (process.env.VITE_AKB_TEST_MODE !== "mock") return;
      publishMockDescriptor(server);
      server.middlewares.use((request, response, next) => {
        const pathname = new URL(request.url || "/", "http://localhost").pathname;
        if (!pathname.startsWith("/__akb_mock__/")) {
          next();
          return;
        }

        const origin = `http://${request.headers.host || "127.0.0.1:4173"}`;
        response.setHeader("Content-Type", "application/json");
        if (pathname === "/__akb_mock__/health" && request.method === "GET") {
          response.end(
            JSON.stringify({
              status: "ready",
              mode: "mock",
              scenario: mockScenario,
              reset_generation: mockResetGeneration,
            }),
          );
          return;
        }
        if (pathname === "/__akb_mock__/discover" && request.method === "GET") {
          response.end(JSON.stringify(mockDescriptor(origin)));
          return;
        }
        if (pathname === "/__akb_mock__/reset" && request.method === "POST") {
          mockResetGeneration += 1;
          resetMockFixtureState();
          response.end(
            JSON.stringify({
              status: "ready",
              mode: "mock",
              scenario: mockScenario,
              reset_generation: mockResetGeneration,
            }),
          );
          return;
        }
        if (pathname === "/__akb_mock__/fixture/state" && request.method === "GET") {
          response.end(JSON.stringify(fixtureStateSnapshot()));
          return;
        }
        if (pathname === "/__akb_mock__/fixture/remote-revision" && request.method === "POST") {
          void readJsonBody(request).then((body) => {
            mockFixtureState.remote_document = mockDocumentFromBody(body);
            response.end(JSON.stringify({ status: "ready", action: "remote-revision", document: currentFixtureDocument() }));
          });
          return;
        }
        if (pathname === "/__akb_mock__/fixture/refetch" && request.method === "POST") {
          mockFixtureState.refetch_generation += 1;
          response.end(JSON.stringify({ status: "ready", refetch_generation: mockFixtureState.refetch_generation }));
          return;
        }
        if (pathname === "/__akb_mock__/fixture/failure" && request.method === "POST") {
          void readJsonBody(request).then((body) => {
            const operation = body.operation === "upload" ? "upload" : "save";
            const times = typeof body.times === "number" && Number.isInteger(body.times)
              ? Math.max(0, Math.min(body.times, 5))
              : 1;
            mockFixtureState.faults[operation] = times;
            response.end(JSON.stringify({ status: "ready", operation, times }));
          });
          return;
        }
        if (pathname === "/__akb_mock__/fixture/expire-draft" && request.method === "POST") {
          mockFixtureState.expire_draft_generation += 1;
          response.end(JSON.stringify({ status: "ready", expire_draft_generation: mockFixtureState.expire_draft_generation }));
          return;
        }
        if (pathname === "/__akb_mock__/fixture/consume-fault" && request.method === "POST") {
          void readJsonBody(request).then((body) => {
            const operation = body.operation === "upload" ? "upload" : "save";
            const consumed = mockFixtureState.faults[operation] > 0;
            if (consumed) mockFixtureState.faults[operation] -= 1;
            response.end(JSON.stringify({ status: "ready", consumed, operation }));
          });
          return;
        }
        if (pathname === "/__akb_mock__/fixture/upload" && request.method === "POST") {
          void readJsonBody(request).then((body) => {
            mockFixtureState.asset_sequence += 1;
            const id = typeof body.id === "string" ? body.id : `fixture-asset-${mockFixtureState.asset_sequence}`;
            mockFixtureState.assets.push({
              id,
              filename: typeof body.filename === "string" ? body.filename : "fixture.png",
              status: "unclaimed",
            });
            response.end(JSON.stringify({ status: "ready", id }));
          });
          return;
        }
        if (pathname === "/__akb_mock__/fixture/commit" && request.method === "POST") {
          void readJsonBody(request).then((body) => {
            const expected = typeof body.expected_commit === "string" ? body.expected_commit : "";
            if (expected && expected !== currentFixtureDocument().current_commit) {
              response.statusCode = 409;
              response.end(JSON.stringify({ error: "current_commit moved", code: "conflict" }));
              return;
            }
            mockFixtureState.remote_document = mockDocumentFromBody(body);
            const assetIds = Array.isArray(body.asset_ids)
              ? body.asset_ids.filter((assetId): assetId is string => typeof assetId === "string")
              : [];
            for (const asset of mockFixtureState.assets) {
              if (asset.status === "unclaimed") asset.status = "claimed";
            }
            response.end(JSON.stringify({ status: "ready", document: currentFixtureDocument() }));
          });
          return;
        }
        if (pathname === "/__akb_mock__/fixture/discard" && request.method === "POST") {
          void readJsonBody(request).then((body) => {
            const id = typeof body.id === "string" ? body.id : "";
            const asset = mockFixtureState.assets.find((candidate) => candidate.id === id);
            if (asset && asset.status === "unclaimed") asset.status = "discarded";
            response.end(JSON.stringify({ status: "ready", id, discarded: asset?.status === "discarded" }));
          });
          return;
        }
        response.statusCode = 404;
        response.end(JSON.stringify({ error: "mock_control_not_found" }));
      });
    },
  };
}

export default defineConfig(() => {
  return {
    plugins: [react(), tailwindcss(), mockControlPlugin()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
      // react-force-graph-2d ships CJS and nests react-kapsule + prop-types;
      // without dedupe Vite can load a second React copy for them, making
      // React.useRef resolve to null inside ForceGraph2D. Dedupe forces
      // every consumer onto the same React instance.
      dedupe: ["react", "react-dom"],
    },
    optimizeDeps: {
      include: ["react-force-graph-2d", "react-kapsule"],
    },
    cacheDir,
    server: {
      proxy: {
        "/api": {
          target,
          changeOrigin: true,
          secure: isHttps,
        },
        "/mcp": {
          target,
          changeOrigin: true,
          secure: isHttps,
        },
        "/livez": { target, changeOrigin: true, secure: isHttps },
        "/readyz": { target, changeOrigin: true, secure: isHttps },
        "/health": { target, changeOrigin: true, secure: isHttps },
      },
    },
  };
});
