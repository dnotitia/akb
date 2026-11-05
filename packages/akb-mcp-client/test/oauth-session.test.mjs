import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer, request as httpRequest } from 'node:http';
import { AKBProxy } from '../lib/proxy.mjs';
import { mkdtemp, rm, chmod, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { OAuthSession } from '../lib/oauth-session.mjs';
import { OAuthStore } from '../lib/oauth-store.mjs';
import { createCallback } from '../lib/oauth-callback.mjs';

const pause = (ms = 10) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(check, timeout = 3000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { const value = check(); if (value) return value; await pause(); }
  throw new Error('Timed out waiting for the expected OAuth state.');
}

async function mockIssuer({ advertisedAuthMethods = ['none'] } = {}) {
  let origin;
  let authorization;
  let generation = 0;
  let available = true;
  let openings = 0;
  const exchanges = [];
  let holdRefresh = null;
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, origin);
    let body = '';
    for await (const chunk of request) body += chunk;
    response.setHeader('Content-Type', 'application/json');
    if (!available && url.pathname.includes('.well-known')) return response.writeHead(503).end('{}');
    if (url.pathname === '/.well-known/oauth-protected-resource/prefix/mcp') return response.end(JSON.stringify({ resource: origin + '/prefix/mcp', authorization_servers: [origin], scopes_supported: ['akb:vault:read', 'akb:vault:write'], akb_native_client_id: 'native-test' }));
    if (url.pathname.includes('.well-known')) return response.end(JSON.stringify({ issuer: origin, authorization_endpoint: origin + '/authorize', token_endpoint: origin + '/token', response_types_supported: ['code'], grant_types_supported: ['authorization_code', 'refresh_token'], code_challenge_methods_supported: ['S256'], token_endpoint_auth_methods_supported: advertisedAuthMethods, authorization_response_iss_parameter_supported: true }));
    if (url.pathname === '/token') {
      const params = new URLSearchParams(body);
      assert.equal(params.get('resource'), origin + '/prefix/mcp');
      assert.equal(params.get('client_id'), 'native-test');
      assert.equal(params.has('client_secret'), false);
      assert.equal(request.headers.authorization, undefined);
      exchanges.push(params.get('grant_type'));
      if (params.get('grant_type') === 'authorization_code') {
        assert.equal(params.get('redirect_uri'), authorization.searchParams.get('redirect_uri'));
        assert.ok(params.get('code_verifier').length >= 43);
        assert.equal(params.get('code'), 'test-code');
      } else {
        assert.equal(params.get('refresh_token'), `refresh-${generation}`);
        if (holdRefresh) await holdRefresh;
      }
      generation++;
      return response.end(JSON.stringify({ access_token: `access-${generation}`, refresh_token: `refresh-${generation}`, expires_in: 3600, token_type: 'Bearer' }));
    }
    response.writeHead(404).end('{}');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  return {
    installation: { resource: origin + '/prefix/mcp', endpointUrl: origin + '/prefix/mcp/', baseUrl: origin + '/prefix' }, exchanges,
    browserOpener: async (value) => { openings++; authorization = new URL(value); return false; },
    openings: () => openings,
    outage: (value) => { available = !value; },
    authorization: () => authorization,
    async finish() {
      await until(() => authorization);
      assert.equal(authorization.searchParams.get('resource'), origin + '/prefix/mcp');
      assert.equal(authorization.searchParams.get('code_challenge_method'), 'S256');
      assert.equal(authorization.searchParams.get('scope').includes('offline_access'), false);
      const callback = new URL(authorization.searchParams.get('redirect_uri'));
      assert.equal(callback.pathname, '/oauth/callback');
      callback.searchParams.set('state', authorization.searchParams.get('state'));
      callback.searchParams.set('iss', origin);
      callback.searchParams.set('code', 'test-code');
      await fetch(callback);
    },
    hold() { let release; holdRefresh = new Promise((resolve) => { release = resolve; }); return () => { holdRefresh = null; release(); }; },
    async close() { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); },
  };
}

