import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const { chromium, expect } = await import(pathToFileURL(resolve('frontend/node_modules/@playwright/test/index.mjs')).href);
import { writeFile } from 'node:fs/promises';
const browser = await chromium.launch({ headless: true });
const evidence = [];
async function fixture(options = {}) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  const state = { summaryError: false };
  await page.addInitScript(() => localStorage.setItem('akb_token', 'isolated-loading-audit'));
  await page.route('**/health/**', r => r.fulfill({ json: {} }));
  await page.route('**/api/v1/**', async r => {
    const p = new URL(r.request().url()).pathname;
    let json = { items: [], history: [], relations: [], vaults: [], changes: [], subscribed: false, unread_count: 0 };
    if (p.endsWith('/auth/config')) json = { schema_version: 2, auth_mode: 'local', local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
    else if (p.endsWith('/auth/me')) json = { user_id: 'loading-audit', username: 'fixture', display_name: 'Audit fixture', email: 'audit@example.invalid', auth_method: 'local', is_admin: false };
    else if (p.endsWith('/my/workspace-summary')) {
      if (state.summaryError) return r.fulfill({ status: 503, json: { detail: 'Fixture outage' } });
      json = { version: 1, scope: 'accessible', observed_at: new Date().toISOString(), vault_count: 1, document_count: 36, table_count: 2, file_count: 7 };
    } else if (p.endsWith('/vaults/templates')) {
      if (options.templatesGate) await options.templatesGate;
      json = [{ name: 'team', description: 'Team starter' }];
    } else if (p.endsWith('/vaults')) json = { vaults: [{ id: 'fixture-vault', name: 'fixture', role: 'owner' }] };
    else if (p.endsWith('/info')) {
      if (options.infoGate) await options.infoGate;
      json = { name: 'fixture', role: 'owner', public_access: 'none', description: 'Fixture vault', document_count: 36, table_count: 2, file_count: 7, member_count: 1, collection_count: 1, is_archived: false, is_external_git: false };
    } else if (p.includes('/activity/')) {
      if (options.activityError) return r.fulfill({ status: 503, json: { detail: 'Fixture outage' } });
      json = { total: 0, activity: [] };
    } else if (p.includes('/documents/fixture/')) json = { title: 'Guide', path: 'overview/vault-skill.md', content: '# Guide\n\nFixture content.' };
    else if (p.includes('/browse/')) json = { items: [] };
    else if (p.endsWith('/help/skill-template')) return r.fulfill({ contentType: 'text/plain', body: '# {vault} Guide' });
    return r.fulfill({ json });
  });
  return { page, context, state };
}
try {
  let release;
  const a = await fixture({ infoGate: new Promise(r => { release = r; }) });
  await a.page.goto('http://127.0.0.1:4191/vault/fixture');
  await expect(a.page.getByText('Loading vault overview…', { exact: true })).toBeVisible();
  const overviewVisible = await a.page.getByRole('region', { name: 'fixture Vault overview', exact: true }).isVisible();
  evidence.push({ case: 'overview-info-pending', overviewVisible, mainText: await a.page.locator('#main').innerText() });
  await a.page.screenshot({ path: '/tmp/akb-loading-overview.png' });
  release(); await a.context.close();

  const b = await fixture();
  await b.page.goto('http://127.0.0.1:4191/');
  const summary = b.page.getByRole('region', { name: 'Workspace summary' });
  await expect(summary).toContainText('36');
  const before = await summary.innerText();
  b.state.summaryError = true;
  await b.page.evaluate(() => window.dispatchEvent(new CustomEvent('akb:inventory-changed', { detail: { vault: 'fixture' } })));
  await expect(summary).toContainText('Totals unavailable');
  evidence.push({ case: 'home-summary-refetch-503', before, after: await summary.innerText() });
  await b.page.screenshot({ path: '/tmp/akb-loading-home-error.png' });
  await b.context.close();

  const c = await fixture({ activityError: true });
  await c.page.goto('http://127.0.0.1:4191/vault/fixture/activity');
  await expect(c.page.getByRole('heading', { name: 'Failed to load' })).toBeVisible();
  evidence.push({ case: 'activity-initial-503', alert: await c.page.getByRole('heading', { name: 'Failed to load' }).innerText(), busy: await c.page.locator('#main [aria-busy="true"]').count() });
  await c.context.close();

  let releaseTemplates;
  const d = await fixture({ templatesGate: new Promise(r => { releaseTemplates = r; }) });
  await d.page.goto('http://127.0.0.1:4191/vault/new');
  await expect(d.page.getByRole('button', { name: 'Vault template' })).toBeVisible();
  evidence.push({ case: 'vault-templates-pending', selects: await d.page.getByRole('button', { name: 'Vault template' }).evaluateAll(es => es.map(e => ({ text: e.textContent, disabled: e.disabled, busy: e.getAttribute('aria-busy') }))) });
  await d.page.screenshot({ path: '/tmp/akb-loading-template.png' });
  releaseTemplates(); await d.context.close();
  await writeFile('/tmp/akb-loading-audit-evidence.json', JSON.stringify(evidence, null, 2));
  console.log(JSON.stringify(evidence, null, 2));
} finally { await browser.close(); }
