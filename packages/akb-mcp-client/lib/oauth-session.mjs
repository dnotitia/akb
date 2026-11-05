import { randomBytes, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { discoverOAuthProtectedResourceMetadata, discoverAuthorizationServerMetadata, startAuthorization, exchangeAuthorization, refreshAuthorization } from '@modelcontextprotocol/client';
import { OAuthStore, sameBinding } from './oauth-store.mjs';
import { createCallback } from './oauth-callback.mjs';

function secureUrl(value) {
  const url = new URL(value);
  if (url.username || url.password || url.hash || url.search || (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))) throw new Error('OAuth metadata contains an unsafe endpoint.');
  return String(value);
}

async function boundedFetch(input, options = {}, signal) {
  secureUrl(String(input));
  const combined = AbortSignal.any([AbortSignal.timeout(15000), ...(signal ? [signal] : [])]);
  const response = await fetch(input, { ...options, redirect: 'error', signal: combined });
  const reader = response.body?.getReader();
  const chunks = [];
  let size = 0;
  if (reader) {
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 1024 * 1024) throw new Error('OAuth response exceeds the size limit.');
        chunks.push(value);
      }
    } finally { await reader.cancel().catch(() => {}); }
  }
  return new Response(Buffer.concat(chunks), { status: response.status, headers: response.headers });
}

function openBrowser(url, signal) {
  return new Promise((resolve) => {
    const command = process.platform === 'darwin' ? 'open' : 'xdg-open';
    const child = spawn(command, [url], { stdio: 'ignore', signal });
    const timer = setTimeout(() => { child.kill(); resolve(false); }, 5000);
    child.once('error', () => { clearTimeout(timer); resolve(false); });
    child.once('exit', (code) => { clearTimeout(timer); resolve(code === 0); });
  });
}

function usable(record) {
  const lifetime = record?.tokens?.expires_in * 1000;
  return Number.isFinite(lifetime) && lifetime > 0 && record.expiresAt > Date.now() + Math.min(30000, lifetime / 10);
}

