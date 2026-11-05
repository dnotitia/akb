import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'node:http';
import { ToolConnection } from '../lib/connection.mjs';
import { createCredentialOwner } from '../lib/credentials.mjs';
import { parseConfiguration } from '../lib/config.mjs';
import { startAuthorization } from '@modelcontextprotocol/client';

for (const outcome of [500, 502, 'abort']) {
  test(`a dispatched control write with ${outcome} has an unknown outcome and is submitted once`, async () => {
    let writes = 0;
    const server = createServer(async (request, response) => {
      for await (const _chunk of request) {}
      writes++;
      if (outcome === 'abort') {
        response.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': '1000' });
        response.write('{"secret":"upstream-private-marker');
        setImmediate(() => response.destroy(new Error('upstream-private-marker')));
      } else response.writeHead(outcome).end('upstream-private-marker');
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const configuration = parseConfiguration(['--url', `http://127.0.0.1:${server.address().port}/prefix/mcp`, '--pat', 'selected-pat'], {});
    const connection = new ToolConnection(configuration.installation, createCredentialOwner(configuration.authentication, configuration.installation));
    try {
      await assert.rejects(connection.control('POST', '/api/v1/files/example/upload', null, {}, { timeoutMs: 1000 }), (error) => {
        assert.equal(error.execution, 'unknown');
        assert.doesNotMatch(error.message, /upstream-private-marker|selected-pat/);
        return true;
      });
      assert.equal(writes, 1);
      assert.equal(connection.connectivity, outcome === 'abort' ? 'unreachable' : 'reachable');
    } finally {
      connection.close();
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    }
  });
}

test('a clean credential rejection is not started and never retries the write', async () => {
  let writes = 0;
  const server = createServer((_request, response) => { writes++; response.writeHead(401).end('upstream-private-marker'); });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const configuration = parseConfiguration(['--url', `http://127.0.0.1:${server.address().port}/mcp`, '--pat', 'selected-pat'], {});
  const owner = createCredentialOwner(configuration.authentication, configuration.installation);
  const connection = new ToolConnection(configuration.installation, owner);
  try {
    await assert.rejects(connection.control('POST', '/api/v1/files/example/upload'), (error) => error.execution === 'not_started' && error.code === 'authentication_rejected');
    assert.equal(writes, 1);
    assert.equal(owner.status().authentication, 'rejected');
    await assert.rejects(connection.control('POST', '/api/v1/files/example/upload'), (error) => error.execution === 'not_started');
    assert.equal(writes, 1);
  } finally { connection.close(); server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); }
});

for (const suffix of ['/mcp', '/mcp/']) {
  test(`MCP input ${suffix} targets the direct mounted endpoint and keeps one canonical resource`, async () => {
    const paths = [];
    const server = createServer((request, response) => {
      paths.push(request.url);
      if (request.url !== '/prefix/mcp/') return response.writeHead(307, { Location: '/prefix/mcp/' }).end();
      response.writeHead(200).end('{"result":{}}');
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    const base = `http://127.0.0.1:${server.address().port}/prefix`;
    const configuration = parseConfiguration(['--url', base + suffix, '--pat', 'selected-pat'], {});
    const connection = new ToolConnection(configuration.installation, createCredentialOwner(configuration.authentication, configuration.installation));
    try {
      assert.equal((await connection.mcp('{}')).statusCode, 200);
      assert.deepEqual(paths, ['/prefix/mcp/']);
      assert.equal(configuration.installation.resource, base + '/mcp');
      const authorization = await startAuthorization('https://issuer.example', {
        metadata: { authorization_endpoint: 'https://issuer.example/authorize', response_types_supported: ['code'], code_challenge_methods_supported: ['S256'] },
        clientInformation: { client_id: 'native-test' }, redirectUrl: 'http://127.0.0.1:1234/oauth/callback',
        resource: new URL(configuration.installation.resource), state: 'test-state',
      });
      assert.equal(authorization.authorizationUrl.searchParams.get('resource'), base + '/mcp');
    } finally { connection.close(); server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); }
  });
}

test('an MCP redirect is refused without a second transport request', async () => {
  const paths = [];
  const server = createServer((request, response) => { paths.push(request.url); response.writeHead(307, { Location: '/another/mcp/' }).end(); });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const configuration = parseConfiguration(['--url', `http://127.0.0.1:${server.address().port}/prefix/mcp/`, '--pat', 'selected-pat'], {});
  const connection = new ToolConnection(configuration.installation, createCredentialOwner(configuration.authentication, configuration.installation));
  try {
    await assert.rejects(connection.mcp('{}'), (error) => error.statusCode === 307 && /redirects are refused/.test(error.message));
    assert.deepEqual(paths, ['/prefix/mcp/']);
  } finally { connection.close(); server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); }
});
