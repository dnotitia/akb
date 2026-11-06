import assert from 'node:assert/strict';
import { createCatalog } from '../lib/catalog.mjs';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const catalog = createCatalog();
const tools = catalog.initialCatalog().tools;
assert.ok(catalog.canonical.length > 0, 'The generated backend catalog is empty.');
assert.equal(new Set(tools.map((tool) => tool.name)).size, tools.length, 'Tool names collide.');
for (const tool of tools) {
  assert.match(tool.name, /^[a-z][a-z0-9_]*$/);
  assert.equal(tool.inputSchema?.type, 'object', `${tool.name} must have an object input schema.`);
  assert.ok(tool.inputSchema.properties && typeof tool.inputSchema.properties === 'object', `${tool.name} has no properties contract.`);
}
const manifest = JSON.parse(readFileSync(new URL('../package.json', import.meta.url)));
assert.equal(manifest.dependencies['@modelcontextprotocol/client'], '2.0.0');
assert.ok(readFileSync(new URL('../lib/proxy.mjs', import.meta.url), 'utf8').includes(`const PROXY_VERSION = "${manifest.version}";`), 'Proxy protocol version must match the package version.');
process.stderr.write(`Validated ${catalog.canonical.length} canonical and ${tools.length - catalog.canonical.length} local tools.\n`);

const repository = fileURLToPath(new URL('../../../', import.meta.url));
const generated = fileURLToPath(new URL('../lib/generated-tools.json', import.meta.url));
const refinements = fileURLToPath(new URL('../lib/generated-tool-refinements.json', import.meta.url));
const check = spawnSync('uv', ['run', '--locked', '--project', resolve(repository, 'backend'), 'python', resolve(repository, 'scripts/generate_mcp_catalog.py'), '--output', generated, '--refinements-output', refinements, '--check'], { cwd: repository, stdio: 'inherit' });
if (check.error) throw new Error('Catalog generation requires a full repository checkout and uv on PATH.', { cause: check.error });
if (check.status !== 0) throw new Error('The catalog drift check failed. Regenerate both catalog artifacts before packing.');