test('mock OAuth session uses public S256 flow, durable rotation, shared signout and late-401 matching', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'akb-oauth-session-'));
  const issuer = await mockIssuer();
  const options = { storeOptions: { directory }, browserOpener: issuer.browserOpener };
  const first = new OAuthSession(issuer.installation, options);
  const second = new OAuthSession(issuer.installation, options);
  let store;
  try {
    first.start();
    second.start();
    await until(() => first.state.authorizationUrl || second.state.authorizationUrl);
    await issuer.finish();
    const original = await until(() => first.currentCredential());
    await until(() => second.currentCredential());
    assert.deepEqual(issuer.exchanges, ['authorization_code']);
    assert.equal(first.status().authentication, 'ready');
    store = new OAuthStore(issuer.installation.resource, { directory });
    await until(() => store.acquire());
    store.save({ ...store.read(), expiresAt: Date.now() - 1 });
    store.release();
    const release = issuer.hold();
    await until(() => issuer.exchanges.length === 2);
    assert.equal(first.currentCredential(), null);
    assert.equal(second.currentCredential(), null);
    const intent = store.read();
    assert.ok(intent.refreshIntent);
    assert.equal(store.acquire(), false);
    release();
    const refreshed = await until(() => first.currentCredential());
    assert.equal(refreshed.generation, original.generation + 1);
    await until(() => second.currentCredential()?.generation === refreshed.generation);
    first.rejectCredential(original);
    assert.equal(second.currentCredential().generation, refreshed.generation);
    assert.deepEqual(issuer.exchanges, ['authorization_code', 'refresh_token']);
    await until(() => !first.task && !second.task);
    first.signOut();
    assert.equal(second.currentCredential(), null);
    assert.equal(second.status().authentication, 'signed_out');
  } finally { await first.close(); await second.close(); store?.close(); await issuer.close(); await rm(directory, { recursive: true, force: true }); }
});

test('callback ignores malformed unsolicited requests and consumes one valid state', async () => {
  const callback = await createCallback({ state: 'correct-state', issuer: 'https://issuer.example', issuerRequired: true, timeoutMs: 2000 });
  try {
    const url = new URL(callback.redirectUri);
    url.search = '?state=wrong&code=bad&iss=https%3A%2F%2Fissuer.example';
    assert.equal((await fetch(url)).status, 400);
    url.search = '?state=correct-state&state=correct-state&code=bad&iss=https%3A%2F%2Fissuer.example';
    assert.equal((await fetch(url)).status, 400);
    url.search = '?state=correct-state&code=good&iss=https%3A%2F%2Fother.example';
    assert.equal((await fetch(url)).status, 400);
    url.search = '?state=correct-state&code=good&iss=https%3A%2F%2Fissuer.example';
    assert.equal((await fetch(url)).status, 200);
    assert.deepEqual(await callback.result, { code: 'good', iss: 'https://issuer.example' });
    await assert.rejects(fetch(url));
  } finally { callback.close(); }
});

