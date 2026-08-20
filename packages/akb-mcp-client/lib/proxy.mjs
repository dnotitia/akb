/**
 * AKB MCP Proxy — stdio ↔ Streamable HTTP with auto-reconnect.
 *
 * - Reads JSON-RPC from stdin, forwards to AKB server over HTTP
 * - Handles file tools locally:
 *   - Gets presigned URLs from AKB server
 *   - Uploads/downloads directly to/from S3 (AKB never touches file bytes)
 *   - Uploads bounded document images through AKB for validation and ACLs
 * - Auto-reconnects on server restart
 * - Zero dependencies (Node.js built-in only)
 */

import { request as httpsRequest, Agent as httpsAgent } from "node:https";
import { request as httpRequest, Agent as httpAgent } from "node:http";
import { createInterface } from "node:readline";
import { createReadStream, createWriteStream, readFileSync, statSync } from "node:fs";
import { mkdir, stat as fsStat } from "node:fs/promises";
import { basename, dirname, extname, join } from "node:path";
import { createHash } from "node:crypto";

// ── Connection reuse ───────────────────────────────────────
// Without keepAlive agents, every MCP tool call (search, browse, put,
// update, relations, …) triggers a fresh TCP+TLS handshake to the backend.
// A typical agent session chains 5–15 tool calls; reusing connections
// saves one round-trip per call (40–100 ms on a nearby cloud backend,
// more across regions or with slow TLS termination).
const httpKeepAlive = new httpAgent({ keepAlive: true });
const httpsKeepAlive = new httpsAgent({ keepAlive: true });

// ── MIME type inference ────────────────────────────────────
// Covers common file types. Unknown extensions fall back to octet-stream.
// Callers can override via the `mime_type` parameter of akb_put_file.

const MIME_TABLE = {
  ".html": "text/html", ".htm": "text/html",
  ".css": "text/css", ".js": "text/javascript", ".mjs": "text/javascript",
  ".json": "application/json", ".xml": "application/xml",
  ".yaml": "application/yaml", ".yml": "application/yaml",
  ".txt": "text/plain", ".md": "text/markdown", ".log": "text/plain",
  ".csv": "text/csv", ".tsv": "text/tab-separated-values",
  ".pdf": "application/pdf",
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
  ".bmp": "image/bmp", ".ico": "image/x-icon",
  ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
  ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
  ".zip": "application/zip", ".gz": "application/gzip", ".tar": "application/x-tar",
  ".7z": "application/x-7z-compressed",
  ".doc": "application/msword",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xls": "application/vnd.ms-excel",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".ppt": "application/vnd.ms-powerpoint",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".hwp": "application/x-hwp", ".hwpx": "application/haansofthwpx",
  ".parquet": "application/vnd.apache.parquet",
  ".arrow": "application/vnd.apache.arrow.file",
};

function guessMime(filename) {
  const dot = filename.lastIndexOf(".");
  if (dot < 0) return "application/octet-stream";
  return MIME_TABLE[filename.slice(dot).toLowerCase()] || "application/octet-stream";
}

const DOCUMENT_IMAGE_MAX_BYTES = 10 * 1024 * 1024;
const DOCUMENT_IMAGE_MIMES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);
const ASSET_URL_RE = /^\/api\/assets\/([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\/?$/i;

function parseAssetUrl(url) {
  if (typeof url !== "string") throw new Error("image url must be a string");
  const match = ASSET_URL_RE.exec(url);
  if (!match) {
    throw new Error(
      `Invalid document image URL: '${url}'. Expected /api/assets/<uuid>.`,
    );
  }
  return match[1].toLowerCase();
}

function markdownAltText(value) {
  return String(value || "Image")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\\/g, "\\\\")
    .replace(/\[/g, "\\[")
    .replace(/\]/g, "\\]") || "Image";
}

// ── Unicode NFC normalization ──────────────────────────────
// macOS (HFS+/APFS) reports Hangul filenames as NFD (decomposed jamo).
// If we forward that NFD text to the backend as titles/paths/args, the
// BM25 tokenizer and embedding model treat it as different tokens from
// user queries typed in NFC, and the document becomes invisible to
// search. Normalize every outbound string here — idempotent for
// already-NFC text, cheap, and catches args read from disk.

function nfcDeep(value) {
  if (typeof value === "string") return value.normalize("NFC");
  if (Array.isArray(value)) return value.map(nfcDeep);
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      out[typeof k === "string" ? k.normalize("NFC") : k] = nfcDeep(v);
    }
    return out;
  }
  return value;
}

// `parent` URI decoder for write tools. Mirrors the backend's
// `_resolve_parent` helper (mcp_server/server.py). Accepts:
//   akb://{vault}                    → (vault, "")
//   akb://{vault}/coll/{path}        → (vault, path)
// Falls back to legacy `vault` + `collection` args when `parent`
// is absent. Throws on a leaf URI (doc/table/file) or malformed
// input.
const PARENT_VAULT_RE = /^akb:\/\/([^/]+)\/?$/;
const PARENT_COLL_RE = /^akb:\/\/([^/]+)\/coll\/([^/]+(?:\/[^/]+)*)\/?$/;
const PARENT_LEAF_RE = /^akb:\/\/[^/]+(?:\/coll\/[^/]+(?:\/[^/]+)*)?\/(doc|table|file)\//;

