import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { chmod, mkdtemp, rm } from 'node:fs/promises';
import { createServer, get } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fsyncSync } from 'node:fs';
import { OAuthSession } from '../lib/oauth-session.mjs';

const scopes = ['akb:vault:read', 'akb:vault:write'];
const initialTime = 1700000000000;
const reply = (res, status, body) => {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
};

async function readRequest(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks).toString();
}

async function createInstallation(t) {
  const directory = await mkdtemp(join(tmpdir(), 'akb-oauth-session-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const events = [];
  const registrations = new Map();
  let now = initialTime;
  let authorization;
  let version = 0;
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, origin).pathname;
    events.push([req.method, path, req.headers.authorization]);
    if (path === '/.well-known/oauth-protected-resource/prefix/mcp') {
      return reply(res, 200, {
        resource: fixture.advertisedResource ?? resource,
        authorization_servers: [fixture.advertisedIssuer ?? issuer],
        scopes_supported: scopes,
      });
    }
    if (['/.well-known/oauth-authorization-server/realm', '/.well-known/oauth-authorization-server/another-realm'].includes(path)) {
      if (fixture.redirectDiscovery) {
        res.writeHead(302, { Location: fixture.redirectDiscovery });
        res.end();
        return;
      }
      return reply(res, 200, {
        issuer: fixture.serverIssuer ?? fixture.advertisedIssuer ?? issuer,
        authorization_endpoint: `${origin}/authorize`,
        token_endpoint: fixture.tokenEndpoint ?? `${origin}/token`,
        registration_endpoint: fixture.registrationEndpoint ?? `${origin}/register`,
        grant_types_supported: ['authorization_code', 'refresh_token'],
        response_types_supported: ['code'],
        code_challenge_methods_supported: ['S256'],
        scopes_supported: ['openid', 'profile', 'email', ...scopes],
        authorization_response_iss_parameter_supported: true,
      });
    }
    if (req.method === 'POST' && path === '/register') {
      const body = JSON.parse(await readRequest(req));
      const id = `local-client-${registrations.size + 1 + version}`;
      const registration = {
        ...body,
        response_types: ['code', 'none'],
        client_id: id,
        registration_client_uri: `${origin}/register/${id}`,
        registration_access_token: `management-${id}`,
      };
      registrations.set(id, registration);
      return reply(res, 201, registration);
    }
    if (req.method === 'GET' && path.startsWith('/register/')) {
      const registration = registrations.get(path.split('/').at(-1));
      return registration && req.headers.authorization === `Bearer ${registration.registration_access_token}`
        ? reply(res, 200, registration)
        : reply(res, 401, { error: 'invalid_token' });
    }
    if (req.method === 'POST' && path === '/token') {
      const body = new URLSearchParams(await readRequest(req));
      if (req.headers.authorization || body.get('resource') !== resource || !registrations.has(body.get('client_id'))) {
        return reply(res, 400, { error: 'invalid_request' });
      }
      if (body.get('grant_type') === 'authorization_code') {
        const challenge = createHash('sha256').update(body.get('code_verifier') ?? '').digest('base64url');
        if (body.get('code') !== 'returned-code' || authorization?.get('code_challenge') !== challenge
          || body.get('redirect_uri') !== authorization.get('redirect_uri')) return reply(res, 400, { error: 'invalid_grant' });
      } else if (body.get('grant_type') === 'refresh_token') {
        if (fixture.refreshResponse && await fixture.refreshResponse(res, body)) return;
        if (body.get('refresh_token') !== `refresh-${version}`) return reply(res, 400, { error: 'invalid_grant' });
      } else {
        return reply(res, 400, { error: 'unsupported_grant_type' });
      }
      version++;
      return reply(res, 200, { access_token: `access-${version}`, refresh_token: `refresh-${version}`, token_type: 'Bearer', expires_in: 60 });
    }
    reply(res, 404, { error: 'not_found' });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => { server.close(); server.closeAllConnections(); });
  const origin = `http://127.0.0.1:${server.address().port}`;
  const resource = `${origin}/prefix/mcp`;
  const issuer = `${origin}/realm`;
  const installation = { resource, endpointUrl: new URL(resource), baseUrl: new URL(`${origin}/prefix`) };
  const callbackFor = (url) => {
    authorization = new URL(url).searchParams;
    const callback = new URL(authorization.get('redirect_uri'));
    callback.searchParams.set('iss', fixture.advertisedIssuer ?? issuer);
    callback.searchParams.set('state', authorization.get('state'));
    callback.searchParams.set('code', 'returned-code');
    return callback;
  };
  const defaultBrowser = async (url) => {
    const callback = callbackFor(url);
    assert.equal(authorization.get('code_challenge_method'), 'S256');
    assert.equal(authorization.get('resource'), resource);
    assert.equal(authorization.get('scope'), 'openid profile email akb:vault:read akb:vault:write');
    assert.equal(callback.hostname, '127.0.0.1');
    assert.equal(callback.pathname, '/oauth/callback');
    assert.ok(Number(callback.port) > 0);
    assert.equal((await fetch(callback)).status, 200);
    return true;
  };
  const fixture = {
    directory, resource, issuer, origin, registrations, events, callbackFor,
    advance: () => { now += 60000; },
    session(options = {}) {
      const session = new OAuthSession(installation, {
        openBrowser: defaultBrowser,
        now: () => now,
        ...options,
        storeOptions: { directory, ...options.storeOptions },
      });
      t.after(() => session.close());
      return session;
    },
  };
  return fixture;
}

