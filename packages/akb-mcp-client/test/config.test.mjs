import assert from 'node:assert/strict';
import test from 'node:test';
import { parseConfiguration, validateEndpoint } from '../lib/config.mjs';
import { createCredentialOwner } from '../lib/credentials.mjs';

test('URL-only OAuth and active PAT inputs select one consistent method', () => {
  const url = 'https://example.com/prefix/mcp/';
  assert.deepEqual(parseConfiguration(['--url', url], {}), { installation: { resource: 'https://example.com/prefix/mcp', baseUrl: 'https://example.com/prefix' }, authentication: { kind: 'oauth' } });
  assert.equal(parseConfiguration(['--url', url, '--pat', 'test'], {}).authentication.kind, 'pat');
  assert.equal(parseConfiguration([], { AKB_MCP_URL: url, AKB_PAT: 'test' }).authentication.kind, 'pat');
  for (const args of [['--url', url, '--auth-mode', 'oauth', '--pat', 'test'], ['--url', url, '--auth-mode', 'pat'], ['--url', url, '--insecure'], ['--url'], ['--url', url, '--url', url], ['--url', url, '--auth-mode', 'other']]) assert.throws(() => parseConfiguration(args, {}));
  assert.throws(() => parseConfiguration(['--url', url, '--auth-mode', 'oauth'], { AKB_PAT: 'test' }));
});

test('endpoint validation rejects unsafe production URLs and preserves prefixes', () => {
  for (const url of ['http://example.com/mcp', 'https://name:secret@example.com/mcp', 'https://example.com/mcp?q=x', 'https://example.com/mcp#x', 'file:///mcp', 'https://example.com/api']) assert.throws(() => validateEndpoint(url));
  for (const host of ['localhost', '127.0.0.1', '[::1]']) assert.equal(validateEndpoint(`http://${host}:1234/prefix/mcp`).baseUrl, `http://${host}:1234/prefix`);
});

test('PAT actions never start OAuth or require an OAuth store', () => {
  const owner = createCredentialOwner({ kind: 'pat', pat: 'selected' }, validateEndpoint('https://example.com/mcp'));
  owner.start();
  assert.equal(owner.status().mode, 'pat');
  const snapshot = owner.currentCredential();
  assert.equal(snapshot.accessToken, 'selected');
  for (const action of ['signIn', 'cancel', 'signOut']) assert.equal(owner[action]().code, 'inapplicable_action');
  owner.rejectCredential(snapshot);
  assert.equal(owner.currentCredential(), null);
  assert.equal(owner.status().authentication, 'rejected');
  owner.close();
});
