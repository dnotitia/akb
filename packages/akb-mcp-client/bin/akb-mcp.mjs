#!/usr/bin/env node
/**
 * akb-mcp — stdio proxy for connecting Claude Code to AKB.
 *
 * Usage:
 *   akb-mcp --url https://akb.example.com/mcp/ --pat akb_xxx
 *   akb-mcp --url https://akb.example.com/mcp/ --pat akb_xxx --insecure
 *
 * Claude Code config:
 *   claude mcp add akb -- akb-mcp --url https://akb.example.com/mcp/ --pat akb_xxx
 */

import { AKBProxy } from "../lib/proxy.mjs";

function parseArgs(argv) {
  const args = { url: null, pat: null, insecure: false };

  for (let i = 2; i < argv.length; i++) {
    switch (argv[i]) {
      case "--url":
        args.url = argv[++i];
        break;
      case "--pat":
        args.pat = argv[++i];
        break;
      case "--insecure":
      case "-k":
        args.insecure = true;
        break;
      case "--help":
      case "-h":
        console.error(`akb-mcp — AKB MCP stdio client

Usage:
  akb-mcp --url <mcp-url> --pat <token> [--insecure]

Options:
  --url       AKB MCP endpoint (e.g. https://akb.example.com/mcp/)
  --pat       Personal Access Token (akb_xxx)
  --insecure  Skip TLS certificate verification
  -h, --help  Show this help

Setup by tool:

  Claude Code:
    claude mcp add --scope user akb -- npx akb-mcp --url <URL> --pat <TOKEN> --insecure

  Cursor (~/.cursor/mcp.json):
  Windsurf (~/.codeium/windsurf/mcp_config.json):
  Gemini CLI (~/.gemini/settings.json):
    {"mcpServers":{"akb":{"command":"npx","args":["akb-mcp","--url","<URL>","--pat","<TOKEN>","--insecure"]}}}

  OpenAI Codex CLI (~/.codex/config.toml):
    [mcp_servers.akb]
    command = "npx"
    args = ["akb-mcp", "--url", "<URL>", "--pat", "<TOKEN>", "--insecure"]

  VS Code Copilot (.vscode/mcp.json):
    {"servers":{"akb":{"type":"stdio","command":"npx","args":["akb-mcp","--url","<URL>","--pat","<TOKEN>","--insecure"]}}}

  OpenClaw (~/.openclaw/openclaw.json):
    {"mcp":{"servers":{"akb":{"command":"npx","args":["akb-mcp","--url","<URL>","--pat","<TOKEN>","--insecure"]}}}}`);
        process.exit(0);
    }
  }

  // Environment variable fallbacks
  args.url = args.url || process.env.AKB_MCP_URL;
  args.pat = args.pat || process.env.AKB_PAT;

  if (!args.url || !args.pat) {
    console.error(
      "Error: --url and --pat are required (or set AKB_MCP_URL and AKB_PAT env vars)"
    );
    process.exit(1);
  }

  return args;
}

const args = parseArgs(process.argv);

process.stderr.write(
  `[akb-mcp] Connecting to ${args.url}${args.insecure ? " (insecure)" : ""}\n`
);

const proxy = new AKBProxy(args);
proxy.start().catch((err) => {
  process.stderr.write(`[akb-mcp] Fatal: ${err.message}\n`);
  process.exit(1);
});
