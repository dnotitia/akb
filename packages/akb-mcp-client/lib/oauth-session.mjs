import { spawn } from 'node:child_process';
import { randomBytes, randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { createCallback } from './oauth-callback.mjs';
import { checkCancelled, OAuthAuthError, oauthBusy } from './oauth-error.mjs';
import * as oauth from './oauth-provider.mjs';
import { OAuthStore } from './oauth-store.mjs';

export { OAuthAuthError } from './oauth-error.mjs';

function openSystemBrowser(url, signal) {
  return new Promise((resolve) => {
    const command = process.platform === 'darwin' ? 'open' : 'xdg-open';
    const child = spawn(command, [url], { stdio: 'ignore', signal });
    const finish = (opened) => {
      clearTimeout(timer);
      resolve(opened);
    };
    const timer = setTimeout(() => {
      child.kill();
      child.unref();
      finish(false);
    }, 5000);
    child.once('error', () => finish(false));
    child.once('exit', (code) => finish(code === 0));
  });
}

function isUsable(grant, now) {
  const lifetime = grant.expiresAt - grant.issuedAt;
  return grant.expiresAt > now + Math.min(30000, lifetime / 10);
}

function matches(grant, credential) {
  return grant?.state === 'active' && grant.id === credential?.grantId && grant.generation === credential.generation;
}

function hasBearerSyntax(token) {
  return typeof token === 'string' && /^[A-Za-z0-9._~+/-]+=*$/.test(token);
}

function activeGrant(tokens, id, generation, now) {
  if (!hasBearerSyntax(tokens?.access_token) || !hasBearerSyntax(tokens.refresh_token)
    || tokens.token_type?.toLowerCase() !== 'bearer'
    || !Number.isFinite(tokens.expires_in) || tokens.expires_in <= 0) return null;
  const expiresAt = now + tokens.expires_in * 1000;
  if (!Number.isFinite(expiresAt) || expiresAt <= now) return null;
  return {
    state: 'active',
    id,
    generation,
    issuedAt: now,
    expiresAt,
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    publicationId: randomUUID(),
  };
}

function snapshot(grant) {
  return Object.freeze({ kind: 'oauth', accessToken: grant.accessToken, grantId: grant.id, generation: grant.generation });
}

export class OAuthSession {
  #store;
  #resource;
  #options;
  #controller = new AbortController();
  #signal;
  #task;
  #closed;
  #rejected;
  #storageError;

  constructor({ resource }, {
    signal,
    storeOptions,
    fetchFn = fetch,
    openBrowser = openSystemBrowser,
    onAuthorizationUrl = (url) => process.stderr.write(`Open this URL to continue OAuth login:\n${url}\n`),
    callbackTimeoutMs = 180000,
    refreshWaitMs = 16000,
    now = Date.now,
  } = {}) {
    this.#resource = resource;
    this.#options = { fetchFn, openBrowser, onAuthorizationUrl, callbackTimeoutMs, refreshWaitMs, now };
    this.#signal = signal ? AbortSignal.any([signal, this.#controller.signal]) : this.#controller.signal;
    if (!['darwin', 'linux'].includes(process.platform)) {
      throw new OAuthAuthError('oauth_storage', 'OAuth requires private local storage on macOS or Linux. Use PAT on this platform.');
    }
    checkCancelled(this.#signal);
    this.#store = this.#storage(() => new OAuthStore(resource, storeOptions));
  }

  status() {
    checkCancelled(this.#signal);
    const record = this.#read();
    const status = { mode: 'oauth', resource: this.#resource, state: 'login_required' };
    if (!record) return status;
    status.issuer = record.binding.issuer;
    if (record.registration) status.clientId = record.registration.client_id;
    if (record.grant?.state === 'refreshing') {
      status.state = 'refresh_pending';
    } else if (this.#published(record.grant) && !matches(record.grant, this.#rejected)) {
      status.state = isUsable(record.grant, this.#options.now()) ? 'ready' : 'refresh_needed';
      status.expiresAt = record.grant.expiresAt;
    }
    return status;
  }

  async credential() {
    checkCancelled(this.#signal);
    const record = this.#read();
    const credential = this.#readyCredential(record);
    if (credential) return credential;
    if (!record?.grant) throw this.#loginRequired();
    if (this.#task?.kind === 'refresh') return this.#task.promise;
    return this.#run('refresh', () => this.#refresh());
  }

  async rejected(credential) {
    checkCancelled(this.#signal);
    const record = this.#read();
    if (!matches(record?.grant, credential)) return;
    this.#rejected = { grantId: credential.grantId, generation: credential.generation };
    if (this.#task || !this.#acquire()) return;
    try {
      const current = this.#read();
      if (matches(current?.grant, credential)) this.#save({ ...current, grant: null });
    } finally {
      this.#release();
    }
  }

  async login() {
    return this.#run('login', async () => {
      if (!this.#acquire()) throw oauthBusy();
      try { return await this.#login(); } finally { this.#release(); }
    });
  }

  async logout() {
    checkCancelled(this.#signal);
    if (this.#task || !this.#acquire()) throw oauthBusy();
    try {
      const record = this.#read();
      if (record?.grant) this.#save({ ...record, grant: null });
      return this.status();
    } finally {
      this.#release();
    }
  }

  async close() {
    if (!this.#closed) {
      this.#controller.abort();
      this.#closed = (async () => {
        await this.#task?.promise.catch(() => {});
        this.#store.close();
      })();
    }
    await this.#closed;
  }

  #run(kind, operation) {
    checkCancelled(this.#signal);
    if (this.#task) throw oauthBusy();
    const promise = operation().finally(() => { this.#task = undefined; });
    this.#task = { kind, promise };
    return promise;
  }

  #readyCredential(record) {
    const grant = record?.grant;
    if (!this.#published(grant) || matches(grant, this.#rejected) || !isUsable(grant, this.#options.now())) return null;
    return snapshot(grant);
  }

  async #refresh() {
    const started = performance.now();
    let waitMs = 100;
    for (;;) {
      checkCancelled(this.#signal);
      const record = this.#read();
      const credential = this.#readyCredential(record);
      if (credential) return credential;
      if (!record?.grant) throw this.#loginRequired();
      if (this.#acquire()) {
        try { return await this.#refreshOwned(); } finally { this.#release(); }
      }
      if (record.grant.state === 'refreshing') waitMs = this.#options.refreshWaitMs;
      const remaining = waitMs - (performance.now() - started);
      if (remaining <= 0) throw oauthBusy();
      await delay(Math.min(50, remaining), undefined, { signal: this.#signal }).catch(() => checkCancelled(this.#signal));
    }
  }

  async #refreshOwned() {
    const record = this.#read();
    const credential = this.#readyCredential(record);
    if (credential) return credential;
    const grant = record?.grant;
    if (!this.#published(grant)) throw this.#loginRequired();
    if (matches(grant, this.#rejected)) {
      this.#save({ ...record, grant: null });
      throw this.#loginRequired();
    }
    const { id, generation, refreshToken } = grant;
    this.#save({ ...record, grant: { state: 'refreshing', id, generation } });
    checkCancelled(this.#signal);
    const tokens = await oauth.refresh(record, refreshToken, this.#signal, this.#options.fetchFn);
    const next = activeGrant(tokens, id, generation + 1, this.#options.now());
    if (!next || next.refreshToken === refreshToken) throw this.#loginRequired();
    return this.#publish({ ...record, grant: next });
  }

  async #login() {
    const deadline = new AbortController();
    const timer = setTimeout(() => {
      deadline.abort(new OAuthAuthError('oauth_timeout', 'OAuth login timed out. Run login again to start a new attempt.'));
    }, this.#options.callbackTimeoutMs);
    const signal = AbortSignal.any([this.#signal, deadline.signal]);
    let callback;
    try {
      let current = this.#read();
      if (current?.grant?.state === 'refreshing') {
        current = { ...current, grant: null };
        this.#save(current);
      }
      const discovered = await oauth.discover(this.#resource, signal, this.#options.fetchFn);
      const { binding } = discovered;
      let record = current;
      if (!oauth.sameBinding(current?.binding, binding)) {
        record = { binding, registration: null, grant: null };
        if (current) this.#save(record);
      }
      const verified = record.registration && await oauth.verifyRegistration(record.registration, discovered);
      if (!verified && record.registration) {
        record = { binding, registration: null, grant: null };
        this.#save(record);
      }
      const registration = verified ?? await oauth.register(discovered);
      record = { ...record, registration };
      this.#save(record);
      const state = randomBytes(32).toString('base64url');
      callback = await createCallback({ state, issuer: binding.issuer, signal });
      const { authorizationUrl, codeVerifier } = await oauth.authorize(discovered, registration, callback.redirectUri, state);
      checkCancelled(signal);
      const opened = await this.#options.openBrowser(authorizationUrl.href, signal);
      checkCancelled(signal);
      if (!opened) this.#options.onAuthorizationUrl(authorizationUrl.href);
      const { code, iss, error } = await callback.result;
      checkCancelled(signal);
      if (error === 'invalid_client') this.#forgetRegistration(binding);
      if (error) throw new OAuthAuthError('oauth_denied', 'OAuth authorization was declined. Run login again to start a new attempt.');
      const tokens = await oauth.exchange(discovered, registration, code, iss, codeVerifier, callback.redirectUri);
      if (!tokens) this.#forgetRegistration(binding);
      checkCancelled(signal);
      const grant = activeGrant(tokens, randomUUID(), 0, this.#options.now());
      if (!grant) throw new OAuthAuthError('oauth_denied', 'The authorization server did not return a usable bearer and refresh token pair.');
      this.#publish({ ...record, grant });
      return this.status();
    } finally {
      clearTimeout(timer);
      callback?.close();
    }
  }

  #forgetRegistration(binding) {
    this.#save({ binding, registration: null, grant: null });
    throw new OAuthAuthError('oauth_denied', 'The authorization server rejected the client registration. Run login again to register a new client.');
  }

  #loginRequired() {
    const url = /^[a-zA-Z0-9:/._[%\]-]+$/.test(this.#resource)
      ? this.#resource
      : `'${this.#resource.replaceAll("'", "'\\''")}'`;
    return new OAuthAuthError('login_required', `OAuth login required. Run npx akb-mcp login --url ${url}`);
  }

  #read() {
    return this.#storage(() => {
      const record = this.#store.read();
      if (!record) return null;
      oauth.validateBinding(record.binding, this.#resource);
      if (record.registration !== null) oauth.validateRegistration(record.registration, record.binding);
      const { grant } = record;
      if (grant === null) return record;
      if (!record.registration || !grant?.id || typeof grant.id !== 'string'
        || !Number.isSafeInteger(grant.generation) || grant.generation < 0) throw new Error('Invalid OAuth grant.');
      if (grant.state === 'refreshing') return record;
      if (grant.state !== 'active' || typeof grant.publicationId !== 'string'
        || !hasBearerSyntax(grant.accessToken) || !hasBearerSyntax(grant.refreshToken)
        || !Number.isFinite(grant.issuedAt) || !Number.isFinite(grant.expiresAt) || grant.issuedAt >= grant.expiresAt) {
        throw new Error('Invalid OAuth token pair.');
      }
      return record;
    });
  }

  #save(record) {
    this.#storage(() => this.#store.save(record));
  }

  #publish(record) {
    this.#save(record);
    // The SQLite marker becomes visible only after the credential file and its rename are durable.
    this.#storage(() => this.#store.publish(record.grant.publicationId));
    return snapshot(record.grant);
  }

  #published(grant) {
    return grant?.state === 'active' && this.#storage(() => this.#store.isPublished(grant.publicationId));
  }

  #acquire() {
    return this.#storage(() => this.#store.acquire());
  }

  #release() {
    this.#store.release();
  }

  #storage(operation) {
    if (this.#storageError) throw this.#storageError;
    try { return operation(); } catch (error) {
      if (error instanceof OAuthAuthError && error.code === 'oauth_busy') throw error;
      this.#storageError = new OAuthAuthError('oauth_storage', 'OAuth credential storage is unsafe or unavailable. Check private local storage, then run login again.');
      throw this.#storageError;
    }
  }
}