function _resolveParent(args) {
  const parent = args.parent;
  if (parent) {
    if (PARENT_LEAF_RE.test(parent)) {
      throw new Error(
        `Invalid \`parent\` URI: '${parent}' addresses a leaf resource. ` +
        `Use a vault root or coll URI to place a new resource inside.`,
      );
    }
    let m = parent.match(PARENT_COLL_RE);
    if (m) return { vault: m[1], collection: m[2] };
    m = parent.match(PARENT_VAULT_RE);
    if (m) return { vault: m[1], collection: "" };
    throw new Error(
      `Invalid \`parent\` URI: '${parent}'. Expected akb://<vault> or ` +
      `akb://<vault>/coll/<path>.`,
    );
  }
  return { vault: args.vault, collection: args.collection || "" };
}

// ── File tool definitions (injected into tools/list) ────────

const FILE_TOOLS = [
  {
    name: "akb_put_file",
    description:
      "Upload a local file to a vault's file storage (S3-backed). Use for PDFs, images, datasets, or any binary content too large for akb_put. Response includes the canonical `uri` — `akb://{vault}/coll/{collection}/file/{uuid}` when stored under a collection, or `akb://{vault}/file/{uuid}` at the vault root — pass that to akb_get_file / akb_update_file / akb_delete_file. MIME type is auto-detected from the filename extension unless overridden.",
    inputSchema: {
      type: "object",
      properties: {
        parent: {
          type: "string",
          description:
            "Parent location as a canonical URI — `akb://{vault}` for the vault root, " +
            "`akb://{vault}/coll/{path}` for a collection. When given, the file is " +
            "uploaded there and `vault`/`collection` are derived from the URI.",
        },
        vault: { type: "string", description: "Vault name. Required unless `parent` is given." },
        file_path: {
          type: "string",
          description: "Absolute path to the local file to upload",
        },
        collection: {
          type: "string",
          description: "Logical grouping (like document collections). Ignored when `parent` is given.",
          default: "",
        },
        description: {
          type: "string",
          description: "Brief description of the file",
        },
        mime_type: {
          type: "string",
          description:
            "MIME type of the file (e.g. 'text/html', 'application/pdf', 'image/png'). " +
            "Optional — if omitted, it is auto-detected from the filename extension. " +
            "Override only when the extension is missing, ambiguous, or wrong.",
        },
      },
      required: ["file_path"],
    },
  },
  {
    name: "akb_put_image",
    description:
      "Upload a local PNG, JPEG, GIF, or WebP (maximum 10 MiB) for inline use in an AKB Markdown document. Returns a stable `/api/assets/{uuid}` URL and a ready-to-paste `markdown` image expression. For a new document, place it with akb_put. For an existing document, prefer a targeted akb_edit; akb_update(content=...) replaces the entire body and must never receive only an image fragment. Images are immutable: upload a replacement and edit the Markdown reference. This creates a hidden document attachment, not a standalone File; use akb_put_file when the binary should appear in browse/search. If the document write fails, call akb_discard_image with the returned URL.",
    inputSchema: {
      type: "object",
      properties: {
        parent: {
          type: "string",
          description:
            "Vault or collection URI (`akb://{vault}` or `akb://{vault}/coll/{path}`). " +
            "The image is owned by that vault; the collection portion only identifies the vault.",
        },
        vault: {
          type: "string",
          description: "Vault name. Required unless `parent` is given.",
        },
        file_path: {
          type: "string",
          description: "Absolute path to the local image file (maximum 10 MiB).",
        },
        alt_text: {
          type: "string",
          description:
            "Accessible Markdown alt text. Defaults to the filename without its extension.",
        },
        mime_type: {
          type: "string",
          enum: ["image/png", "image/jpeg", "image/gif", "image/webp"],
          description:
            "Optional MIME override for extensionless or unusually named files. The server verifies it against decoded bytes.",
        },
      },
      required: ["file_path"],
    },
  },
  {
    name: "akb_discard_image",
    description:
      "Discard a document image upload that was never committed in an AKB document. Use this only to clean up after a failed or abandoned akb_put/akb_update. Images already claimed by a document or retained Git revision cannot be discarded through this tool.",
    inputSchema: {
      type: "object",
      properties: {
        parent: {
          type: "string",
          description:
            "Vault or collection URI used for the upload. The vault is derived from it.",
        },
        vault: {
          type: "string",
          description: "Vault name. Required unless `parent` is given.",
        },
        url: {
          type: "string",
          description: "Stable `/api/assets/{uuid}` URL returned by akb_put_image.",
        },
      },
      required: ["url"],
    },
  },
  {
    name: "akb_get_file",
    description: "Download a file from vault storage to a local path. Pass the file URI — `akb://{vault}[/coll/{coll_path}]/file/{uuid}` — from akb_browse or akb_put_file.",
    inputSchema: {
      type: "object",
      properties: {
        uri: {
          type: "string",
          description: "File URI (akb://{vault}/file/{id})",
        },
        save_to: {
          type: "string",
          description: "Local directory or file path to save to",
        },
      },
      required: ["uri", "save_to"],
    },
  },
  {
    name: "akb_update_file",
    description:
      "Replace the bytes of an existing vault file while preserving its URI. The local file is hashed before transfer; identical content is skipped. Pass expected_content_hash and/or expected_version from akb_get_file to reject stale writes with HTTP 409 instead of overwriting a concurrent change.",
    inputSchema: {
      type: "object",
      properties: {
        uri: {
          type: "string",
          description: "Existing file URI (`akb://{vault}[/coll/{path}]/file/{uuid}`)",
        },
        file_path: {
          type: "string",
          description: "Absolute path to the local replacement file",
        },
        expected_content_hash: {
          type: "string",
          description: "Optional sha256 returned by akb_get_file; stale values are rejected with 409",
        },
        expected_version: {
          type: "string",
          description: "Optional opaque `version` returned by akb_get_file; stale values are rejected with 409",
        },
        mime_type: {
          type: "string",
          description: "Optional replacement MIME type. The existing file type is preserved when omitted.",
        },
      },
      required: ["uri", "file_path"],
    },
  },
  {
    name: "akb_delete_file",
    description: "Delete a file from vault storage by its URI.",
    inputSchema: {
      type: "object",
      properties: {
        uri: {
          type: "string",
          description: "File URI (akb://{vault}/file/{id})",
        },
      },
      required: ["uri"],
    },
  },
];

