import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { AKBProxy } from '../lib/proxy.mjs';
import { parseConfiguration } from '../lib/config.mjs';

const fileId = '11111111-2222-4333-8444-555555555555';
const replacementId = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
const uri = `akb://example/file/${fileId}`;
const assetId = 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff';
const bytes = Buffer.alloc(2 * 1024 * 1024, 71);
const hash = createHash('sha256').update(bytes).digest('hex');

async function fixture({ rejectConfirm = false, badHash = false, redirectBytes = false, dropConfirm = false, malformedConfirm = false, removeSource = null } = {}) {
  let origin;
  let generation = 0;
  const controls = [];
  const transfers = [];
  const server = createServer(async (request, response) => {
    const path = new URL(request.url, origin).pathname;
    const body = [];
    for await (const chunk of request) body.push(chunk);
    const raw = Buffer.concat(body);
    if (path.startsWith('/bytes/')) {
      transfers.push({ path, authorization: request.headers.authorization, bytes: raw });
      if (redirectBytes) return response.writeHead(307, { Location: origin + '/unexpected' }).end();
      if (request.method === 'PUT') return response.writeHead(200).end();
      return response.writeHead(200).end(bytes);
    }
    assert.equal(request.headers.authorization, `Bearer current-${generation}`);
    generation++;
    if (path === '/prefix/mcp/') {
      const rpc = JSON.parse(raw);
      if (rpc.method === 'server/discover') return response.end(JSON.stringify({ result: { supportedVersions: ['2026-07-28'] } }));
      return response.end(JSON.stringify({ result: { content: [{ type: 'text', text: '{}' }], isError: false } }));
    }
    assert.ok(path.startsWith('/prefix/api/v1/'));
    controls.push({ path, method: request.method });
    let result;
    if (path.endsWith('/upload')) { if (removeSource) await rm(removeSource); result = { uri, upload_url: origin + '/bytes/upload' }; }
    else if (path.endsWith('/download')) result = { name: 'file.bin', download_url: origin + '/bytes/download', size_bytes: bytes.length, content_hash: badHash ? '0'.repeat(64) : hash, hash_algorithm: 'sha256' };
    else if (path.endsWith('/replace')) result = { replacement_id: replacementId, upload_url: origin + '/bytes/replace', mime_type: 'application/octet-stream' };
    else if (path.endsWith('/confirm')) {
      if (dropConfirm) return response.destroy();
      if (malformedConfirm) return response.writeHead(200).end('upstream-private-marker');
      if (rejectConfirm) return response.writeHead(401).end('upstream-private-marker');
      result = { uri, file_id: fileId, content_hash: hash, hash_algorithm: 'sha256' };
    } else if (path === '/prefix/api/v1/assets/example') result = { id: assetId, url: `/api/assets/${assetId}`, name: 'image.png', mime_type: 'image/png', size_bytes: raw.length, width: 1, height: 1 };
    else if (path.includes('/assets/')) result = { discarded: true };
    else result = { deleted: true };
    response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(result));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  const configuration = parseConfiguration(['--url', origin + '/prefix/mcp/', '--auth-mode', 'oauth'], {});
  const owner = { currentCredential: () => ({ kind: 'oauth', grantId: 'test-grant', generation, accessToken: `current-${generation}` }), status: () => ({ mode: 'oauth', authentication: 'ready' }), rejectCredential() {}, close() {} };
  const proxy = new AKBProxy({ ...configuration, credentialOwner: owner });
  proxy._startBackendMonitor = () => {};
  const call = async (name, arguments_) => {
    const response = await proxy.context.run({ signal: new AbortController().signal, receipt: null }, () => proxy._handle({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: arguments_ } }));
    return { ...JSON.parse(response.result.content[0].text), isError: response.result.isError };
  };
  return { proxy, controls, transfers, call, async close() { proxy.connection.close(); server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); } };
}

test('all eight mocked controls use the current generation and capability bytes have no general bearer', async () => {
  const root = await mkdtemp(join(tmpdir(), 'akb-transfer-'));
  const run = await fixture();
  try {
    const file = join(root, 'source.bin'); await writeFile(file, bytes);
    assert.equal((await run.call('akb_put_file', { vault: 'example', file_path: file })).isError, false);
    const destination = join(root, 'download.bin');
    assert.equal((await run.call('akb_get_file', { uri, save_to: destination })).isError, false);
    assert.deepEqual(await readFile(destination), bytes);
    assert.equal((await run.call('akb_update_file', { uri, file_path: file })).isError, false);
    assert.equal((await run.call('akb_delete_file', { uri })).isError, false);
    const image = join(root, 'image.png'); await writeFile(image, Buffer.from([137, 80, 78, 71]));
    assert.equal((await run.call('akb_put_image', { vault: 'example', file_path: image })).isError, false);
    assert.equal((await run.call('akb_discard_image', { vault: 'example', url: `/api/assets/${assetId}` })).isError, false);
    assert.equal(run.controls.length, 8);
    assert.equal(run.transfers.length, 3);
    for (const transfer of run.transfers) assert.equal(transfer.authorization, undefined);
    assert.deepEqual(run.transfers[0].bytes, bytes);
    assert.deepEqual(run.transfers[2].bytes, bytes);
  } finally { await run.close(); await rm(root, { recursive: true, force: true }); }
});

