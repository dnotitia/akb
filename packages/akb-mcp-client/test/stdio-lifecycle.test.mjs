import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import canonical from '../lib/generated-tools.json' with { type: 'json' };

const pause = (ms = 10) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(check, timeout = 4000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { const value = await check(); if (value) return value; await pause(); }
  throw new Error('Timed out waiting for stdio lifecycle outcome.');
}

async function runtime({ oauth = false, expiresIn = 3600 } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'akb-stdio-lifecycle-'));
  const openDirectory = join(root, 'bin');
  await mkdir(openDirectory, { mode: 0o700 });
  const opener = '#!/bin/sh\numask 077\nprintf "%s" "$1" > "$AKB_TEST_AUTH_URL"\n';
  for (const executable of ['open', 'xdg-open']) await writeFile(join(openDirectory, executable), opener, { mode: 0o700 });
  let origin;
  const calls = [];
  const tokenRequests = [];
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, origin);
    let body = '';
    for await (const chunk of request) body += chunk;
    response.setHeader('Content-Type', 'application/json');
    if (url.pathname.includes('oauth-protected-resource')) return response.end(JSON.stringify({ resource: origin + '/prefix/mcp', authorization_servers: [origin], akb_native_client_id: 'native-test', scopes_supported: ['akb:vault:read', 'akb:vault:write'] }));
    if (url.pathname.includes('.well-known')) return response.end(JSON.stringify({ issuer: origin, authorization_endpoint: origin + '/authorize', token_endpoint: origin + '/token', response_types_supported: ['code'], grant_types_supported: ['authorization_code', 'refresh_token'], code_challenge_methods_supported: ['S256'], token_endpoint_auth_methods_supported: ['none'], authorization_response_iss_parameter_supported: true }));
    if (url.pathname === '/token') { tokenRequests.push(new URLSearchParams(body).get('grant_type')); return response.end(JSON.stringify({ access_token: 'mock-access', refresh_token: 'mock-refresh', token_type: 'Bearer', expires_in: expiresIn })); }
    assert.equal(url.pathname, '/prefix/mcp/');
    const message = JSON.parse(body);
    if (message.method === 'server/discover') return response.end(JSON.stringify({ result: { supportedVersions: ['2026-07-28'] } }));
    if (message.method === 'tools/list') return response.end(JSON.stringify({ result: { tools: canonical } }));
    calls.push(message);
    if (message.params?.arguments?.hold) return;
    response.end(JSON.stringify({ result: { content: [{ type: 'text', text: '{"completed":true}' }], isError: false } }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  const env = { ...process.env, PATH: openDirectory + ':' + process.env.PATH, AKB_TEST_AUTH_URL: join(root, 'authorization-url'), AKB_MCP_AUTH_DIR: join(root, 'auth') };
  delete env.AKB_PAT;
  const args = [new URL('../bin/akb-mcp.mjs', import.meta.url).pathname, '--url', origin + '/prefix/mcp/', ...(oauth ? [] : ['--pat', 'selected-pat'])];
  const child = spawn(process.execPath, args, { env, stdio: ['pipe', 'pipe', 'pipe'] });
  let diagnostics = '';
  child.stderr.on('data', (chunk) => { diagnostics += chunk; });
  const lines = [];
  const reader = createInterface({ input: child.stdout });
  reader.on('line', (line) => lines.push(JSON.parse(line)));
  const exited = new Promise((resolve) => child.once('exit', (code, signal) => resolve({ code, signal })));
  const send = (id, method, params = {}) => child.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...(id == null ? {} : { id }), method, params }) + '\n');
  const response = (id) => until(() => lines.find((message) => message.id === id));
  let nextId = 1000;
  return {
    root, child, calls, tokenRequests, lines, send, response,
    async call(name, arguments_ = {}) { const id = nextId++; send(id, 'tools/call', { name, arguments: arguments_ }); return response(id); },
    async authorization() { return until(async () => { try { return new URL(await readFile(env.AKB_TEST_AUTH_URL, 'utf8')); } catch { return null; } }); },
    async eof() { child.stdin.end(); const result = await Promise.race([exited, pause(2000).then(() => { throw new Error('EOF did not stop the proxy.'); })]); assert.equal(result.code, 0, diagnostics); },
    async close() { if (child.exitCode == null) { child.kill('SIGKILL'); await exited; } reader.close(); server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); await rm(root, { recursive: true, force: true }); },
  };
}

