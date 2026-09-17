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
            "react-force-graph-2d",
            "@tanstack/react-virtual",
          ]
            .filter((dep) => dep !== "react-kapsule"),
        ),
      ),
    };
    return config;
  },
};

export default config;
