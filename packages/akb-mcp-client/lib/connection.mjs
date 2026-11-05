import { request as httpsRequest, Agent as HttpsAgent } from 'node:https';
import { request as httpRequest, Agent as HttpAgent } from 'node:http';
import { CredentialUnavailable } from './credentials.mjs';

const controls = [
  ['POST', /^\/api\/v1\/files\/[^/]+\/upload$/],
  ['POST', /^\/api\/v1\/files\/[^/]+\/[^/]+\/confirm$/],
  ['POST', /^\/api\/v1\/files\/[^/]+\/[^/]+\/replace$/],
  ['POST', /^\/api\/v1\/files\/[^/]+\/[^/]+\/replace\/[^/]+\/confirm$/],
  ['GET', /^\/api\/v1\/files\/[^/]+\/[^/]+\/download$/],
  ['DELETE', /^\/api\/v1\/files\/[^/]+\/[^/]+$/],
  ['POST', /^\/api\/v1\/assets\/[^/]+$/],
  ['DELETE', /^\/api\/v1\/assets\/[^/]+\/[^/]+$/],
];

export class ToolConnection {
  constructor(installation, owner) {
    this.installation = installation;
    this.owner = owner;
    this.agents = { 'https:': new HttpsAgent({ keepAlive: true }), 'http:': new HttpAgent({ keepAlive: true }) };
    this.connectivity = 'unknown';
  }
  close() { for (const agent of Object.values(this.agents)) agent.destroy(); }
  mcp(body, headers, options) { return this._request('POST', new URL(this.installation.resource), body, headers, options); }
  control(method, path, body, headers, options) {
    const pathname = path.split('?')[0];
    if (!controls.some(([verb, pattern]) => verb === method && pattern.test(pathname))) {
      throw new Error('This operation is outside the tool resource control contract.');
    }
    return this._request(method, new URL(this.installation.baseUrl + path), body, headers, options);
  }
  _request(method, url, body, extraHeaders = {}, options = {}) {
    const snapshot = this.owner.currentCredential();
    if (!snapshot) return Promise.reject(new CredentialUnavailable(this.owner.status()));
    const signal = options.signal;
    if (signal?.aborted) return Promise.reject(Object.assign(new Error('Request cancelled before dispatch.'), { code: 'cancelled', execution: 'not_started' }));
    return new Promise((resolve, reject) => {
      let dispatched = false;
      let settled = false;
      let connectTimer;
      const fail = () => {
        if (settled) return;
        settled = true;
        clearTimeout(connectTimer);
        this.connectivity = 'unreachable';
        const error = new Error(signal?.aborted ? 'Request cancelled. The dispatched outcome may be unknown.' : 'The backend connection failed. Submit a new call only after checking its outcome.');
        Object.assign(error, { code: signal?.aborted ? 'cancelled' : 'network_error', connectionError: true, execution: dispatched ? 'unknown' : 'not_started' });
        reject(error);
      };
      const headers = { ...extraHeaders, Authorization: `Bearer ${snapshot.accessToken}` };
      if (body != null) {
        headers['Content-Length'] = Buffer.byteLength(body);
        if (!headers['Content-Type']) headers['Content-Type'] = 'application/json';
      }
      const request = (url.protocol === 'https:' ? httpsRequest : httpRequest)(url, {
        method, headers, agent: this.agents[url.protocol], signal,
      }, (response) => {
        dispatched = true;
        clearTimeout(connectTimer);
        const chunks = [];
        let size = 0;
        response.on('data', (chunk) => {
          size += chunk.length;
          if (size > 32 * 1024 * 1024) request.destroy(new Error('Control response exceeds the size limit.'));
          else chunks.push(chunk);
        });
        response.on('aborted', fail);
        response.on('error', fail);
        response.on('end', () => {
          if (settled) return;
          settled = true;
          const text = Buffer.concat(chunks).toString('utf8');
          this.connectivity = 'reachable';
          const status = response.statusCode;
          if (status >= 200 && status < 300) return resolve({ text, headers: response.headers, statusCode: status });
          let code;
          try { const parsed = JSON.parse(text); code = parsed.code ?? parsed.detail?.code ?? parsed.error?.data?.code; } catch {}
          if (status === 401) this.owner.rejectCredential(snapshot);
          const error = new Error(status === 401 ? 'The selected credential was rejected. Check akb_connection before a new call.' : status === 403 ? (code === 'insufficient_scope' ? 'The selected grant lacks the required scope.' : 'Access was denied by the resource policy.') : status >= 300 && status < 400 ? 'Control redirects are refused.' : `Control request failed with HTTP ${status}.`);
          Object.assign(error, { statusCode: status, code: status === 401 ? 'authentication_rejected' : code ?? (status === 403 ? 'access_denied' : 'control_error'), execution: status >= 500 ? 'unknown' : 'not_started' });
          reject(error);
        });
      });
      request.on('socket', (socket) => {
        if (socket.connecting) {
          connectTimer = setTimeout(() => request.destroy(new Error('Connection timeout.')), options.connectTimeoutMs ?? 10000);
          socket.once('connect', () => clearTimeout(connectTimer));
        }
      });
      request.on('finish', () => { dispatched = true; });
      request.on('error', fail);
      request.setTimeout(options.timeoutMs ?? 300000, () => request.destroy(new Error('Request timeout.')));
      request.end(body ?? undefined);
    });
  }
}
