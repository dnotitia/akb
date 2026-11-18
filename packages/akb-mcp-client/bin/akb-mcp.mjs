#!/usr/bin/env node

import { parseConfiguration } from "../lib/config.mjs";
import { createCredentials } from "../lib/credentials.mjs";
import { requireSupportedNode } from "../lib/node-runtime.mjs";

const help = `Usage:
  akb-mcp login --url <mcp-url>
  akb-mcp status --url <mcp-url>
  akb-mcp logout --url <mcp-url>
  akb-mcp --url <mcp-url> [--auth-mode oauth]
  akb-mcp --url <mcp-url> [--auth-mode pat] --pat <token>

Set AKB_MCP_URL and AKB_PAT to supply the URL and PAT through the environment.
A URL without a PAT selects OAuth. Run login separately before starting the MCP host.`;

try {
  requireSupportedNode();
  const argv = process.argv.slice(2);
  if (argv.length === 1 && ["--help", "-h"].includes(argv[0])) {
    process.stderr.write(help + "\n");
  } else {
    const { command, installation, authentication } = parseConfiguration(argv);
    if (process.env.NODE_TLS_REJECT_UNAUTHORIZED === "0") {
      throw new Error("Enable TLS certificate verification before running akb-mcp.");
    }
    const controller = new AbortController();
    const abort = () => controller.abort();
    process.once("SIGINT", abort);
    process.once("SIGTERM", abort);
    try {
      const credentials = await createCredentials(authentication, installation, {
        signal: controller.signal,
        onAuthorizationUrl: (url) => process.stderr.write(`Open this URL to continue sign-in:\n${url}\n`),
      });
      try {
        if (command === "stdio") {
          const { AKBProxy } = await import("../lib/proxy.mjs");
          await new AKBProxy({ url: installation.endpointUrl, credentials }).start(controller);
        } else {
          const status = await credentials[command]();
          if (command === "logout") {
            process.stderr.write("[akb-mcp] Cleared the local grant. Other proxies using this URL will require login.\n");
          }
          process.stdout.write(JSON.stringify({ resource: installation.resource, ...status }) + "\n");
        }
      } finally {
        await credentials.close?.();
      }
    } finally {
      process.removeListener("SIGINT", abort);
      process.removeListener("SIGTERM", abort);
    }
  }
} catch (error) {
  process.stderr.write(`[akb-mcp] ${error.message}\n`);
  process.exitCode = 1;
}
