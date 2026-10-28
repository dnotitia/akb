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

test("packed CLI rejects unpatched runtimes before connecting with default npm engine policy", async () => {
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
    const packed = await exec("npm", ["pack", packageRoot, "--json", "--pack-destination", root, "--ignore-scripts"], options);
    const archive = join(root, JSON.parse(packed.stdout)[0].filename);
    await exec("npm", ["install", archive, "--offline", "--ignore-scripts", "--no-audit", "--no-fund"], options);
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
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});