// Parse an akb://{vault}/file/{id} URI into (vault, id). Throws if malformed.
function parseFileUri(uri) {
  if (typeof uri !== "string") throw new Error("uri must be a string");
  // 0.3.0 location-aware form: optional `/coll/<path>` segment
  // between the vault and `/file/`. Both root and in-collection
  // file URIs are accepted; the `id` (UUID) is what we use to
  // address the file in subsequent /confirm / /download calls.
  const collMatch = uri.match(/^akb:\/\/([^/]+)\/coll\/[^/]+(?:\/[^/]+)*\/file\/(.+)$/);
  if (collMatch) {
    return { vault: collMatch[1], id: collMatch[2] };
  }
  const rootMatch = uri.match(/^akb:\/\/([^/]+)\/file\/(.+)$/);
  if (!rootMatch) {
    throw new Error(
      `Invalid file URI: '${uri}'. Expected akb://<vault>[/coll/<path>]/file/<uuid>.`,
    );
  }
  return { vault: rootMatch[1], id: rootMatch[2] };
}

const FILE_TOOL_NAMES = new Set(FILE_TOOLS.map((t) => t.name));
const FILE_WRITE_TOOL_NAMES = new Set([
  "akb_put_file",
  "akb_put_image",
  "akb_discard_image",
  "akb_update_file",
  "akb_delete_file",
]);

// Tools where proxy injects a `file` param as alternative to `content`
const FILE_CONTENT_TOOLS = new Set(["akb_put", "akb_update"]);

// Kept in sync with package.json `version`. Reported to the client in the
// local `initialize` response, so it must not silently drift on a proxy
// behaviour change. There is no import of package.json here to keep lib/
// zero-dependency and load-safe across Node versions.
const PROXY_VERSION = "2.2.1";
const PROXY_INSTRUCTIONS =
  "This akb-mcp proxy provides local-file tools in addition to the AKB backend. " +
  "A first write may return vault_skill_required before any mutation; apply its " +
  "vault_skill payload and retry the same call. " +
  "For an inline document image, call akb_put_image and place its returned `markdown` " +
  "with akb_put for a new document or a targeted akb_edit for an existing one. " +
  "Never pass only an image fragment to akb_update(content=...), because it replaces " +
  "the entire document body. If the document write fails, clean up the uncommitted " +
  "upload with akb_discard_image.";

// Fallback MCP protocol version echoed to the client when its `initialize`
// request omits one. We otherwise echo the client's requested version.
const MCP_PROTOCOL_VERSION = "2025-06-18";

export class AKBProxy {
  constructor({ url, pat, insecure = false }) {
    this.url = new URL(url);
    this.pat = pat;
    this.insecure = insecure;
    this.sessionId = null;
    this.msgId = 0;
    this._initialized = false;
    // ── Backend-liveness state (decoupled from client liveness) ──────
    // The client's view of this server must NOT depend on backend
    // reachability: a stdio MCP server that fails `initialize` is dropped
    // by the client for the whole session (the VPN-down-at-startup bug).
    // So we answer `initialize` locally and manage the backend session
    // out of band via a background monitor.
    this._clientInitParams = null; // client initialize params, replayed to backend
    this._backendReady = false; // backend MCP session established
    this._connecting = null; // in-flight backend-connect promise (single-flight lock)
    this._cachedTools = null; // last successful backend tools/list result (raw)
    this._servedDegraded = false; // client was handed a fallback (file-tools-only) list
    this._monitorRunning = false; // background reconnect monitor active
    this._closed = false; // stdin closed / shutting down
  }

  _sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // Short timeout for backend liveness probes (initialize / tools-list) so
  // the reconnect monitor cycles quickly and a single tool call fails fast
  // when the backend is unreachable, instead of blocking on the generous
  // per-request timeout used for legitimately slow operations.
  _probeTimeoutMs() {
    return Number(process.env.AKB_MCP_CONNECT_TIMEOUT_MS) || 10000;
  }

  async start() {
    const rl = createInterface({ input: process.stdin });

    for await (const line of rl) {
      const trimmed = line.trim();
      if (!trimmed) continue;

      let msg;
      try {
        msg = JSON.parse(trimmed);
      } catch {
        this._writeError(null, -32700, "Parse error");
        continue;
      }

      try {
        const result = await this._handle(msg);
        if (result !== null) {
          this._write(result);
        }
      } catch (err) {
        this._writeError(msg.id, -32603, err.message);
      }
    }
    // stdin closed → client disconnected. Stop the background monitor so
    // the process can exit cleanly instead of looping forever.
    this._closed = true;
  }