test('durable intent survives owner death and a suspended owner has no timeout takeover', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'akb-oauth-crash-'));
  const resource = 'https://resource.example/mcp';
  const moduleUrl = new URL('../lib/oauth-store.mjs', import.meta.url).href;
  const script = `import { OAuthStore } from ${JSON.stringify(moduleUrl)}; const store = new OAuthStore(${JSON.stringify(resource)}, { directory: process.env.TEST_AUTH_DIR }); if (!store.acquire()) process.exit(2); store.save({binding:{resource:${JSON.stringify(resource)},issuer:'https://issuer.example',clientId:'native'},grantId:'grant',generation:1,tokens:{access_token:'old-access',refresh_token:'old-refresh'},expiresAt:Date.now()+3600000,refreshIntent:{grantId:'grant',generation:1}}); process.stdout.write('owned\\n'); setInterval(()=>{ if (!store.owned) process.exit(3); },1000);`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', script], { env: { ...process.env, TEST_AUTH_DIR: directory }, stdio: ['ignore', 'pipe', 'pipe'] });
  child.stderr.resume();
  let store;
  try {
    await new Promise((resolve, reject) => { child.stdout.once('data', resolve); child.once('exit', (code) => reject(new Error(`Owner exited early ${code}`))); });
    store = new OAuthStore(resource, { directory });
    child.kill('SIGSTOP');
    assert.equal(store.acquire(), false);
    await pause(100);
    assert.equal(store.acquire(), false);
    child.kill('SIGKILL');
    await new Promise((resolve) => child.once('exit', resolve));
    assert.equal(store.acquire(), true);
    assert.ok(store.read().refreshIntent);
    assert.equal(store.read().tokens.refresh_token, 'old-refresh');
    store.release();
    const session = new OAuthSession({ resource, baseUrl: 'https://resource.example' }, { storeOptions: { directory } });
    session.store = store;
    session.binding = { resource, issuer: 'https://issuer.example', clientId: 'native' };
    assert.equal(session.currentCredential(), null);
    assert.equal(session.status().authentication, 'reauthorization_required');
  } finally { child.kill('SIGCONT'); child.kill('SIGKILL'); store?.close(); await rm(directory, { recursive: true, force: true }); }
});

test('unsafe storage modes and symlinks fail explicitly', async () => {
  const root = await mkdtemp(join(tmpdir(), 'akb-oauth-private-'));
  try {
    await chmod(root, 0o755);
    assert.throws(() => new OAuthStore('https://example.com/mcp', { directory: root }), /private/);
    await chmod(root, 0o700);
    await symlink(root, join(root, 'linked'));
    assert.throws(() => new OAuthStore('https://example.com/mcp', { directory: join(root, 'linked') }), /symlinks/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('a post-rename directory-sync failure never publishes a rotated pair to a peer', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'akb-oauth-publication-'));
  const issuer = await mockIssuer();
  let failDirectory = false;
  const { fsyncSync } = await import('node:fs');
  const first = new OAuthSession(issuer.installation, {
    storeOptions: { directory, syncDirectory: (fd) => { if (failDirectory) throw Object.assign(new Error('injected directory sync failure'), { code: 'EIO' }); fsyncSync(fd); } },
    browserOpener: issuer.browserOpener,
    fetchFn: async (url, options) => {
      const response = await fetch(url, { ...options, redirect: 'error' });
      if (options?.body?.get('grant_type') === 'refresh_token') failDirectory = true;
      return response;
    },
  });
  const peer = new OAuthSession(issuer.installation, { storeOptions: { directory }, browserOpener: issuer.browserOpener });
  let store;
  try {
    first.start();
    await issuer.finish();
    await until(() => first.currentCredential());
    peer.start();
    await until(() => peer.currentCredential());
    store = new OAuthStore(issuer.installation.resource, { directory });
    await until(() => store.acquire());
    store.save({ ...store.read(), expiresAt: Date.now() - 1 });
    store.release();
    await until(() => first.status().authentication === 'storage_error');
    assert.equal(store.read().tokens.access_token, 'access-2', 'fault occurs after the replacement file is renamed');
    assert.equal(store.isPublished(store.read()), false);
    assert.equal(peer.currentCredential(), null);
    await pause(600);
    assert.equal(peer.currentCredential(), null);
    assert.equal(peer.status().authentication, 'reauthorization_required');
    assert.deepEqual(issuer.exchanges, ['authorization_code', 'refresh_token']);
  } finally { await first.close(); await peer.close(); store?.close(); await issuer.close(); await rm(directory, { recursive: true, force: true }); }
});

for (const action of ['cancel', 'timeout', 'deny']) {
  test(`authorization ${action} closes the callback and requires explicit recovery`, async () => {
    const directory = await mkdtemp(join(tmpdir(), 'akb-oauth-recovery-'));
    const issuer = await mockIssuer();
    let openings = 0;
    const session = new OAuthSession(issuer.installation, { storeOptions: { directory }, callbackTimeoutMs: action === 'timeout' ? 100 : 3000, browserOpener: async (url) => { openings++; return issuer.browserOpener(url); } });
    try {
      session.start();
      await until(() => issuer.authorization());
      const authorization = issuer.authorization();
      const callback = new URL(authorization.searchParams.get('redirect_uri'));
      if (action === 'cancel') session.cancel();
      if (action === 'deny') {
        callback.searchParams.set('state', authorization.searchParams.get('state'));
        callback.searchParams.set('iss', new URL(issuer.installation.resource).origin);
        callback.searchParams.set('error', 'access_denied');
        await fetch(callback);
      }
      await until(() => !session.task);
      assert.equal(session.currentCredential(), null);
      assert.equal(session.status().authentication, action === 'deny' ? 'denied' : action === 'timeout' ? 'timeout' : 'cancelled');
      await pause(600);
      assert.equal(openings, 1);
      assert.deepEqual(issuer.exchanges, []);
      await assert.rejects(fetch(callback));
    } finally { await session.close(); await issuer.close(); await rm(directory, { recursive: true, force: true }); }
  });
}

test('a current 401 during external ownership stays unusable and a replacement grant survives a late 401', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'akb-oauth-late-401-'));
  const issuer = await mockIssuer();
  const session = new OAuthSession(issuer.installation, { storeOptions: { directory }, browserOpener: issuer.browserOpener });
  let store;
  try {
    session.start(); await issuer.finish();
    const original = await until(() => session.currentCredential());
    store = new OAuthStore(issuer.installation.resource, { directory });
    await until(() => store.acquire());
    session.rejectCredential(original);
    assert.equal(session.currentCredential(), null);
    const replacement = { ...store.read(), grantId: 'replacement-grant', generation: original.generation, publicationId: 'replacement-publication' };
    store.save(replacement); store.publish(replacement.publicationId);
    const current = await until(() => session.currentCredential());
    assert.equal(current.grantId, 'replacement-grant');
    session.rejectCredential(original);
    assert.equal(session.currentCredential().grantId, 'replacement-grant');
    await until(() => !session.task);
    assert.equal(session.currentCredential().grantId, 'replacement-grant');
    assert.deepEqual(issuer.exchanges, ['authorization_code']);
  } finally { await session.close(); store?.close(); await issuer.close(); await rm(directory, { recursive: true, force: true }); }
});