export class OAuthSession {
  constructor(installation, { storeOptions, fetchFn = boundedFetch, browserOpener = openBrowser, callbackTimeoutMs } = {}) {
    this.installation = installation;
    this.options = { storeOptions, fetchFn, browserOpener, callbackTimeoutMs };
    this.state = { authentication: 'pending' };
    this.binding = null;
    this.store = null;
    this.task = null;
    this.closed = false;
    this.controller = null;
    this.timer = null;
    this.failure = null;
    this.rejection = null;
    this.connectivity = 'unknown';
  }
  status() {
    this.currentCredential();
    return { mode: 'oauth', ...this.state, connectivity: this.connectivity };
  }
  currentCredential() {
    if (!this.store || !this.binding || this.failure) return null;
    try {
      const record = this.store.read();
      if (!record || !sameBinding(record.binding, this.binding)) return null;
      if (record.refreshIntent || (!record.signedOut && !record.recovery && !this.store.isPublished(record))) {
        if (!this.task) this.state = { authentication: 'reauthorization_required', message: 'A token exchange was interrupted. Use sign_in for a fresh authorization.' };
        return null;
      }
      if (this.rejection && record.grantId === this.rejection.grantId && record.generation === this.rejection.generation) {
        this.state = { authentication: 'rejected', message: 'The grant was rejected. Use sign_in for a fresh authorization.' };
        return null;
      }
      if (record.signedOut || record.recovery) {
        if (!this.task) this.state = { authentication: record.signedOut ? 'signed_out' : record.recovery };
        return null;
      }
      if (!record.tokens || !record.grantId || !Number.isSafeInteger(record.generation) || !Number.isFinite(record.expiresAt)) throw new Error('Invalid OAuth grant.');
      if (!usable(record)) { if (!this.task) this.state = { authentication: 'refresh_pending' }; return null; }
      this.state = { authentication: 'ready' };
      return Object.freeze({ kind: 'oauth', accessToken: record.tokens.access_token, grantId: record.grantId, generation: record.generation });
    } catch (error) {
      if (/busy|locked/i.test(error.message)) this.state = { authentication: 'owner_busy' };
      else this._storageFailure();
      return null;
    }
  }
  _storageFailure() {
    this.failure = true;
    this.state = { authentication: 'storage_error', message: 'OAuth storage is unsafe or unavailable. Repair private local storage, then restart and sign in.' };
  }
  start() {
    if (this.closed || this.timer) return;
    void this._run('automatic');
    this.timer = setInterval(() => { if (!this.closed && !this.task && !this.failure) void this._run('observe'); }, 500);
    this.timer.unref();
  }
  signIn() { if (this.failure) return { code: 'storage_error' }; void this._run('explicit'); return { scheduled: true, ...this.status() }; }
  cancel() {
    if (!this.task) return { cancelled: false, ...this.status() };
    this.controller?.abort();
    return { cancelled: true, ...this.status() };
  }
  signOut() {
    if (this.task) { this.controller?.abort(); return { code: 'owner_busy', message: 'The local attempt is cancelling. Retry sign_out after it stops.' }; }
    if (!this.store || !this.binding) return { code: 'authentication_pending', message: 'Discovery is not ready. Retry sign_out.' };
    if (!this.store.acquire()) return { code: 'owner_busy', message: 'Another proxy owns authorization. Cancel or finish it there, then retry sign_out.' };
    try { this.store.save({ binding: this.binding, signedOut: true }); this.state = { authentication: 'signed_out' }; return this.status(); }
    catch { this._storageFailure(); return this.status(); }
    finally { this.store.release(); }
  }
  rejectCredential(snapshot) {
    this.rejection = { grantId: snapshot.grantId, generation: snapshot.generation };
    this.currentCredential();
    if (!this.task) void this._run('observe');
  }
  _persistRejection(record) {
    if (!this.rejection || !record || record.grantId !== this.rejection.grantId || record.generation !== this.rejection.generation) return false;
    this.store.save({ binding: this.binding, recovery: 'rejected' });
    this.state = { authentication: 'rejected' };
    return true;
  }
  async _discover(signal) {
    const fetchFn = async (url, options) => {
      try {
        const response = await this.options.fetchFn(url, options, signal);
        if (response.status >= 500 || [408, 429].includes(response.status)) throw new Error('Discovery endpoint unavailable.');
        return response;
      } catch (error) {
        if (signal.aborted) throw error;
        throw Object.assign(new Error('OAuth discovery is unavailable. Existing authorization was not rejected. Connectivity will retry.'), { code: 'discovery_unavailable' });
      }
    };
    const resource = await discoverOAuthProtectedResourceMetadata(this.installation.resource, undefined, fetchFn);
    if (resource.resource !== this.installation.resource || resource.authorization_servers?.length !== 1 || typeof resource.akb_native_client_id !== 'string' || !resource.akb_native_client_id.trim()) throw new Error('The installation OAuth resource or native-client metadata is incompatible.');
    const issuer = secureUrl(resource.authorization_servers[0]);
    const metadata = await discoverAuthorizationServerMetadata(issuer, { fetchFn });
    if (!metadata || metadata.issuer !== issuer || !metadata.response_types_supported?.includes('code') || !metadata.grant_types_supported?.includes('authorization_code') || !metadata.grant_types_supported?.includes('refresh_token') || !metadata.code_challenge_methods_supported?.includes('S256')) throw new Error('The authorization server must support a public S256 native client and rotating refresh tokens.');
    secureUrl(metadata.authorization_endpoint);
    secureUrl(metadata.token_endpoint);
    const binding = { resource: this.installation.resource, issuer, clientId: resource.akb_native_client_id };
    if (this.binding && !sameBinding(this.binding, binding)) this.state = { authentication: 'reauthorization_required' };
    this.binding = binding;
    this.metadata = metadata;
    this.scope = ['akb:vault:read', 'akb:vault:write'].filter((scope) => resource.scopes_supported?.includes(scope)).join(' ');
    if (!this.scope) throw new Error('The installation advertises no supported AKB scope.');
    this.connectivity = 'reachable';
  }
  _run(action) {
    if (this.closed || this.task || this.failure || (action === 'observe' && this.state.authentication === 'configuration_error')) return this.task;
    this.controller = new AbortController();
    const signal = this.controller.signal;
    this.task = this._work(action, signal).catch((error) => {
      if (this.failure) return;
      if (error.code === 'discovery_unavailable') {
        this.connectivity = 'unreachable';
        this.state = { authentication: 'pending', discovery: 'unavailable', message: error.message };
        return;
      }
      if (error.code === 'discovery_invalid') {
        this.state = { authentication: 'configuration_error', discovery: 'incompatible', message: 'OAuth discovery metadata is incompatible. Repair the installation settings, then use sign_in.' };
        return;
      }
      const code = signal.aborted ? 'cancelled' : ['denied', 'timeout'].includes(error.code) ? error.code : 'reauthorization_required';
      this.state = { authentication: code, message: code === 'reauthorization_required' ? 'Authorization could not finish safely. Check network and installation settings, then use sign_in.' : error.message };
      if (this.store?.owned && this.binding) {
        try {
          const record = this.store.read();
          if (!record?.refreshIntent) this.store.save({ binding: this.binding, recovery: code });
        } catch { this._storageFailure(); }
      }
    }).finally(() => { this.store?.release(); this.task = null; this.controller = null; });
    return this.task;
  }
  async _work(action, signal) {
    if (!this.store) {
      try { this.store = new OAuthStore(this.installation.resource, this.options.storeOptions); }
      catch (error) {
        if (/busy|locked/i.test(error.message)) { this.state = { authentication: 'owner_busy' }; return; }
        this._storageFailure(); throw error;
      }
    }
    if (!this.binding || action === 'explicit') {
      try { await this._discover(signal); }
      catch (error) {
        if (!error.code && !signal.aborted) error.code = 'discovery_invalid';
        throw error;
      }
    }
    let record = this.store.read();
    if (!this.rejection && sameBinding(record?.binding, this.binding) && this.store.isPublished(record) && !record.refreshIntent && !record.signedOut && !record.recovery && usable(record) && action !== 'explicit') { this.currentCredential(); return; }
    if (action === 'observe' && (!record || !sameBinding(record.binding, this.binding) || record.refreshIntent || !this.store.isPublished(record) || record.signedOut || record.recovery)) { this.currentCredential(); return; }
    if (!this.store.acquire()) { this.state = { authentication: 'owner_busy', message: 'Another proxy owns sign-in or refresh. Local MCP requests remain available.' }; return; }
    record = this.store.read();
    if (action !== 'explicit' && this._persistRejection(record)) return;
    this.rejection = null;
    if (action !== 'explicit' && record) {
      if (!sameBinding(record.binding, this.binding) || record.refreshIntent || !this.store.isPublished(record) || record.signedOut || record.recovery) { this.state = { authentication: record.signedOut ? 'signed_out' : record.recovery ?? 'reauthorization_required' }; return; }
      if (usable(record)) { this.currentCredential(); return; }
      if (record.tokens?.refresh_token) { await this._refresh(record, signal); return; }
      this.state = { authentication: 'reauthorization_required' }; return;
    }
    if (action === 'observe') return;
    await this._authorize(signal);
  }
  _saveTokens(tokens, grantId, generation) {
    if (typeof tokens.access_token !== 'string' || !tokens.access_token || typeof tokens.refresh_token !== 'string' || !tokens.refresh_token || !Number.isFinite(tokens.expires_in) || tokens.expires_in <= 0 || tokens.token_type?.toLowerCase() !== 'bearer') throw new Error('The token response is incompatible with the rotating native session.');
    const publicationId = randomUUID();
    try {
      this.store.save({ binding: this.binding, grantId, generation, tokens, expiresAt: Date.now() + tokens.expires_in * 1000, publicationId });
      this.store.publish(publicationId);
    }
    catch (error) { this._storageFailure(); throw error; }
    this.currentCredential();
  }
  async _refresh(record, signal) {
    this.state = { authentication: 'refreshing' };
    this.store.save({ ...record, refreshIntent: { grantId: record.grantId, generation: record.generation } });
    const tokens = await refreshAuthorization(this.binding.issuer, {
      metadata: this.metadata, clientInformation: { client_id: this.binding.clientId }, refreshToken: record.tokens.refresh_token, resource: new URL(this.binding.resource),
      addClientAuthentication: (_headers, params) => params.set('client_id', this.binding.clientId),
      fetchFn: (url, options) => this.options.fetchFn(url, options, signal),
    });
    if (tokens.refresh_token === record.tokens.refresh_token) throw new Error('The authorization server did not rotate the refresh token.');
    this._saveTokens(tokens, record.grantId, record.generation + 1);
  }
  async _authorize(signal) {
    this.state = { authentication: 'authorizing' };
    const state = randomBytes(32).toString('base64url');
    const callback = await createCallback({ state, issuer: this.binding.issuer, issuerRequired: this.metadata.authorization_response_iss_parameter_supported === true, signal, timeoutMs: this.options.callbackTimeoutMs });
    try {
      const { authorizationUrl, codeVerifier } = await startAuthorization(this.binding.issuer, {
        metadata: this.metadata, clientInformation: { client_id: this.binding.clientId }, redirectUrl: callback.redirectUri,
        scope: this.scope, state, resource: new URL(this.binding.resource),
      });
      this.state = { authentication: 'authorizing', authorizationUrl: authorizationUrl.href };
      const opened = await this.options.browserOpener(authorizationUrl.href, signal);
      if (!opened && !signal.aborted) this.state = { ...this.state, message: 'Browser opening failed. Open authorizationUrl to finish this attempt before its deadline.' };
      const response = await callback.result;
      this.store.save({ binding: this.binding, refreshIntent: { authorization: true } });
      const tokens = await exchangeAuthorization(this.binding.issuer, {
        metadata: this.metadata, clientInformation: { client_id: this.binding.clientId }, authorizationCode: response.code, iss: response.iss, codeVerifier,
        redirectUri: callback.redirectUri, resource: new URL(this.binding.resource),
        addClientAuthentication: (_headers, params) => params.set('client_id', this.binding.clientId),
        fetchFn: (url, options) => this.options.fetchFn(url, options, signal),
      });
      this._saveTokens(tokens, randomUUID(), 0);
    } finally { callback.close(); }
  }
  async close() {
    if (this.closed) return;
    this.closed = true;
    clearInterval(this.timer);
    this.controller?.abort();
    await this.task;
    this.store?.close();
  }
}