function payload(response) { return JSON.parse(response.result.content[0].text); }

test('actual stdio keeps the full catalog responsive during mock OAuth and never retains pre-auth work', async () => {
  const run = await runtime({ oauth: true });
  try {
    run.send(1, 'initialize', { protocolVersion: '2025-06-18' });
    await run.response(1);
    run.send(2, 'tools/list'); run.send(3, 'ping');
    const tools = await run.response(2);
    assert.equal(tools.result.tools.length, canonical.length + 7);
    await run.response(3);
    const refused = await run.call('akb_create_vault', { name: 'never-retained' });
    assert.equal(payload(refused).execution, 'not_started');
    assert.equal(run.calls.length, 0);
    const authorization = await run.authorization();
    assert.equal(authorization.searchParams.get('resource').endsWith('/prefix/mcp'), true);
    const callback = new URL(authorization.searchParams.get('redirect_uri'));
    callback.searchParams.set('state', authorization.searchParams.get('state'));
    callback.searchParams.set('iss', authorization.origin);
    callback.searchParams.set('code', 'mock-code');
    await fetch(callback);
    await until(async () => payload(await run.call('akb_connection')).authentication === 'ready');
    assert.equal(run.calls.length, 0);
    assert.equal(payload(await run.call('akb_list_vaults')).completed, true);
    assert.equal(run.calls.length, 1);
    await run.eof();
    await assert.rejects(fetch(callback));
  } finally { await run.close(); }
});

test('actual stdio bounds active work, services local requests, cancels once and closes on EOF', async () => {
  const run = await runtime();
  try {
    run.send(1, 'initialize', { protocolVersion: '2025-06-18' }); await run.response(1);
    for (let id = 10; id < 42; id++) run.send(id, 'tools/call', { name: 'akb_update', arguments: { hold: true } });
    await until(() => run.calls.length === 32);
    run.send(42, 'tools/call', { name: 'akb_update', arguments: { hold: true } });
    const overload = await run.response(42);
    assert.match(overload.error.message, /did not start/);
    run.send(50, 'ping'); run.send(51, 'tools/list');
    await run.response(50); assert.equal((await run.response(51)).result.tools.length, canonical.length + 7);
    const status = payload(await run.call('akb_connection'));
    assert.equal(status.mode, 'pat');
    run.send(null, 'notifications/cancelled', { requestId: 10 });
    assert.equal(payload(await run.response(10)).execution, 'unknown');
    assert.equal(run.calls.length, 32);
    await run.eof();
    assert.equal(run.calls.length, 32);
  } finally { await run.close(); }
});

test('EOF during mock OAuth closes the owned loopback listener', async () => {
  const run = await runtime({ oauth: true });
  try {
    run.send(1, 'initialize'); await run.response(1);
    const authorization = await run.authorization();
    const callback = new URL(authorization.searchParams.get('redirect_uri'));
    await run.eof();
    await assert.rejects(fetch(callback));
    assert.equal(run.calls.length, 0);
  } finally { await run.close(); }
});


test('a short-lived mock OAuth token is ready for a new stdio call without an immediate refresh loop', async () => {
  const run = await runtime({ oauth: true, expiresIn: 5 });
  try {
    run.send(1, 'initialize'); await run.response(1);
    const authorization = await run.authorization();
    const callback = new URL(authorization.searchParams.get('redirect_uri'));
    callback.searchParams.set('state', authorization.searchParams.get('state'));
    callback.searchParams.set('iss', authorization.origin);
    callback.searchParams.set('code', 'mock-code');
    await fetch(callback);
    await until(async () => payload(await run.call('akb_connection')).authentication === 'ready');
    assert.equal(payload(await run.call('akb_list_vaults')).completed, true);
    await pause(650);
    assert.equal(payload(await run.call('akb_connection')).authentication, 'ready');
    assert.deepEqual(run.tokenRequests, ['authorization_code']);
    assert.equal(run.calls.length, 1);
    await run.eof();
  } finally { await run.close(); }
});
