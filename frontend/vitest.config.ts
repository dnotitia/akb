import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    globals: false,
    // Playwright lives under e2e/ and runs via `npm run test:e2e`.
    // Excluding here prevents vitest from importing @playwright/test,
    // which complains when invoked outside a Playwright runner.
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
});