  async _handle(msg) {
    // Normalize every inbound string to NFC before anything else sees it.
    // macOS-sourced paths enter here as NFD; letting them reach the
    // backend poisons the search index (see nfcDeep note).
    if (msg && typeof msg === "object" && msg.params !== undefined) {
      msg = { ...msg, params: nfcDeep(msg.params) };
    }

    const { method, id, params } = msg;

    if (method === "initialize") {
      return await this._initialize(id, params);
    }

    if (id === undefined || id === null) {
      return null;
    }

    if (method === "tools/list") {
      return await this._toolsList(id, params);
    }

    if (method === "tools/call" && FILE_TOOL_NAMES.has(params?.name)) {
      return await this._handleFileTool(id, params);
    }

    // Resolve `file` → `content` for akb_put / akb_update before forwarding
    if (method === "tools/call" && FILE_CONTENT_TOOLS.has(params?.name)) {
      const args = params.arguments;
      if (args?.file) {
        try {
          msg = {
            ...msg,
            params: {
              ...params,
              arguments: this._resolveFileToContent(args),
            },
          };
        } catch (err) {
          return {
            jsonrpc: "2.0",
            id,
            result: {
              content: [{ type: "text", text: JSON.stringify({ error: err.message }) }],
              isError: false,
            },
          };
        }
      }
    }

    return await this._forward(msg);
  }