function count(events, method, path) {
  return events.filter((event) => event[0] === method && event[1] === path).length;
}

test('login persists a public registration and logout clears the grant for all local sessions', async (t) => {
  const fixture = await createInstallation(t);
  const session = fixture.session();
  await assert.rejects(session.credential(), { code: 'login_required' });
  assert.deepEqual(fixture.events, []);
  assert.equal(session.status().state, 'login_required');

  const state = await session.login();
  assert.equal(state.mode, 'oauth');
  assert.equal(state.state, 'ready');
  assert.equal(state.issuer, fixture.issuer);
  const registration = fixture.registrations.get(state.clientId);
  assert.equal(registration.token_endpoint_auth_method, 'none');
  assert.equal(registration.application_type, 'native');
  assert.deepEqual(registration.grant_types, ['authorization_code', 'refresh_token']);
  assert.deepEqual(registration.redirect_uris, ['http://127.0.0.1/oauth/callback']);
  assert.equal(registration.scope, 'profile email akb:vault:read akb:vault:write');
  assert.doesNotMatch(JSON.stringify(state), /access-|refresh-|management-|returned-code/);

  const peer = fixture.session();
  const prior = await peer.credential();
  assert.equal(prior.accessToken, 'access-1');
  assert.equal(prior.generation, 0);
  assert.equal((await session.logout()).clientId, state.clientId);
  await assert.rejects(peer.credential(), { code: 'login_required' });

  assert.equal((await session.login()).clientId, state.clientId);
  const current = await session.credential();
  assert.notEqual(current.grantId, prior.grantId);
  await peer.rejected(prior);
  assert.deepEqual(await peer.credential(), current);
  assert.equal(count(fixture.events, 'POST', '/register'), 1);
  assert.equal(count(fixture.events, 'GET', '/register/local-client-1'), 1);
  assert.equal(count(fixture.events, 'POST', '/token'), 2);
});

test('an issuer that reports the saved DCR client removed allows a fresh registration at explicit login', async (t) => {
  const fixture = await createInstallation(t);
  const session = fixture.session();
  const previous = await session.login();
  fixture.registrations.delete(previous.clientId);
  const current = await session.login();
  assert.notEqual(current.clientId, previous.clientId);
  assert.equal(count(fixture.events, 'POST', '/register'), 2);
  assert.equal(count(fixture.events, 'GET', '/register/local-client-1'), 1);
});

test('a changed issuer gets a new registration without receiving the previous management token or refresh token', async (t) => {
  const fixture = await createInstallation(t);
  const session = fixture.session();
  const previous = await session.login();
  fixture.events.length = 0;
  fixture.advertisedIssuer = `${fixture.origin}/another-realm`;
  const current = await session.login();
  assert.equal(current.issuer, fixture.advertisedIssuer);
  assert.notEqual(current.clientId, previous.clientId);
  assert.equal(count(fixture.events, 'POST', '/register'), 1);
  assert.equal(count(fixture.events, 'POST', '/token'), 1);
  assert.ok(fixture.events.every(([, , bearer]) => bearer === undefined));
});

