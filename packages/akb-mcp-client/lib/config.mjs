const commands = new Set(["login", "status", "logout"]);
const options = new Set(["--url", "--pat", "--auth-mode"]);

function installation(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error("Supply a valid MCP endpoint with --url or AKB_MCP_URL.");
  }
  if (url.username || url.password || /^[a-z]+:\/\/[^/]*@/i.test(value) ||
      url.href.includes("?") || url.href.includes("#")) {
    throw new Error("The MCP URL must not contain user information, a query, or a fragment.");
  }
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) {
    throw new Error("The MCP URL requires HTTPS. Use HTTP only on loopback for development.");
  }
  if (!/\/mcp\/?$/.test(url.pathname)) throw new Error("The MCP URL path must end in /mcp.");
  const resource = url.href.replace(/\/$/, "");
  return { resource, endpointUrl: resource + "/", baseUrl: resource.slice(0, -4) };
}

export function parseConfiguration(argv, env = process.env) {
  const command = commands.has(argv[0]) ? argv[0] : "stdio";
  const args = command === "stdio" ? argv : argv.slice(1);
  const values = new Map();
  for (let i = 0; i < args.length; i += 2) {
    const [key, value] = [args[i], args[i + 1]];
    if (!options.has(key) || values.has(key) || !value || value.startsWith("--")) {
      throw new Error("Invalid or repeated command option. Run akb-mcp --help.");
    }
    values.set(key, value);
  }

  const target = installation(values.get("--url") ?? env.AKB_MCP_URL);
  const pat = values.get("--pat") ?? env.AKB_PAT;
  const mode = values.get("--auth-mode") ?? (pat ? "pat" : "oauth");
  if (!["pat", "oauth"].includes(mode)) throw new Error("--auth-mode must be pat or oauth.");
  if (mode === "oauth" && pat) throw new Error("OAuth mode conflicts with --pat or AKB_PAT.");
  if (mode === "pat" && !pat) throw new Error("PAT mode requires --pat or AKB_PAT.");
  if (pat && /[\s\x00-\x1f\x7f]/.test(pat)) throw new Error("The PAT contains invalid characters.");
  if (mode === "pat" && (command === "login" || command === "logout")) {
    throw new Error("Select OAuth without a PAT to use this command.");
  }

  return { command, installation: target, authentication: mode === "pat" ? { kind: "pat", pat } : { kind: "oauth" } };
}