  async _initialize(id, params) {
    // Answer the handshake LOCALLY — never round-trip to the backend here.
    // This is the crux of the reconnect fix: the client considers this MCP
    // server initialized (and keeps it registered for the whole session)
    // regardless of whether the backend is reachable right now. If the VPN
    // is down at startup, the server still registers; tools recover once
    // connectivity returns (see the monitor + tools/list_changed path).
    this._clientInitParams = params || null;
    this._initialized = true;
    // Kick off the backend session + tool prefetch in the background.
    this._startBackendMonitor();

    const protocolVersion =
      (params && typeof params.protocolVersion === "string" && params.protocolVersion) ||
      MCP_PROTOCOL_VERSION;
    return {
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion,
        // Advertise listChanged so we can push the real toolset after a
        // degraded (backend-unreachable) tools/list is recovered.
        capabilities: { tools: { listChanged: true } },
        serverInfo: { name: "akb-mcp", version: PROXY_VERSION },
        instructions: PROXY_INSTRUCTIONS,
      },
    };
  }

  // Decorate a raw backend tools/list result with the proxy-local file
  // tools and the injected `file` param. Never mutates the cached result.
  _decorateTools(resp) {
    const tools = (resp.tools || []).map((t) => {
      if (FILE_CONTENT_TOOLS.has(t.name) && t.inputSchema?.properties) {
        return {
          ...t,
          inputSchema: {
            ...t.inputSchema,
            properties: {
              ...t.inputSchema.properties,
              file: {
                type: "string",
                description:
                  "Local file path to read as document body (alternative to content). " +
                  "Provide either file or content, not both.",
              },
            },
          },
        };
      }
      return { ...t };
    });
    tools.push(...FILE_TOOLS);
    return { ...resp, tools };
  }

  async _toolsList(id, params) {
    // Serve a live or cached backend tool list. If the backend is
    // unreachable and nothing is cached, degrade to the local file tools
    // only — a valid (partial) response the client can register — and mark
    // the list stale so the monitor re-lists it on recovery. Never error
    // the whole tools/list on backend unreachability.
    let resp = this._cachedTools;
    if (!resp) {
      const ok = await this._ensureBackend();
      if (ok) {
        try {
          resp = await this._syncTools();
        } catch {
          resp = null;
        }
      }
    }

    if (!resp) {
      this._servedDegraded = true;
      this._startBackendMonitor();
      process.stderr.write(
        "[akb-mcp] backend unreachable — serving file tools only; will re-list on recovery\n",
      );
      return { jsonrpc: "2.0", id, result: { tools: [...FILE_TOOLS] } };
    }

    return { jsonrpc: "2.0", id, result: this._decorateTools(resp) };
  }

  // ── File-to-content resolution ─────────────────────────

  /**
   * Read a local file and replace `file` with `content` in tool arguments.
   * Throws if both `file` and `content` are provided, or file is unreadable.
   */
  _resolveFileToContent(args) {
    const { file, content, ...rest } = args;
    if (!file) {
      throw new Error("'file' parameter is empty.");
    }
    if (content) {
      throw new Error("Cannot provide both 'file' and 'content'. Use one or the other.");
    }

    const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB
    let fileSize;
    try {
      fileSize = statSync(file).size;
    } catch (err) {
      throw new Error(`Cannot read file: ${file} (${err.message})`);
    }
    if (fileSize > MAX_FILE_SIZE) {
      throw new Error(`File too large: ${(fileSize / 1024 / 1024).toFixed(1)}MB (max ${MAX_FILE_SIZE / 1024 / 1024}MB). Use akb_put_file for binary/large files.`);
    }

    return { ...rest, content: readFileSync(file, "utf-8") };
  }

  // ── File tool handlers ──────────────────────────────────

  async _handleFileTool(id, params) {
    const { name, arguments: args } = params;
    try {
      const vault = this._fileToolVault(name, args);
      const vaultSkill = await this._fileToolSkillPreflight(vault);
      if (vaultSkill && FILE_WRITE_TOOL_NAMES.has(name)) {
        return {
          jsonrpc: "2.0",
          id,
          result: {
            content: [{
              type: "text",
              text: JSON.stringify({
                error: "Apply the vault instructions, then retry this write.",
                code: "vault_skill_required",
                retryable: true,
                vault_skill: vaultSkill,
              }),
            }],
            isError: false,
          },
        };
      }
      let result;
      switch (name) {
        case "akb_put_file":
          result = await this._putFile(args);
          break;
        case "akb_put_image":
          result = await this._putImage(args);
          break;
        case "akb_discard_image":
          result = await this._discardImage(args);
          break;
        case "akb_get_file":
          result = await this._getFile(args);
          break;
        case "akb_update_file":
          result = await this._updateFile(args);
          break;
        case "akb_delete_file":
          result = await this._deleteFile(args);
          break;
      }
      return {
        jsonrpc: "2.0",
        id,
        result: {
          content: [{
            type: "text",
            text: JSON.stringify(
              vaultSkill ? { ...result, vault_skill: vaultSkill } : result,
            ),
          }],
          isError: false,
        },
      };
    } catch (err) {
      return {
        jsonrpc: "2.0",
        id,
        result: {
          content: [
            { type: "text", text: JSON.stringify({ error: err.message }) },
          ],
          isError: false,
        },
      };
    }
  }

  _fileToolVault(name, args) {
    if (name === "akb_put_file" || name === "akb_put_image" || name === "akb_discard_image") {
      return _resolveParent(args).vault;
    }
    return parseFileUri(args.uri).vault;
  }

  /**
   * Touch the backend MCP session before a proxy-local file operation.
   *
   * Local tools bypass the backend `call_tool` dispatcher, so without this
   * bridge their first write could commit before the session received the
   * vault guide. `akb_help` is a reader-authorized, mirror-aware backend call
   * and therefore reuses the same version/session gate as every native tool.
   * A write-only credential receives no payload and remains usable.
   */
  async _fileToolSkillPreflight(vault) {
    if (!vault) return null;
    const ok = await this._ensureBackend();
    if (!ok) throw new Error("backend unreachable");
    const response = await this._rpc("tools/call", {
      name: "akb_help",
      arguments: { topic: "vault-skill", vault },
    });
    const text = response.content?.find((item) => item?.type === "text")?.text;
    if (!text) return null;
    try {
      const envelope = JSON.parse(text);
      return envelope?.vault_skill || null;
    } catch {
      return null;
    }
  }

  async _putFile(args) {
    const { file_path, description = "" } = args;
    // Resolve placement: either `parent` URI (vault root or coll URI)
    // or legacy `vault` + `collection`. Mirrors the backend's
    // `_resolve_parent` helper for akb_put / akb_create_table so the
    // three write tools accept the same shape.
    const { vault, collection } = _resolveParent(args);
    if (!file_path) throw new Error("file_path required");
    if (!vault) throw new Error(
      "Either `parent` (akb:// URI) or `vault` is required to upload a file."
    );

    const filename = basename(file_path);
    let fileSize;
    try {
      fileSize = statSync(file_path).size;
    } catch {
      throw new Error(`File not found: ${file_path}`);
    }

    // Resolve MIME type: explicit override wins, otherwise guess from extension.
    const mimeType = args.mime_type || guessMime(filename);
    const contentHash = await this._sha256File(file_path);

    // 1. Get presigned upload URL from AKB. The backend returns the
    //    canonical `uri` plus a transient `s3_key` + presigned upload
    //    URL. We parse the URI to recover the file UUID (needed only
    //    for the internal `/confirm` round-trip below); it never
    //    leaves this function.
    const params = new URLSearchParams({
      filename,
      collection,
      description,
      mime_type: mimeType,
    });
    const initResp = await this._http(
      "POST",
      `/api/v1/files/${encodeURIComponent(vault)}/upload?${params}`,
    );
    const initBody = JSON.parse(initResp.text);
    const { uri, upload_url } = initBody;
    const { id: fileId } = parseFileUri(uri);

    // 2. Upload directly to S3 via presigned URL (streaming).
    //    Content-Type MUST match the mime_type sent to /upload above, since
    //    boto3 generate_presigned_url includes it in X-Amz-SignedHeaders.
    await this._uploadToS3(upload_url, file_path, fileSize, mimeType);

    // 3. Confirm upload with AKB.
    const confirmResp = await this._http(
      "POST",
      `/api/v1/files/${encodeURIComponent(vault)}/${fileId}/confirm?` +
        new URLSearchParams({
          content_hash: contentHash,
          hash_algorithm: "sha256",
        }),
    );
    return JSON.parse(confirmResp.text);
  }

  async _putImage(args) {
    const { vault } = _resolveParent(args);
    const { file_path: filePath } = args;
    if (!filePath) throw new Error("file_path required");
    if (!vault) {
      throw new Error(
        "Either `parent` (akb:// URI) or `vault` is required to upload an image.",
      );
    }

    const filename = basename(filePath);
    let fileStat;
    try {
      fileStat = statSync(filePath);
    } catch {
      throw new Error(`Image file not found: ${filePath}`);
    }
    if (!fileStat.isFile()) throw new Error(`Image path is not a regular file: ${filePath}`);
    if (fileStat.size < 1) throw new Error("Image is empty.");
    if (fileStat.size > DOCUMENT_IMAGE_MAX_BYTES) {
      throw new Error(
        `Image too large: ${(fileStat.size / 1024 / 1024).toFixed(1)}MB (max 10MB).`,
      );
    }

    const mimeType = args.mime_type || guessMime(filename);
    if (!DOCUMENT_IMAGE_MIMES.has(mimeType)) {
      throw new Error("Document images must be PNG, JPEG, GIF, or WebP.");
    }

    // The backend intentionally receives the complete bounded byte string: it
    // decodes the image, verifies MIME/dimensions/frame limits, and writes a
    // hidden vault attachment. Unlike akb_put_file, no presigned S3 URL is
    // exposed and no unverified object can become a Markdown image.
    const response = await this._http(
      "POST",
      `/api/v1/assets/${encodeURIComponent(vault)}?` +
        new URLSearchParams({ filename }),
      readFileSync(filePath),
      { "Content-Type": mimeType },
    );
    const asset = JSON.parse(response.text);
    const assetId = parseAssetUrl(asset.url);
    if (asset.id !== assetId) {
      throw new Error("AKB returned inconsistent document image identifiers.");
    }

    const defaultAlt = filename.slice(0, filename.length - extname(filename).length);
    const altText = markdownAltText(args.alt_text ?? defaultAlt);
    return {
      kind: "document_image",
      vault,
      url: asset.url,
      markdown: `![${altText}](${asset.url})`,
      name: asset.name,
      mime_type: asset.mime_type,
      size_bytes: asset.size_bytes,
      width: asset.width,
      height: asset.height,
    };
  }

  async _discardImage(args) {
    const { vault } = _resolveParent(args);
    if (!vault) {
      throw new Error(
        "Either `parent` (akb:// URI) or `vault` is required to discard an image.",
      );
    }
    if (!args.url) throw new Error("url required");
    const assetId = parseAssetUrl(args.url);
    const response = await this._http(
      "DELETE",
      `/api/v1/assets/${encodeURIComponent(vault)}/${encodeURIComponent(assetId)}`,
    );
    let discarded = null;
    const responseText = response.text?.trim();
    if (responseText) {
      const body = JSON.parse(responseText);
      if (typeof body.discarded === "boolean") discarded = body.discarded;
    }
    return {
      kind: "document_image",
      vault,
      url: args.url,
      // null is possible only with an older backend that returned an empty
      // 204. Never claim deletion unless the backend confirmed the row change.
      discarded,
    };
  }

  async _getFile(args) {
    const { uri, save_to } = args;
    if (!uri || !save_to) throw new Error("uri and save_to required");
    const { vault, id: fileId } = parseFileUri(uri);

    // 1. Get presigned download URL from AKB
    const resp = await this._http(
      "GET",
      `/api/v1/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}/download`,
    );
    const {
      name: filename,
      download_url,
      size_bytes,
      content_hash,
      hash_algorithm,
      etag,
      storage_version,
      version,
    } = JSON.parse(resp.text);

    // 2. Determine save path
    let savePath = save_to;
    try {
      const s = await fsStat(save_to);
      if (s.isDirectory()) savePath = join(save_to, filename);
    } catch {
      // use as-is
    }

    // 3. Download directly from S3 (streaming to file)
    await mkdir(dirname(savePath), { recursive: true });
    const bytesWritten = await this._downloadFromS3(download_url, savePath);

    return {
      name: filename,
      save_to: savePath,
      size_bytes: bytesWritten,
      uri,
      content_hash,
      hash_algorithm,
      etag,
      storage_version,
      version,
    };
  }

  async _updateFile(args) {
    const { uri, file_path } = args;
    if (!uri || !file_path) throw new Error("uri and file_path required");
    const { vault, id: fileId } = parseFileUri(uri);

    let fileSize;
    try {
      fileSize = statSync(file_path).size;
    } catch {
      throw new Error(`File not found: ${file_path}`);
    }
    const contentHash = await this._sha256File(file_path);
    const initiateParams = new URLSearchParams({ content_hash: contentHash });
    if (args.mime_type) initiateParams.set("mime_type", args.mime_type);
    if (args.expected_content_hash) {
      initiateParams.set("expected_content_hash", args.expected_content_hash);
    }
    if (args.expected_version) {
      initiateParams.set("expected_version", args.expected_version);
    }

    const initiateResp = await this._http(
      "POST",
      `/api/v1/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}/replace?${initiateParams}`,
    );
    const initiated = JSON.parse(initiateResp.text);
    if (initiated.unchanged) return initiated;

    const { replacement_id, upload_url } = initiated;
    if (!replacement_id || !upload_url) {
      throw new Error("Invalid file replacement response: missing replacement_id or upload_url");
    }
    const uploadMimeType = initiated.mime_type || args.mime_type || "application/octet-stream";
    await this._uploadToS3(upload_url, file_path, fileSize, uploadMimeType);

    const confirmParams = new URLSearchParams({ content_hash: contentHash });
    if (args.expected_content_hash) {
      confirmParams.set("expected_content_hash", args.expected_content_hash);
    }
    if (args.expected_version) {
      confirmParams.set("expected_version", args.expected_version);
    }
    const confirmResp = await this._http(
      "POST",
      `/api/v1/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}` +
        `/replace/${encodeURIComponent(replacement_id)}/confirm?${confirmParams}`,
    );
    return JSON.parse(confirmResp.text);
  }

  async _deleteFile(args) {
    const { uri } = args;
    if (!uri) throw new Error("uri required");
    const { vault, id: fileId } = parseFileUri(uri);

    const resp = await this._http(
      "DELETE",
      `/api/v1/files/${encodeURIComponent(vault)}/${encodeURIComponent(fileId)}`,
    );
    return JSON.parse(resp.text);
  }

  // ── S3 direct transfer ────────────────────────────────────

  _sha256File(filePath) {
    return new Promise((resolve, reject) => {
      const hash = createHash("sha256");
      const stream = createReadStream(filePath);
      stream.on("data", (chunk) => hash.update(chunk));
      stream.on("end", () => resolve(hash.digest("hex")));
      stream.on("error", reject);
    });
  }

  /**
   * Stream a local file directly to S3 via presigned PUT URL.
   * Content-Type MUST match the mime_type that was signed into the presigned URL,
   * otherwise S3 rejects with SignatureDoesNotMatch.
   */
  _uploadToS3(presignedUrl, filePath, fileSize, contentType = "application/octet-stream") {
    return new Promise((resolve, reject) => {
      const url = new URL(presignedUrl);
      const isHttps = url.protocol === "https:";
      const doRequest = isHttps ? httpsRequest : httpRequest;

      const opts = {
        hostname: url.hostname,
        port: url.port || (isHttps ? 443 : 80),
        path: url.pathname + url.search,
        method: "PUT",
        headers: {
          "Content-Type": contentType,
          "Content-Length": fileSize,
        },
      };
      if (isHttps && this.insecure) opts.rejectUnauthorized = false;

      const req = doRequest(opts, (res) => {
        let data = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          if (res.statusCode >= 400) {
            reject(new Error(`S3 upload failed: HTTP ${res.statusCode} ${data.slice(0, 200)}`));
          } else {
            resolve();
          }
        });
      });

      req.on("error", reject);
      req.setTimeout(600000, () => req.destroy(new Error("S3 upload timeout")));

      const stream = createReadStream(filePath);
      stream.pipe(req);
      stream.on("error", (err) => req.destroy(err));
    });
  }

  /**
   * Stream a file directly from S3 via presigned GET URL to local disk.
   */
  _downloadFromS3(presignedUrl, savePath) {
    return new Promise((resolve, reject) => {
      const url = new URL(presignedUrl);
      const isHttps = url.protocol === "https:";
      const doRequest = isHttps ? httpsRequest : httpRequest;

      const opts = {
        hostname: url.hostname,
        port: url.port || (isHttps ? 443 : 80),
        path: url.pathname + url.search,
        method: "GET",
      };
      if (isHttps && this.insecure) opts.rejectUnauthorized = false;

      const req = doRequest(opts, (res) => {
        if (res.statusCode >= 400) {
          let data = "";
          res.setEncoding("utf8");
          res.on("data", (c) => (data += c));
          res.on("end", () =>
            reject(new Error(`S3 download failed: HTTP ${res.statusCode} ${data.slice(0, 200)}`))
          );
          return;
        }

        let bytesWritten = 0;
        const ws = createWriteStream(savePath);
        res.on("data", (chunk) => {
          bytesWritten += chunk.length;
          ws.write(chunk);
        });
        res.on("end", () => ws.end(() => resolve(bytesWritten)));
        res.on("error", reject);
        ws.on("error", reject);
      });

      req.on("error", reject);
      req.setTimeout(600000, () => req.destroy(new Error("S3 download timeout")));
      req.end();
    });
  }

  // ── AKB HTTP helper ───────────────────────────────────────

  _http(method, path, body = null, extraHeaders = {}, callOptions = {}) {
    return new Promise((resolve, reject) => {
      const isHttps = this.url.protocol === "https:";
      const doRequest = isHttps ? httpsRequest : httpRequest;

      const headers = {
        Authorization: `Bearer ${this.pat}`,
        ...extraHeaders,
      };
      if (body && !extraHeaders["Content-Type"]) {
        headers["Content-Type"] = "application/json";
      }
      if (body) {
        headers["Content-Length"] = Buffer.byteLength(body);
      }

      const requestOptions = {
        hostname: this.url.hostname,
        port: this.url.port || (isHttps ? 443 : 80),
        path,
        method,
        headers,
        agent: isHttps ? httpsKeepAlive : httpKeepAlive,
      };
      if (isHttps && this.insecure) requestOptions.rejectUnauthorized = false;

      // Connect-phase timeout, separate from the (long) response timeout.
      // A VPN blackhole leaves a brand-new socket stuck in `connecting`;
      // without this the request would hang until the 5-min response
      // timeout. We only arm it for sockets still connecting — reused
      // keep-alive sockets are already established.
      let connectTimer = null;
      const connectMs = this._probeTimeoutMs();
      const clearConnectTimer = () => {
        if (connectTimer) {
          clearTimeout(connectTimer);
          connectTimer = null;
        }
      };

      const req = doRequest(requestOptions, (res) => {
        clearConnectTimer();
        let data = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          if (res.statusCode >= 400) {
            const error = new Error(`HTTP ${res.statusCode}: ${data.slice(0, 300)}`);
            error.statusCode = res.statusCode;
            reject(error);
          } else {
            resolve({ text: data, headers: res.headers });
          }
        });
      });

      req.on("socket", (socket) => {
        if (socket.connecting) {
          connectTimer = setTimeout(() => {
            req.destroy(new Error(`Connect timeout (${Math.round(connectMs / 1000)}s)`));
          }, connectMs);
          socket.once("connect", clearConnectTimer);
        }
      });

      req.on("error", (err) => {
        clearConnectTimer();
        reject(err);
      });
      // Default 5 min — destructive ops like `akb_delete_vault` on
      // large vaults (7K+ docs) take well over 30s for the backend
      // cascade (chunks + vector outbox + git cleanup). Hardcoding
      // 30s caused the client to abort while the backend continued
      // processing, leaving the operator with a misleading timeout
      // error. Override via `AKB_MCP_REQUEST_TIMEOUT_MS`, or per-call via
      // `callOptions.timeoutMs` (liveness probes use the short probe timeout).
      const reqTimeoutMs =
        callOptions.timeoutMs || Number(process.env.AKB_MCP_REQUEST_TIMEOUT_MS) || 300000;
      req.setTimeout(reqTimeoutMs, () => req.destroy(new Error(`Request timeout (${Math.round(reqTimeoutMs / 1000)}s)`)));
      if (body) req.write(body);
      req.end();
    });
  }

  // ── Backend session + reconnect monitor ───────────────────

  // A connection/session failure that a background reconnect can recover
  // from — as opposed to a genuine application error we must surface.
  _isConnError(message) {
    return /session|404|ECONNREFUSED|ECONNRESET|socket hang up|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|ENETDOWN|ENOTFOUND|EPIPE|timeout|unreachable/i.test(
      message || "",
    );
  }

  // Establish the backend MCP session (the `initialize` handshake that
  // yields an mcp-session-id) if not already up. Single-flight: concurrent
  // callers share one in-flight attempt. Returns true on success, false on
  // failure — never throws — so callers can degrade gracefully.
  async _ensureBackend() {
    if (this._backendReady) return true;
    if (this._connecting) return this._connecting;
    this._connecting = (async () => {
      try {
        const initParams = this._clientInitParams || {
          protocolVersion: MCP_PROTOCOL_VERSION,
          capabilities: {},
          clientInfo: { name: "akb-mcp-client", version: PROXY_VERSION },
        };
        await this._rpc("initialize", initParams, { timeoutMs: this._probeTimeoutMs() });
        this._backendReady = true;
        return true;
      } catch {
        this._backendReady = false;
        return false;
      } finally {
        this._connecting = null;
      }
    })();
    return this._connecting;
  }

  // Fetch and cache the backend tool list. Caller is responsible for having
  // an established session. Uses the short probe timeout.
  async _syncTools() {
    const resp = await this._rpc("tools/list", {}, { timeoutMs: this._probeTimeoutMs() });
    this._cachedTools = resp;
    return resp;
  }

  // Background loop that keeps trying to (re)establish the backend session
  // with exponential backoff — effectively forever while the client is
  // connected. On (re)connect it refreshes the tool cache and, if the
  // client was previously handed a degraded list, pushes a
  // tools/list_changed notification so the full toolset reappears WITHOUT a
  // session restart. Idempotent: at most one loop runs at a time.
  _startBackendMonitor() {
    if (this._monitorRunning || this._closed) return;
    this._monitorRunning = true;

    const loop = async () => {
      let delay = 1000;
      const maxDelay = 30000;
      while (!this._closed) {
        const ok = await this._ensureBackend();
        if (ok) {
          try {
            await this._syncTools();
            if (this._servedDegraded) {
              this._servedDegraded = false;
              this._notify("notifications/tools/list_changed", {});
              process.stderr.write("[akb-mcp] backend recovered — re-listing tools\n");
            }
            break; // connected + synced; idle until a forward detects a drop
          } catch {
            // initialize succeeded but tools/list failed — treat as not
            // ready and keep retrying.
            this._backendReady = false;
          }
        }
        await this._sleep(delay);
        delay = Math.min(maxDelay, delay * 2);
      }
      this._monitorRunning = false;
    };

    loop().catch(() => {
      this._monitorRunning = false;
    });
  }

  // ── MCP RPC forwarding ────────────────────────────────────

  async _forward(msg) {
    const maxRetries = 2;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        if (!this._backendReady) {
          const ok = await this._ensureBackend();
          if (!ok) throw new Error("backend unreachable");
        }
        const resp = await this._rpc(msg.method, msg.params || {});
        return { jsonrpc: "2.0", id: msg.id, result: resp };
      } catch (err) {
        if (this._isConnError(err.message)) {
          // Drop the dead session and let the background monitor restore
          // it. The proxy process and the client-visible server stay
          // alive, so calls recover once connectivity returns.
          this._backendReady = false;
          this.sessionId = null;
          this._startBackendMonitor();

          if (attempt < maxRetries) {
            process.stderr.write(
              `[akb-mcp] backend unreachable, retrying (attempt ${attempt + 1})...\n`,
            );
            continue;
          }
        }
        throw err;
      }
    }
  }

  async _rpc(method, params, rpcOpts = {}) {
    this.msgId++;
    const body = JSON.stringify({
      jsonrpc: "2.0",
      id: this.msgId,
      method,
      params,
    });

    const headers = {
      Authorization: `Bearer ${this.pat}`,
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
    };
    if (this.sessionId) {
      headers["mcp-session-id"] = this.sessionId;
    }

    const resp = await this._http(
      "POST",
      this.url.pathname,
      Buffer.from(body),
      headers,
      { timeoutMs: rpcOpts.timeoutMs },
    );

    if (resp.headers["mcp-session-id"]) {
      this.sessionId = resp.headers["mcp-session-id"];
    }

    let parsed;
    try {
      parsed = JSON.parse(resp.text);
    } catch {
      throw new Error(`Invalid JSON response: ${resp.text.slice(0, 200)}`);
    }

    if (parsed._sessionId) {
      this.sessionId = parsed._sessionId;
    }
    if (parsed.error) {
      throw new Error(`MCP error ${parsed.error.code}: ${parsed.error.message}`);
    }
    return parsed.result || {};
  }

  _write(obj) {
    process.stdout.write(JSON.stringify(obj) + "\n");
  }

  // Emit a JSON-RPC notification (no id) to the client. Used for
  // notifications/tools/list_changed after a backend recovery.
  _notify(method, params) {
    this._write({ jsonrpc: "2.0", method, params: params || {} });
  }

  _writeError(id, code, message) {
    this._write({ jsonrpc: "2.0", id, error: { code, message } });
  }
}
