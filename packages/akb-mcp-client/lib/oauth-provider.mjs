import {
  discoverAuthorizationServerMetadata,
  discoverOAuthProtectedResourceMetadata,
  exchangeAuthorization,
  refreshAuthorization,
  registerClient,
  startAuthorization,
} from '@modelcontextprotocol/client';
import { CALLBACK_PATH } from './oauth-callback.mjs';
import { checkCancelled, OAuthAuthError } from './oauth-error.mjs';

const NATIVE_REDIRECT_URI = `http://127.0.0.1${CALLBACK_PATH}`;
const GRANT_TYPES = ['authorization_code', 'refresh_token'];
const AKB_SCOPES = ['akb:vault:read', 'akb:vault:write'];
const IDENTITY_SCOPES = ['profile', 'email'];
const MAX_RESPONSE_BYTES = 1024 * 1024;
const LOOPBACK = new Set(['127.0.0.1', '[::1]', 'localhost']);

function configuration(message) {
  return new OAuthAuthError('oauth_configuration', message);
}

function transport() {
  return new OAuthAuthError('oauth_transport', 'The OAuth server could not be reached. Check your connection and the installation URL.');
}

function endpoint(value, expectedOrigin) {
  const url = typeof value === 'string' && /^https?:\/\//i.test(value) ? URL.parse(value) : null;
  if (!url || url.username || url.password || /[?#]/.test(url.href)
    || /^https?:\/\/[^/]*@/i.test(value)
    || (url.protocol !== 'https:' && !(url.protocol === 'http:' && LOOPBACK.has(url.hostname)))
    || (expectedOrigin && url.origin !== expectedOrigin)) {
    throw configuration('OAuth metadata contains an unsafe endpoint.');
  }
  return url;
}

function issuerOrigin(issuer, resource) {
  const url = endpoint(issuer);
  if (endpoint(resource).protocol === 'https:' && url.protocol !== 'https:') {
    throw configuration('An HTTPS MCP resource requires an HTTPS authorization server.');
  }
  return url.origin;
}

function boundedFetch(expectedOrigin, signal, fetchFn) {
  return async (input, options) => {
    const url = endpoint(String(input), expectedOrigin);
    const requestSignal = AbortSignal.any([signal, AbortSignal.timeout(15000)]);
    let response;
    try {
      response = await fetchFn(url, { ...options, redirect: 'manual', signal: requestSignal });
      if (response.status >= 300 && response.status < 400) {
        await response.body?.cancel();
        throw configuration('OAuth endpoints must respond without redirects.');
      }
      if (response.status >= 500 || [408, 429].includes(response.status)) {
        await response.body?.cancel();
        throw transport();
      }
      const reader = response.body?.getReader();
      if (!reader) return response;
      const chunks = [];
      let size = 0;
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          size += value.length;
          if (size > MAX_RESPONSE_BYTES) throw configuration('The OAuth server response exceeds the size limit.');
          chunks.push(value);
        }
      } finally {
        await reader.cancel().catch(() => {});
      }
      return new Response(Buffer.concat(chunks), { status: response.status, headers: response.headers });
    } catch (error) {
      checkCancelled(signal);
      if (error instanceof OAuthAuthError) throw error;
      throw transport();
    }
  };
}

export function validateBinding(binding, resource) {
  if (binding?.resource !== resource || typeof binding.scope !== 'string') {
    throw configuration('The saved OAuth binding does not match this installation.');
  }
  const origin = issuerOrigin(binding.issuer, resource);
  endpoint(binding.tokenEndpoint, origin);
  endpoint(binding.registrationEndpoint, origin);
  const scopes = binding.scope.split(' ');
  if (!scopes.some((scope) => AKB_SCOPES.includes(scope))
    || !scopes.every((scope) => [...AKB_SCOPES, ...IDENTITY_SCOPES].includes(scope))) {
    throw configuration('The saved OAuth binding has an invalid scope.');
  }
}

export function sameBinding(left, right) {
  return left?.resource === right.resource
    && left.issuer === right.issuer
    && left.tokenEndpoint === right.tokenEndpoint
    && left.registrationEndpoint === right.registrationEndpoint
    && left.scope === right.scope;
}

export function validateRegistration(registration, binding) {
  if (typeof registration?.client_id !== 'string' || !registration.client_id.trim()
    || registration.client_secret !== undefined || registration.token_endpoint_auth_method !== 'none'
    || !Array.isArray(registration.redirect_uris) || registration.redirect_uris.length !== 1
    || registration.redirect_uris[0] !== NATIVE_REDIRECT_URI
    || !Array.isArray(registration.grant_types) || registration.grant_types.length !== GRANT_TYPES.length
    || !GRANT_TYPES.every((grant) => registration.grant_types.includes(grant))
    || !Array.isArray(registration.response_types) || !registration.response_types.includes('code')
    || registration.response_types.some((type) => type !== 'code' && type !== 'none')) {
    throw configuration('The authorization server did not return the required public native-client registration.');
  }
  const uri = registration.registration_client_uri;
  const token = registration.registration_access_token;
  if (uri === undefined && token === undefined) return;
  const registrationEndpoint = endpoint(binding.registrationEndpoint, endpoint(binding.issuer).origin);
  const managementEndpoint = endpoint(uri, registrationEndpoint.origin);
  const clientPath = `${registrationEndpoint.pathname.replace(/\/$/, '')}/${encodeURIComponent(registration.client_id)}`;
  if (managementEndpoint.pathname !== clientPath || typeof token !== 'string' || !/^[A-Za-z0-9._~+/=-]+$/.test(token)) {
    throw configuration('The authorization server returned an unsafe registration-management binding.');
  }
}

