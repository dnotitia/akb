import { timingSafeEqual } from 'node:crypto';
import { createServer } from 'node:http';
import { checkCancelled, OAuthAuthError } from './oauth-error.mjs';

export const CALLBACK_PATH = '/oauth/callback';

const RESPONSE_HEADERS = {
  'Content-Type': 'text/plain; charset=utf-8',
  'Cache-Control': 'no-store',
  'Content-Security-Policy': "default-src 'none'",
  'Referrer-Policy': 'no-referrer',
  Connection: 'close',
};

export async function createCallback({ state, issuer, signal }) {
  checkCancelled(signal);
  const { promise: result, resolve, reject } = Promise.withResolvers();
  result.catch(() => {});
  const expectedState = Buffer.from(state);
  let used = false;
  const server = createServer({
    maxHeaderSize: 8192,
    headersTimeout: 5000,
    requestTimeout: 5000,
    connectionsCheckingInterval: 1000,
  }, (request, response) => {
    const callback = parseCallback(request, server.address()?.port, expectedState, issuer);
    if (used || signal.aborted || !callback) {
      response.writeHead(400, RESPONSE_HEADERS);
      response.end('Invalid authorization callback.');
      return;
    }
    used = true;
    server.close();
    response.writeHead(200, RESPONSE_HEADERS);
    const complete = () => resolve(callback);
    response.once('close', complete);
    response.end(
      callback.error ? 'Authorization did not complete. Return to the terminal.' : 'Authorization received. Return to the terminal.',
      complete,
    );
  });
  server.setTimeout(5000, (socket) => socket.destroy());
  const close = () => {
    signal.removeEventListener('abort', abort);
    server.close();
    server.closeAllConnections();
  };
  const abort = () => {
    try { checkCancelled(signal); } catch (error) { reject(error); }
    close();
  };
  await new Promise((ready, failed) => {
    server.once('error', failed);
    server.listen(0, '127.0.0.1', ready);
  }).catch(() => { throw new OAuthAuthError('oauth_callback', 'Could not start the local OAuth callback listener.'); });
  const redirectUri = new URL(CALLBACK_PATH, `http://127.0.0.1:${server.address().port}`).href;
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) abort();
  return { redirectUri, result, close };
}

function parseCallback(request, port, expectedState, issuer) {
  const target = request.url ?? '';
  const hostHeaders = request.headersDistinct.host;
  if (request.method !== 'GET' || hostHeaders?.length !== 1 || hostHeaders[0] !== `127.0.0.1:${port}`) return null;
  if (!target.startsWith(`${CALLBACK_PATH}?`) || target.length > 8192 || target.includes('#')) return null;
  const { searchParams } = new URL(target, 'http://127.0.0.1');
  for (const name of ['state', 'code', 'iss', 'error', 'error_description']) {
    if (searchParams.getAll(name).length > 1) return null;
  }
  const returnedState = Buffer.from(searchParams.get('state') ?? '');
  if (returnedState.length !== expectedState.length || !timingSafeEqual(returnedState, expectedState)) return null;
  const iss = searchParams.get('iss');
  if (iss !== issuer) return null;
  const code = searchParams.get('code');
  const error = searchParams.get('error');
  if (code === null ? !error : !code || error !== null || code.length > 4096) return null;
  return { code, iss, error };
}
