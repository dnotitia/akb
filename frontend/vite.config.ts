import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

// Dev proxy target — the local backend started by docker-compose.
const target = "http://localhost:8000";
const isHttps = false;

export default defineConfig(() => {
  return {
    plugins: [react(), tailwindcss()],
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
