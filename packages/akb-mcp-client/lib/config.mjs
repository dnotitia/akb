export function validateEndpoint(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error('A valid --url MCP endpoint is required.'); }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error('The MCP URL must not contain user information, a query, or a fragment.');
  }
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) {
    throw new Error('The MCP URL requires HTTPS. HTTP is allowed only on loopback for development.');
  }
  if (!/\/mcp\/?$/.test(url.pathname)) throw new Error('The MCP URL path must end in /mcp.');
  url.pathname = url.pathname.replace(/\/$/, '');
  return { resource: url.href, endpointUrl: url.href + '/', baseUrl: url.href.slice(0, -4) };
}

export function parseConfiguration(argv, env = process.env) {
  const options = {};
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (!['--url', '--pat', '--auth-mode'].includes(key)) throw new Error(`Unknown option: ${key}`);
    if (options[key] !== undefined) throw new Error(`Duplicate option: ${key}`);
    const value = argv[++i];
    if (!value || value.startsWith('--')) throw new Error(`A value is required for ${key}.`);
    options[key] = value;
  }
  const installation = validateEndpoint(options['--url'] ?? env.AKB_MCP_URL);
  const pat = options['--pat'] ?? env.AKB_PAT;
  const mode = options['--auth-mode'] ?? (pat ? 'pat' : 'oauth');
  if (!['pat', 'oauth'].includes(mode)) throw new Error('--auth-mode must be pat or oauth.');
  if (mode === 'oauth' && pat) throw new Error('OAuth mode conflicts with --pat or AKB_PAT. Remove the PAT input.');
  if (mode === 'pat' && !pat) throw new Error('PAT mode requires --pat or AKB_PAT.');
  if (pat && /[\r\n]/.test(pat)) throw new Error('The PAT must not contain line breaks.');
  return { installation, authentication: mode === 'pat' ? { kind: 'pat', pat } : { kind: 'oauth' } };
}
