/**
 * AKB MCP Proxy — stdio ↔ Streamable HTTP with auto-reconnect.
 *
 * - Reads JSON-RPC from stdin, forwards to AKB server over HTTP
 * - Handles file tools locally:
 *   - Gets presigned URLs from AKB server
 *   - Uploads/downloads directly to/from S3 (AKB never touches file bytes)
 * - Auto-reconnects on server restart
 * - Zero dependencies (Node.js built-in only)
 */

import { request as httpsRequest } from "node:https";
import { request as httpRequest } from "node:http";
import { createInterface } from "node:readline";
import { createReadStream, createWriteStream, readFileSync, statSync } from "node:fs";
import { mkdir, stat as fsStat } from "node:fs/promises";
import { basename, dirname, join } from "node:path";

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

// ── File tool definitions (injected into tools/list) ────────

const FILE_TOOLS = [
  {
    name: "akb_put_file",
    description:
      "Upload a local file to a vault's file storage (S3-backed). Use for PDFs, images, datasets, or any binary content too large for akb_put. MIME type is auto-detected from the filename extension unless overridden.",
    inputSchema: {
      type: "object",
      properties: {
        vault: { type: "string", description: "Vault name" },
        file_path: {
          type: "string",
          description: "Absolute path to the local file to upload",
        },
        collection: {
          type: "string",
          description: "Logical grouping (like document collections)",
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
      required: ["vault", "file_path"],
    },
  },
  {
    name: "akb_get_file",
    description: "Download a file from vault storage to a local path.",
    inputSchema: {
      type: "object",
      properties: {
        vault: { type: "string", description: "Vault name" },
        file_id: {
          type: "string",
          description: "File ID (from akb_browse)",
        },
        save_to: {
          type: "string",
          description: "Local directory or file path to save to",
        },
      },
      required: ["vault", "file_id", "save_to"],
    },
  },
  {
    name: "akb_delete_file",
    description: "Delete a file from vault storage.",
    inputSchema: {
      type: "object",
      properties: {
        vault: { type: "string", description: "Vault name" },
        file_id: {
          type: "string",
          description: "File ID to delete",
        },
      },
      required: ["vault", "file_id"],
    },
  },
];

const FILE_TOOL_NAMES = new Set(FILE_TOOLS.map((t) => t.name));

// Tools where proxy injects a `file` param as alternative to `content`
const FILE_CONTENT_TOOLS = new Set(["akb_put", "akb_update"]);

export class AKBProxy {
  constructor({ url, pat, insecure = false }) {
    this.url = new URL(url);
    this.pat = pat;
    this.insecure = insecure;
    this.sessionId = null;
    this.msgId = 0;
    this._initialized = false;
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
    const resp = await this._rpc("initialize", params);
    this._initialized = true;
    return { jsonrpc: "2.0", id, result: resp };
  }

  async _toolsList(id, params) {
    const resp = await this._rpc("tools/list", params || {});
    const tools = resp.tools || [];

    // Inject `file` param into tools that support local file → content resolution
    for (const tool of tools) {
      if (FILE_CONTENT_TOOLS.has(tool.name) && tool.inputSchema?.properties) {
        tool.inputSchema.properties.file = {
          type: "string",
          description:
            "Local file path to read as document body (alternative to content). " +
            "Provide either file or content, not both.",
        };
      }
    }

    tools.push(...FILE_TOOLS);
    return { jsonrpc: "2.0", id, result: { ...resp, tools } };
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
      let result;
      switch (name) {
        case "akb_put_file":
          result = await this._putFile(args);
          break;
        case "akb_get_file":
          result = await this._getFile(args);
          break;
        case "akb_delete_file":
          result = await this._deleteFile(args);
          break;
      }
      return {
        jsonrpc: "2.0",
        id,
        result: {
          content: [{ type: "text", text: JSON.stringify(result) }],
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

  async _putFile(args) {
    const { vault, file_path, collection = "", description = "" } = args;
    if (!vault || !file_path) throw new Error("vault and file_path required");

    const filename = basename(file_path);
    let fileSize;
    try {
      fileSize = statSync(file_path).size;
    } catch {
      throw new Error(`File not found: ${file_path}`);
    }

    // Resolve MIME type: explicit override wins, otherwise guess from extension.
    const mimeType = args.mime_type || guessMime(filename);

    // 1. Get presigned upload URL from AKB (mime_type is signed into the URL,
    //    so the S3 PUT below must use the same Content-Type header).
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
    const { id: fileId, upload_url } = JSON.parse(initResp.text);

    // 2. Upload directly to S3 via presigned URL (streaming).
    //    Content-Type MUST match the mime_type sent to /upload above, since
    //    boto3 generate_presigned_url includes it in X-Amz-SignedHeaders.
    await this._uploadToS3(upload_url, file_path, fileSize, mimeType);

    // 3. Confirm upload with AKB
    const confirmResp = await this._http(
      "POST",
      `/api/v1/files/${encodeURIComponent(vault)}/${fileId}/confirm`,
    );
    return JSON.parse(confirmResp.text);
  }

  async _getFile(args) {
    const { vault, file_id, save_to } = args;
    if (!vault || !file_id || !save_to)
      throw new Error("vault, file_id, and save_to required");

    // 1. Get presigned download URL from AKB
    const resp = await this._http(
      "GET",
      `/api/v1/files/${encodeURIComponent(vault)}/${encodeURIComponent(file_id)}/download`,
    );
    const { name: filename, download_url, size_bytes } = JSON.parse(resp.text);

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

    return { name: filename, save_to: savePath, size_bytes: bytesWritten };
  }

  async _deleteFile(args) {
    const { vault, file_id } = args;
    if (!vault || !file_id) throw new Error("vault and file_id required");

    const resp = await this._http(
      "DELETE",
      `/api/v1/files/${encodeURIComponent(vault)}/${encodeURIComponent(file_id)}`,
    );
    return JSON.parse(resp.text);
  }

  // ── S3 direct transfer ────────────────────────────────────

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

  _http(method, path, body = null, extraHeaders = {}) {
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

      const opts = {
        hostname: this.url.hostname,
        port: this.url.port || (isHttps ? 443 : 80),
        path,
        method,
        headers,
      };
      if (isHttps && this.insecure) opts.rejectUnauthorized = false;

      const req = doRequest(opts, (res) => {
        let data = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          if (res.statusCode >= 400) {
            reject(new Error(`HTTP ${res.statusCode}: ${data.slice(0, 300)}`));
          } else {
            resolve({ text: data, headers: res.headers });
          }
        });
      });

      req.on("error", reject);
      req.setTimeout(30000, () => req.destroy(new Error("Request timeout (30s)")));
      if (body) req.write(body);
      req.end();
    });
  }

  // ── MCP RPC forwarding ────────────────────────────────────

  async _forward(msg) {
    const maxRetries = 2;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        const resp = await this._rpc(msg.method, msg.params || {});
        return { jsonrpc: "2.0", id: msg.id, result: resp };
      } catch (err) {
        const isSessionError =
          err.message.includes("session") ||
          err.message.includes("Session") ||
          err.message.includes("404") ||
          err.message.includes("ECONNREFUSED") ||
          err.message.includes("ECONNRESET") ||
          err.message.includes("socket hang up");

        if (isSessionError && attempt < maxRetries) {
          process.stderr.write(
            `[akb-mcp] Connection lost, reconnecting (attempt ${attempt + 1})...\n`
          );
          this.sessionId = null;
          this._initialized = false;

          try {
            await this._rpc("initialize", {
              protocolVersion: "2025-03-26",
              capabilities: {},
              clientInfo: { name: "akb-mcp-client", version: "0.6.0" },
            });
            this._initialized = true;
            process.stderr.write("[akb-mcp] Reconnected.\n");
            continue;
          } catch (reconnectErr) {
            process.stderr.write(
              `[akb-mcp] Reconnect failed: ${reconnectErr.message}\n`
            );
          }
        }
        throw err;
      }
    }
  }

  async _rpc(method, params) {
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

    const resp = await this._http("POST", this.url.pathname, Buffer.from(body), headers);

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

  _writeError(id, code, message) {
    this._write({ jsonrpc: "2.0", id, error: { code, message } });
  }
}
