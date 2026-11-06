import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(new URL('../../../frontend/package.json', import.meta.url));
const { chromium } = require('@playwright/test');
const [urlFile, reportFile] = process.argv.slice(2);
const report = { result: 'FAIL', stage: 'request' };
let browser;
try {
  const authorization = new URL((await readFile(urlFile, 'utf8')).trim().split('\n')[0]);
  const issuer = process.env.AKB_NATIVE_OAUTH_ISSUER;
  const expected = new URL(`${issuer}/protocol/openid-connect/auth`);
  assert.equal(authorization.origin + authorization.pathname, expected.href);
  for (const name of ['client_id', 'response_type', 'redirect_uri', 'state', 'code_challenge', 'code_challenge_method', 'resource']) {
    assert.equal(authorization.searchParams.getAll(name).length, 1);
  }
  assert.equal(authorization.searchParams.get('client_id'), process.env.AKB_NATIVE_OAUTH_CLIENT_ID);
  assert.equal(authorization.searchParams.get('response_type'), 'code');
  assert.equal(authorization.searchParams.get('code_challenge_method'), 'S256');
  assert.match(authorization.searchParams.get('code_challenge'), /^[A-Za-z0-9_-]{43,128}$/);
  assert.equal(authorization.searchParams.get('resource'), process.env.AKB_NATIVE_OAUTH_RESOURCE);
  const scopes = new Set(authorization.searchParams.get('scope').split(' '));
  assert.ok(scopes.has('akb:vault:read') && scopes.has('akb:vault:write') && !scopes.has('offline_access'));
  const callback = new URL(authorization.searchParams.get('redirect_uri'));
  assert.equal(callback.protocol, 'http:');
  assert.equal(callback.hostname, '127.0.0.1');
  assert.ok(Number(callback.port) > 0);
  assert.equal(callback.pathname, '/oauth/callback');
  assert.equal(callback.search + callback.hash + callback.username + callback.password, '');
  const isCallback = (url) => url.origin === callback.origin && url.pathname === callback.pathname;
  report.request = { code: true, s256: true, exact_resource: true, native_client: true, dynamic_loopback_callback: true, no_offline_access: true };
  assert.ok(process.env.AKB_NATIVE_OAUTH_USERNAME && process.env.AKB_E2E_PASSWORD);
  browser = await chromium.launch({ headless: true });
  report.browser_version = browser.version();
  const context = await browser.newContext();
  const page = await context.newPage();
  let callbackStatus;
  page.on('response', (response) => {
    if (isCallback(new URL(response.url()))) callbackStatus = response.status();
  });
  report.stage = 'login';
  const initial = await page.goto(authorization.href, { waitUntil: 'domcontentloaded' });
  assert.equal(initial.status(), 200);
  await page.locator('input[name="username"]').waitFor();
  report.login_form_fields = await page.locator('form input').evaluateAll((inputs) => inputs.map((input) => ({ name: input.name, type: input.type })));
  await page.locator('input[name="username"]').fill(process.env.AKB_NATIVE_OAUTH_USERNAME);
  await page.locator('input[name="password"]').fill(process.env.AKB_E2E_PASSWORD);
  await page.locator('#kc-login').click();
  report.stage = 'consent';
  await Promise.race([
    page.waitForURL(isCallback, { timeout: 30000 }),
    page.locator('[name="accept"]').waitFor({ state: 'visible', timeout: 30000 }),
  ]);
  report.consent_observed = await page.locator('[name="accept"]').isVisible();
  if (report.consent_observed) await page.locator('[name="accept"]').click();
  report.stage = 'callback';
  await page.waitForURL(isCallback, { timeout: 30000 });
  await page.waitForLoadState('domcontentloaded');
  const returned = new URL(page.url());
  assert.equal(callbackStatus, 200);
  for (const name of ['code', 'state', 'iss']) assert.equal(returned.searchParams.getAll(name).length, 1);
  assert.ok(returned.searchParams.get('code'));
  assert.equal(returned.searchParams.get('state'), authorization.searchParams.get('state'));
  assert.equal(returned.searchParams.get('iss'), issuer);
  report.callback = { proxy_owned: true, status: callbackStatus, code_present: true, state_matches: true, issuer_matches: true };
  report.stage = 'complete';
  report.result = 'PASS';
} catch (error) {
  report.error_type = error.name;
} finally {
  await browser?.close();
  await writeFile(reportFile, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
}
process.stdout.write(JSON.stringify({ result: report.result, stage: report.stage, report: reportFile }) + '\n');
process.exitCode = report.result === 'PASS' ? 0 : 1;
