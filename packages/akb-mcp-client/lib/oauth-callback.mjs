import { createServer } from 'node:http';
import { timingSafeEqual } from 'node:crypto';

export async function createCallback({ state, issuer, issuerRequired, signal, timeoutMs = 180000 }) {
  let settle;
  let consumed = false;
  const result = new Promise((resolve, reject) => { settle = { resolve, reject }; });
  result.catch(() => {});
  const server = createServer((request, response) => {
    const fail = () => { response.writeHead(400, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' }); response.end('Invalid authorization callback.'); };
    const hostCount = request.rawHeaders.filter((_value, index) => index % 2 === 0 && request.rawHeaders[index].toLowerCase() === 'host').length;
    if (consumed || !/^\/oauth\/callback(?:\?|$)/.test(request.url ?? '') || hostCount !== 1 || request.method !== 'GET' || request.headers.host !== `127.0.0.1:${server.address().port}` || (request.url?.length ?? 0) > 8192) return fail();
    let url;
    try { url = new URL(request.url, 'http://127.0.0.1'); } catch { return fail(); }
    if (url.pathname !== '/oauth/callback' || url.hash) return fail();
    for (const name of ['state', 'code', 'iss', 'error', 'error_description']) if (url.searchParams.getAll(name).length > 1) return fail();
    const returnedState = url.searchParams.get('state') ?? '';
    if (Buffer.byteLength(returnedState) !== Buffer.byteLength(state) || !timingSafeEqual(Buffer.from(returnedState), Buffer.from(state))) return fail();
    const returnedIssuer = url.searchParams.get('iss');
    if ((issuerRequired && !returnedIssuer) || (returnedIssuer && returnedIssuer !== issuer)) return fail();
    const code = url.searchParams.get('code');
    const denied = url.searchParams.get('error');
    if ((!code && !denied) || (code && denied) || (code?.length ?? 0) > 4096) return fail();
    consumed = true;
    response.writeHead(200, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'none'", 'Referrer-Policy': 'no-referrer' });
    response.end(denied ? 'Authorization was declined. Return to your MCP host to retry.' : 'Authorization received. Return to your MCP host.');
    if (denied) settle.reject(Object.assign(new Error('Authorization was declined. Use akb_connection sign_in to retry.'), { code: 'denied' }));
    else settle.resolve({ code, iss: returnedIssuer ?? undefined });
    close();
  });
  server.maxHeadersCount = 30;
  server.headersTimeout = 5000;
  server.requestTimeout = 5000;
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const redirectUri = `http://127.0.0.1:${server.address().port}/oauth/callback`;
  const abort = () => { settle.reject(Object.assign(new Error('Authorization was cancelled.'), { code: 'cancelled' })); close(); };
  const timer = setTimeout(() => { settle.reject(Object.assign(new Error('Authorization timed out. Use akb_connection sign_in to retry.'), { code: 'timeout' })); close(); }, timeoutMs);
  const close = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); server.close(); server.closeAllConnections(); };
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) abort();
  return { redirectUri, result, close };
}