test('restart discovery outage preserves the grant, reports connectivity, and recovers without another browser', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'akb-oauth-discovery-outage-'));
  const issuer = await mockIssuer();
  const options = { storeOptions: { directory }, browserOpener: issuer.browserOpener };
  const first = new OAuthSession(issuer.installation, options);
  let restarted;
  let proxy;
  try {
    first.start(); await issuer.finish();
    const original = await until(() => first.currentCredential());
    await first.close();
    issuer.outage(true);
    restarted = new OAuthSession(issuer.installation, options);
    restarted.start();
    await until(() => restarted.status().discovery === 'unavailable');
    proxy = new AKBProxy({ installation: issuer.installation, authentication: { kind: 'oauth' }, credentialOwner: restarted });
    const response = await proxy._handle({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'akb_connection', arguments: {} } });
    const status = JSON.parse(response.result.content[0].text);
    assert.equal(status.authentication, 'pending');
    assert.equal(status.connectivity, 'unreachable');
    assert.equal(status.backendConnectivity, 'unknown');
    assert.equal(status.discoveryConnectivity, 'unreachable');
    assert.equal(issuer.openings(), 1);
    assert.deepEqual(issuer.exchanges, ['authorization_code']);
    issuer.outage(false);
    const recovered = await until(() => restarted.currentCredential());
    assert.equal(recovered.grantId, original.grantId);
    assert.equal(recovered.generation, original.generation);
    assert.equal(issuer.openings(), 1);
    assert.deepEqual(issuer.exchanges, ['authorization_code']);
  } finally { await first.close(); await restarted?.close(); proxy?.connection.close(); await issuer.close(); await rm(directory, { recursive: true, force: true }); }
});

