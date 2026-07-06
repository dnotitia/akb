import path from "node:path";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import type { StorybookConfig } from "@storybook/react-vite";

const dirname = path.dirname(fileURLToPath(import.meta.url));

const config: StorybookConfig = {
  stories: ["../src/**/*.stories.@(js|jsx|mjs|ts|tsx)"],
  addons: [
    "@storybook/addon-docs",
    "@storybook/addon-a11y",
    "@storybook/addon-vitest",
    "@storybook/addon-themes",
  ],
  framework: {
    name: "@storybook/react-vite",
    options: {},
  },
  staticDirs: ["../public", "./public"],
  docs: {
    autodocs: "tag",
  },
  core: {
    disableTelemetry: true,
  },
  async viteFinal(config) {
    config.plugins = [...(config.plugins || []), tailwindcss()];
    config.resolve = {
      ...config.resolve,
      alias: {
        ...(config.resolve?.alias || {}),
        "@": path.resolve(dirname, "../src"),
      },
      dedupe: Array.from(new Set([...(config.resolve?.dedupe || []), "react", "react-dom"])),
    };
    config.optimizeDeps = {
      ...config.optimizeDeps,
      include: Array.from(
        new Set(
          [
            ...(config.optimizeDeps?.include || []),
            "@platejs/basic-nodes/react",
            "@platejs/code-block/react",
            "@platejs/link",
            "@platejs/link/react",
            "@platejs/list",
            "@platejs/list/react",
            "@platejs/markdown",
            "@platejs/table",
            "@platejs/table/react",
            "react-force-graph-2d",
            "platejs/react",
            "@tanstack/react-virtual",
            "react-markdown",
            "rehype-katex",
            "remark-gfm",
            "remark-math",
          ]
            .filter((dep) => dep !== "react-kapsule"),
        ),
      ),
    };
    return config;
  },
};

export default config;
