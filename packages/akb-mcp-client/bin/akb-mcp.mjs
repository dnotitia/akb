#!/usr/bin/env node
import { requireSupportedNode } from '../lib/node-runtime.mjs';
import { parseConfiguration } from '../lib/config.mjs';

try {
  requireSupportedNode();
  if (process.argv.slice(2).some((arg) => ['--help', '-h'].includes(arg))) {
    process.stderr.write('akb-mcp\n\nUsage: akb-mcp --url <https://host/prefix/mcp> [--pat <token>] [--auth-mode oauth|pat]\n\nA URL-only invocation selects browser OAuth. --pat or AKB_PAT selects PAT.\nAKB_MCP_URL supplies the endpoint. HTTP is allowed only on loopback for development.\nUse akb_connection to check status, sign in, cancel, or sign out.\n');
  } else {
    const configuration = parseConfiguration(process.argv.slice(2));
    const { AKBProxy } = await import('../lib/proxy.mjs');
    await new AKBProxy(configuration).start();
  }
} catch (error) {
  process.stderr.write(`[akb-mcp] ${error.message}\n`);
  process.exitCode = 1;
}