test('absolute-form and network-path callback targets never consume the valid attempt', async () => {
  const callback = await createCallback({ state: 'correct-state', issuer: 'https://issuer.example', issuerRequired: true, timeoutMs: 3000 });
  const origin = new URL(callback.redirectUri);
  const query = '?state=correct-state&code=good&iss=https%3A%2F%2Fissuer.example';
  const submit = (path) => new Promise((resolve, reject) => {
    const request = httpRequest({ hostname: origin.hostname, port: origin.port, path, headers: { Host: origin.host } }, (response) => { response.resume(); response.once('end', () => resolve(response.statusCode)); });
    request.on('error', reject); request.end();
  });
  try {
    assert.equal(await submit('http://foreign.example/oauth/callback' + query), 400);
    assert.equal(await submit('//foreign.example/oauth/callback' + query), 400);
    assert.equal(await submit('/oauth/callback' + query), 200);
    assert.deepEqual(await callback.result, { code: 'good', iss: 'https://issuer.example' });
  } finally { callback.close(); }
});


test('a pre-registered public client works when Keycloak metadata omits the none auth method', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'akb-oauth-public-client-'));
  const issuer = await mockIssuer({ advertisedAuthMethods: ['private_key_jwt', 'client_secret_basic', 'client_secret_post', 'tls_client_auth', 'client_secret_jwt'] });
  const session = new OAuthSession(issuer.installation, { storeOptions: { directory }, browserOpener: issuer.browserOpener });
  let store;
  try {
    session.start(); await issuer.finish();
    const original = await until(() => session.currentCredential());
    store = new OAuthStore(issuer.installation.resource, { directory });
    await until(() => store.acquire());
    store.save({ ...store.read(), expiresAt: Date.now() - 1 }); store.release();
    const rotated = await until(() => {
      const credential = session.currentCredential();
      return credential?.generation === original.generation + 1 ? credential : null;
    });
    assert.equal(rotated.grantId, original.grantId);
    assert.deepEqual(issuer.exchanges, ['authorization_code', 'refresh_token']);
    assert.equal(issuer.openings(), 1);
  } finally { await session.close(); store?.close(); await issuer.close(); await rm(directory, { recursive: true, force: true }); }
});

for (const changed of ['binding', 'missing']) {
  test(`a shared ${changed} record never leaves status ready without a usable credential`, async () => {
    const directory = await mkdtemp(join(tmpdir(), 'akb-oauth-shared-record-'));
    const issuer = await mockIssuer();
    const session = new OAuthSession(issuer.installation, { storeOptions: { directory }, browserOpener: issuer.browserOpener });
    let peer;
    try {
      session.start(); await issuer.finish();
      await until(() => session.currentCredential());
      assert.equal(session.status().authentication, 'ready');
      peer = new OAuthStore(issuer.installation.resource, { directory });
      await until(() => peer.acquire());
      if (changed === 'binding') {
        peer.save({ ...peer.read(), binding: { resource: issuer.installation.resource, issuer: 'https://another-issuer.example', clientId: 'another-native' }, grantId: 'replacement-grant', publicationId: 'replacement-publication' });
        peer.publish('replacement-publication');
      } else { await rm(peer.recordPath); peer.release(); }
      assert.equal(session.currentCredential(), null);
      assert.equal(session.status().authentication, 'reauthorization_required');
      assert.match(session.status().message, /sign_in/);
      await pause(600);
      assert.equal(issuer.openings(), 1);
      assert.deepEqual(issuer.exchanges, ['authorization_code']);
    } finally { await session.close(); peer?.close(); await issuer.close(); await rm(directory, { recursive: true, force: true }); }
  });
}