test('unsolicited callbacks leave the attempt open and manual browser fallback completes the same attempt', async (t) => {
  const fixture = await createInstallation(t);
  let callback;
  const session = fixture.session({
    openBrowser: async (url) => {
      callback = fixture.callbackFor(url);
      const wrongIssuer = new URL(callback);
      wrongIssuer.searchParams.set('iss', `${fixture.origin}/wrong-issuer`);
      const duplicateState = new URL(callback);
      duplicateState.searchParams.append('state', callback.searchParams.get('state'));
      const missingIssuer = new URL(callback);
      missingIssuer.searchParams.delete('iss');
      const wrongPath = new URL(callback);
      wrongPath.pathname += '/';
      for (const invalid of [wrongIssuer, duplicateState, missingIssuer, wrongPath]) {
        assert.equal((await fetch(invalid)).status, 400);
      }
      assert.equal((await fetch(callback, { method: 'POST' })).status, 400);
      const malformedHostStatus = await new Promise((resolve, reject) => {
        get(callback, { headers: { Host: 'localhost' } }, (response) => {
          response.resume();
          resolve(response.statusCode);
        }).on('error', reject);
      });
      assert.equal(malformedHostStatus, 400);
      assert.equal(count(fixture.events, 'POST', '/token'), 0);
      return false;
    },
    onAuthorizationUrl(url) {
      assert.equal(fixture.callbackFor(url).href, callback.href);
      void fetch(callback).then((response) => assert.equal(response.status, 200));
    },
  });
  assert.equal((await session.login()).state, 'ready');
  assert.equal(count(fixture.events, 'POST', '/token'), 1);
});

test('login denial and cancellation close the listener while retaining the durable registration', async (t) => {
  const fixture = await createInstallation(t);
  const denied = fixture.session({
    async openBrowser(url) {
      const callback = fixture.callbackFor(url);
      callback.searchParams.delete('code');
      callback.searchParams.set('error', 'access_denied');
      callback.searchParams.set('error_description', 'private token and callback data');
      const response = await fetch(callback);
      assert.equal(response.status, 200);
      assert.doesNotMatch(await response.text(), /private token/);
      return true;
    },
  });
  await assert.rejects(denied.login(), (error) => error.code === 'oauth_denied' && !error.message.includes('private token'));
  assert.equal(denied.status().clientId, 'local-client-1');

  const controller = new AbortController();
  const listening = Promise.withResolvers();
  const session = fixture.session({
    signal: controller.signal,
    openBrowser: async (url) => { listening.resolve(fixture.callbackFor(url)); return true; },
  });
  const attempt = session.login();
  const callback = await listening.promise;
  await assert.rejects(fixture.session().login(), { code: 'oauth_busy' });
  controller.abort();
  await assert.rejects(attempt, { code: 'oauth_cancelled' });
  await assert.rejects(fetch(callback));
  assert.equal(count(fixture.events, 'POST', '/register'), 1);
  assert.equal(count(fixture.events, 'POST', '/token'), 0);
});

test('a login attempt times out without a callback and releases ownership for another explicit login', async (t) => {
  const fixture = await createInstallation(t);
  let callback;
  const session = fixture.session({
    callbackTimeoutMs: 500,
    openBrowser: async (url) => { callback = fixture.callbackFor(url); return true; },
  });
  await assert.rejects(session.login(), { code: 'oauth_timeout' });
  assert.ok(callback);
  await assert.rejects(fetch(callback));
  assert.equal((await fixture.session().login()).state, 'ready');
  assert.equal(count(fixture.events, 'POST', '/register'), 1);
});

test('discovery rejects altered resource, issuer, redirects and cross-origin AS endpoints without sending registration data', async (t) => {
  const fixture = await createInstallation(t);
  const session = fixture.session();
  fixture.advertisedResource = `${fixture.origin}/mcp`;
  await assert.rejects(session.login(), { code: 'oauth_configuration' });
  fixture.advertisedResource = undefined;
  fixture.serverIssuer = `${fixture.origin}/wrong-issuer`;
  await assert.rejects(session.login(), { code: 'oauth_configuration' });
  fixture.serverIssuer = undefined;
  fixture.tokenEndpoint = 'http://127.0.0.1:1/token';
  await assert.rejects(session.login(), { code: 'oauth_configuration' });
  fixture.tokenEndpoint = undefined;
  fixture.redirectDiscovery = `${fixture.origin}/register`;
  await assert.rejects(session.login(), { code: 'oauth_configuration' });
  assert.equal(count(fixture.events, 'GET', '/register'), 0);
  assert.equal(count(fixture.events, 'POST', '/register'), 0);
});

