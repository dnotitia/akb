import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import test from "node:test";

const exec = promisify(execFile);
const packageRoot = fileURLToPath(new URL("../", import.meta.url));

test("production tarball includes the SDK and catalog and rejects unsupported runtimes before connection", async () => {
  const root = await mkdtemp(join(tmpdir(), "akb-packed-runtime-"));
  let requests = 0;
  const server = createServer((_req, res) => {
    requests += 1;
    res.writeHead(500).end();
  });
  try {
    const consumer = join(root, "consumer");
    await mkdir(consumer);
    await writeFile(join(consumer, "package.json"), '{"private":true}\n');
    const env = { ...process.env, npm_config_engine_strict: "false" };
    const options = { cwd: consumer, env, timeout: 30_000 };
    const packed = await exec("npm", ["pack", packageRoot, "--json", "--pack-destination", root], options);
    const archive = join(root, JSON.parse(packed.stdout)[0].filename);
    await exec("npm", ["install", archive, "--omit=dev", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"], options);
    const installed = join(consumer, "node_modules", "akb-mcp");
    await assert.rejects(readFile(join(installed, ".npmrc")), { code: "ENOENT" });
    const cli = join(installed, "bin", "akb-mcp.mjs");
    const help = await exec(process.execPath, [cli, "--help"], options);
    assert.match(help.stderr, /Usage:/);

    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const url = `http://127.0.0.1:${server.address().port}/mcp/`;
    // Exercise each release boundary in the installed CLI, without requiring
    // multiple Node installations for the ordinary unit-test gate.
    const preload = join(root, "node-version.mjs");
    for (const version of ["22.23.3", "24.22.0", "26.12.0", "20.20.0", "25.0.0"]) {
      await writeFile(preload, `Object.defineProperty(process.versions, "node", { value: ${JSON.stringify(version)} });\n`);
      await assert.rejects(
        exec(process.execPath, ["--import", preload, cli, "--url", url, "--pat", "synthetic-test-token"], options),
        (error) => {
          assert.equal(error.code, 1);
          assert.equal(error.stdout, "");
          assert.ok(error.stderr.includes(`Unsupported Node.js ${version}`));
          assert.match(error.stderr, /Upgrade Node.js/);
          assert.doesNotMatch(error.stderr, /Connecting|synthetic-test-token/);
          return true;
        },
      );
    }
    assert.equal(requests, 0);
    const sdk = JSON.parse(await readFile(join(consumer, "node_modules/@modelcontextprotocol/client/package.json"), "utf8"));
    assert.equal(sdk.version, "2.0.0");
    for (const excluded of ["backend", "test", "scripts", "node_modules/@modelcontextprotocol/inspector"]) await assert.rejects(readFile(join(installed, excluded)), { code: "ENOENT" });
    await assert.rejects(readFile(join(consumer, "node_modules/@modelcontextprotocol/inspector/package.json")), { code: "ENOENT" });
    const canonical = JSON.parse(await readFile(join(installed, "lib/generated-tools.json"), "utf8"));
    const refinements = JSON.parse(await readFile(join(installed, "lib/generated-tool-refinements.json"), "utf8"));
    assert.deepEqual(Object.keys(refinements), ["akb_create_vault"]);
    assert.equal(refinements.akb_create_vault.length, 3);
    const driver = join(consumer, "consumer.mjs");
    await writeFile(driver, `import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';
const transport = new StdioClientTransport({ command: process.execPath, args: [process.env.TEST_CLI, '--url', process.env.TEST_MCP_URL, '--pat', 'synthetic-test-token'], env: { PATH: process.env.PATH }, stderr: 'pipe' });
const client = new Client({ name: 'packed-production-consumer', version: '1' }, { versionNegotiation: { mode: { pin: '2026-07-28' } } });
try {
  await client.connect(transport, { timeout: 3000 });
  transport.stderr?.resume();
  const catalog = await client.listTools();
  const status = await client.callTool({ name: 'akb_connection', arguments: {} });
  const action = await client.callTool({ name: 'akb_connection', arguments: { action: 'sign_in' } });
  process.stdout.write(JSON.stringify({ names: catalog.tools.map(tool => tool.name), ttlMs: catalog.ttlMs, status: JSON.parse(status.content[0].text), action: JSON.parse(action.content[0].text) }));
} finally { await client.close(); }
`);
    const proof = await exec(process.execPath, [driver], { ...options, env: { ...env, TEST_CLI: cli, TEST_MCP_URL: url } });
    const observed = JSON.parse(proof.stdout);
    assert.equal(observed.names.length, canonical.length + 7);
    for (const tool of canonical) assert.ok(observed.names.includes(tool.name));
    assert.equal(observed.ttlMs, 0);
    assert.equal(observed.status.mode, "pat");
    assert.equal(observed.status.authentication, "ready");
    assert.equal(observed.action.code, "inapplicable_action");
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});
