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
let mockResetGeneration = 0;

function mockDescriptor(origin: string) {
  return {
    schema_version: 2,
    status: "ready",
    mode: "mock",
    scenario: "empty",
    services: {
      web: {
        origin,
        health: { method: "GET", url: `${origin}/__akb_mock__/health` },
        discovery: { method: "GET", url: `${origin}/__akb_mock__/discover` },
        reset: {
          method: "POST",
          url: `${origin}/__akb_mock__/reset`,
          body: { scenario: "empty" },
        },
      },
    },
    access: { login: { method: "browser", url: `${origin}/auth` } },
    mock: {
      worker_url: `${origin}/mockServiceWorker.js`,
      unhandled_api: "error",
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
          response.end(
            JSON.stringify({
              status: "ready",
              mode: "mock",
              scenario: "empty",
              reset_generation: mockResetGeneration,
            }),
          );
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