test('concurrent expired readers adopt one rotated generation and late rejection preserves it', async (t) => {
  const fixture = await createInstallation(t);
  const owner = fixture.session();
  const peer = fixture.session();
  await owner.login();
  const stale = await owner.credential();
  fixture.advance();
  const arrived = Promise.withResolvers();
  const respond = Promise.withResolvers();
  fixture.refreshResponse = async () => { arrived.resolve(); await respond.promise; return false; };
  const first = owner.credential();
  await arrived.promise;
  assert.equal(peer.status().state, 'refresh_pending');
  const second = peer.credential();
  const sameProcess = owner.credential();
  respond.resolve();
  const current = await first;
  assert.equal(current.generation, 1);
  assert.equal(current.grantId, stale.grantId);
  assert.deepEqual(await second, current);
  assert.deepEqual(await sameProcess, current);
  await peer.rejected(stale);
  assert.deepEqual(await owner.credential(), current);
  assert.equal(count(fixture.events, 'POST', '/token'), 2);
  assert.equal(count(fixture.events, 'POST', '/register'), 1);
  await peer.rejected(current);
  await assert.rejects(owner.credential(), { code: 'login_required' });
});

test('uncertain rotation reports transport failure and never reuses the refresh token', async (t) => {
  const fixture = await createInstallation(t);
  const session = fixture.session();
  await session.login();
  fixture.advance();
  fixture.refreshResponse = (res) => { reply(res, 503, { error: 'server_error' }); return true; };
  await assert.rejects(session.credential(), { code: 'oauth_transport' });
  await assert.rejects(fixture.session().credential(), { code: 'login_required' });
  assert.equal(count(fixture.events, 'POST', '/token'), 2);
  assert.equal((await session.login()).state, 'ready');
  assert.equal(count(fixture.events, 'POST', '/register'), 1);
});

test('a suspended refresh owner blocks takeover and death leaves an unresumable durable intent', async (t) => {
  const fixture = await createInstallation(t);
  await fixture.session().login();
  const arrived = Promise.withResolvers();
  const respond = Promise.withResolvers();
  fixture.refreshResponse = async () => { arrived.resolve(); await respond.promise; return false; };
  const moduleUrl = new URL('../lib/oauth-session.mjs', import.meta.url);
  const source = `
    import { OAuthSession } from ${JSON.stringify(moduleUrl.href)};
    const [resource, directory, now] = process.argv.slice(1);
    const session = new OAuthSession({ resource }, { storeOptions: { directory }, now: () => Number(now) });
    await session.credential();
    await session.close();
  `;
  const child = spawn(process.execPath, [
    '--input-type=module', '-e', source, fixture.resource, fixture.directory, String(initialTime + 60000),
  ], { stdio: 'ignore' });
  const childExit = once(child, 'exit');
  t.after(() => { child.kill('SIGKILL'); respond.resolve(); });
  await Promise.race([arrived.promise, childExit.then(() => assert.fail('Refresh owner exited before sending a token request.'))]);
  assert.equal(child.kill('SIGSTOP'), true);
  fixture.advance();
  await assert.rejects(fixture.session({ refreshWaitMs: 75 }).credential(), { code: 'oauth_busy' });
  assert.equal(child.kill('SIGKILL'), true);
  await childExit;
  respond.resolve();
  await assert.rejects(fixture.session().credential(), { code: 'login_required' });
  assert.equal(count(fixture.events, 'POST', '/token'), 2);
});

test('a replacement that fails persistence never becomes usable and unsafe storage is rejected', async (t) => {
  const fixture = await createInstallation(t);
  await fixture.session().login();
  fixture.advance();
  let syncs = 0;
  const session = fixture.session({ storeOptions: { syncDirectory(fd) {
    if (++syncs === 2) throw new Error('Simulated persistence failure.');
    fsyncSync(fd);
  } } });
  await assert.rejects(session.credential(), { code: 'oauth_storage' });
  await assert.rejects(fixture.session().credential(), { code: 'login_required' });
  assert.equal(count(fixture.events, 'POST', '/token'), 2);
  await chmod(fixture.directory, 0o755);
  assert.throws(() => fixture.session(), { code: 'oauth_storage' });
  await chmod(fixture.directory, 0o700);
});