export async function discover(resource, signal, fetchFn) {
  try {
    const resourceFetch = boundedFetch(endpoint(resource).origin, signal, fetchFn);
    const metadata = await discoverOAuthProtectedResourceMetadata(resource, undefined, resourceFetch);
    if (metadata.resource !== resource || metadata.authorization_servers?.length !== 1) {
      throw configuration('OAuth protected-resource metadata does not match the configured MCP URL.');
    }
    const issuer = metadata.authorization_servers[0];
    const origin = issuerOrigin(issuer, resource);
    const issuerFetch = boundedFetch(origin, signal, fetchFn);
    const server = await discoverAuthorizationServerMetadata(issuer, { fetchFn: issuerFetch });
    if (server?.issuer !== issuer
      || !server.response_types_supported?.includes('code')
      || !GRANT_TYPES.every((grant) => server.grant_types_supported?.includes(grant))
      || !server.code_challenge_methods_supported?.includes('S256')
      || server.authorization_response_iss_parameter_supported !== true) {
      throw configuration('The authorization server must support public-client code and refresh grants, S256 PKCE, and an issuer in its callback.');
    }
    if (!server.registration_endpoint) {
      throw configuration('The authorization server does not advertise dynamic client registration. Ask the installation operator to enable it.');
    }
    endpoint(server.authorization_endpoint, origin);
    const toolScopes = AKB_SCOPES.filter((scope) => metadata.scopes_supported?.includes(scope));
    const identityScopes = IDENTITY_SCOPES.filter((scope) => server.scopes_supported?.includes(scope));
    const binding = {
      resource,
      issuer,
      tokenEndpoint: server.token_endpoint,
      registrationEndpoint: server.registration_endpoint,
      scope: [...identityScopes, ...toolScopes].join(' '),
    };
    validateBinding(binding, resource);
    return { binding, server, fetchFn: issuerFetch };
  } catch (error) {
    checkCancelled(signal);
    if (error instanceof OAuthAuthError) throw error;
    throw configuration('The installation OAuth discovery document is missing or incompatible. Check the installation URL and OAuth configuration.');
  }
}

export async function register({ binding, server, fetchFn }) {
  let extra;
  try {
    const registration = await registerClient(binding.issuer, {
      metadata: server,
      clientMetadata: {
        client_name: 'AKB MCP CLI',
        redirect_uris: [NATIVE_REDIRECT_URI],
        token_endpoint_auth_method: 'none',
        grant_types: GRANT_TYPES,
        response_types: ['code'],
        application_type: 'native',
      },
      scope: binding.scope,
      fetchFn: async (url, options) => {
        const response = await fetchFn(url, options);
        if (response.ok) extra = managementFields(await response.clone().json());
        return response;
      },
    });
    const result = { ...registration, ...extra };
    validateRegistration(result, binding);
    return result;
  } catch (error) {
    if (error instanceof OAuthAuthError) throw error;
    throw configuration('Dynamic client registration failed. Ask the installation operator to check the public native-client registration policy.');
  }
}

function managementFields(document) {
  return {
    registration_client_uri: document.registration_client_uri,
    registration_access_token: document.registration_access_token,
  };
}

export async function verifyRegistration(registration, { binding, fetchFn }) {
  if (!registration.registration_client_uri) return registration;
  const response = await fetchFn(registration.registration_client_uri, {
    headers: { Authorization: `Bearer ${registration.registration_access_token}`, Accept: 'application/json' },
  });
  if ([401, 404].includes(response.status)) return null;
  if (!response.ok) throw configuration('The authorization server refused to verify the stored client registration.');
  let document;
  try { document = await response.json(); } catch { throw configuration('The stored client registration could not be verified.'); }
  if (document?.client_id !== registration.client_id) {
    throw configuration('The authorization server returned a different client for the stored registration.');
  }
  const result = {
    ...registration,
    token_endpoint_auth_method: document.token_endpoint_auth_method,
    client_secret: document.client_secret,
    redirect_uris: document.redirect_uris,
    grant_types: document.grant_types,
    response_types: document.response_types,
    registration_client_uri: document.registration_client_uri ?? registration.registration_client_uri,
    registration_access_token: document.registration_access_token ?? registration.registration_access_token,
  };
  try { validateRegistration(result, binding); } catch { return null; }
  return result;
}

export function authorize({ binding, server }, registration, redirectUrl, state) {
  const scope = server.scopes_supported?.includes('openid') ? `openid ${binding.scope}` : binding.scope;
  return startAuthorization(binding.issuer, {
    metadata: server,
    clientInformation: registration,
    redirectUrl,
    scope,
    state,
    resource: binding.resource,
  });
}

export async function exchange({ binding, server, fetchFn }, registration, code, iss, codeVerifier, redirectUri) {
  try {
    return await exchangeAuthorization(binding.issuer, {
      metadata: server,
      clientInformation: registration,
      authorizationCode: code,
      iss,
      codeVerifier,
      redirectUri,
      resource: binding.resource,
      fetchFn,
    });
  } catch (error) {
    if (error instanceof OAuthAuthError) throw error;
    if (error.code === 'invalid_client') return null;
    throw new OAuthAuthError('oauth_denied', 'The authorization code could not be exchanged. Run login again.');
  }
}

export async function refresh({ binding, registration }, refreshToken, signal, fetchFn) {
  try {
    return await refreshAuthorization(binding.issuer, {
      metadata: { token_endpoint: binding.tokenEndpoint },
      clientInformation: registration,
      refreshToken,
      resource: binding.resource,
      fetchFn: boundedFetch(endpoint(binding.issuer).origin, signal, fetchFn),
    });
  } catch (error) {
    checkCancelled(signal);
    if (error instanceof OAuthAuthError) throw error;
    return null;
  }
}