for (const lost of [false, true]) {
  test(`a ${lost ? 'lost' : 'rejected'} confirm keeps a stable receipt and submits each stage once`, async () => {
    const root = await mkdtemp(join(tmpdir(), 'akb-transfer-partial-'));
    const run = await fixture({ rejectConfirm: !lost, dropConfirm: lost });
    try {
      const file = join(root, 'source.bin'); await writeFile(file, bytes);
      const result = await run.call('akb_put_file', { vault: 'example', file_path: file });
      assert.equal(result.execution, lost ? 'unknown' : 'partial');
      assert.deepEqual(result.receipt, { stage: 'uploaded', vault: 'example', uri, file_id: fileId });
      assert.equal(run.controls.length, 2);
      assert.equal(run.transfers.length, 1);
      assert.doesNotMatch(JSON.stringify(result), /upstream-private-marker/);
    } finally { await run.close(); await rm(root, { recursive: true, force: true }); }
  });
}

for (const problem of ['hash', 'redirect']) {
  test(`download ${problem} failure preserves the destination and never follows a byte redirect`, async () => {
    const root = await mkdtemp(join(tmpdir(), 'akb-transfer-integrity-'));
    const run = await fixture({ badHash: problem === 'hash', redirectBytes: problem === 'redirect' });
    try {
      const destination = join(root, 'download.bin'); await writeFile(destination, 'existing bytes');
      const result = await run.call('akb_get_file', { uri, save_to: destination });
      assert.equal(result.isError, true);
      assert.equal(result.execution, 'partial');
      assert.equal(await readFile(destination, 'utf8'), 'existing bytes');
      assert.equal(run.transfers.length, 1);
    } finally { await run.close(); await rm(root, { recursive: true, force: true }); }
  });
}

test('write-only guide refusal is narrowly admitted while auth and ACL failures propagate', async () => {
  const run = await fixture();
  try {
    run.proxy._ensureBackend = async () => true;
    run.proxy._rpc = async () => ({ isError: true, content: [{ type: 'text', text: JSON.stringify({ code: 'insufficient_scope', details: { required_scope: 'akb:vault:read' } }) }] });
    assert.equal(await run.proxy._fileToolSkillPreflight('example'), null);
    for (const code of ['authentication_rejected', 'access_denied', 'insufficient_scope']) {
      run.proxy._rpc = async () => ({ isError: true, content: [{ type: 'text', text: JSON.stringify({ code, details: { required_scope: 'akb:vault:write' } }) }] });
      await assert.rejects(run.proxy._fileToolSkillPreflight('example'), (error) => error.code === code);
    }
    assert.equal(run.controls.length, 0);
  } finally { await run.close(); }
});


test('a local byte source disappearing after reservation settles once without confirming', async () => {
  const root = await mkdtemp(join(tmpdir(), 'akb-transfer-source-interrupted-'));
  const file = join(root, 'source.bin');
  await writeFile(file, bytes);
  const run = await fixture({ removeSource: file });
  let timeout;
  try {
    const result = await Promise.race([
      run.call('akb_put_file', { vault: 'example', file_path: file }),
      new Promise((_resolve, reject) => { timeout = setTimeout(() => reject(new Error('Interrupted local source did not settle promptly.')), 2000); }),
    ]);
    assert.equal(result.isError, true);
    assert.equal(result.execution, 'unknown');
    assert.deepEqual(result.receipt, { stage: 'reserved', vault: 'example', uri, file_id: fileId });
    assert.equal(run.controls.filter((operation) => operation.path.endsWith('/upload')).length, 1);
    assert.equal(run.controls.filter((operation) => operation.path.endsWith('/confirm')).length, 0);
  } finally { clearTimeout(timeout); await run.close(); await rm(root, { recursive: true, force: true }); }
});


test('a malformed dispatched confirm reports unknown without upstream response snippets', async () => {
  const root = await mkdtemp(join(tmpdir(), 'akb-transfer-malformed-'));
  const run = await fixture({ malformedConfirm: true });
  try {
    const file = join(root, 'source.bin'); await writeFile(file, bytes);
    const result = await run.call('akb_put_file', { vault: 'example', file_path: file });
    assert.equal(result.execution, 'unknown');
    assert.equal(result.receipt.file_id, fileId);
    assert.equal(run.controls.length, 2);
    assert.doesNotMatch(JSON.stringify(result), /upstream-private-marker/);
  } finally { await run.close(); await rm(root, { recursive: true, force: true }); }
});
