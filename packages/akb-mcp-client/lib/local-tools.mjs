export const VAULT_SKILL_ACK_ARGUMENT = "_vault_skill_ack";

export function vaultSkillAckProperty() {
  return {
    type: "string",
    maxLength: 128,
    description:
      "Opaque acknowledgement from vault_skill.ack_token. Apply the guide, then retry " +
      "the unchanged operation with this value. The bundled proxy fills it on an exact retry.",
  };
}
export const FILE_TOOLS = [
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

export const FILE_TOOL_NAMES = new Set(FILE_TOOLS.map((t) => t.name));
export const FILE_WRITE_TOOL_NAMES = new Set([
  "akb_put_file",
  "akb_put_image",
  "akb_discard_image",
  "akb_update_file",
  "akb_delete_file",
]);

export const FILE_CONTENT_TOOLS = new Set(["akb_put", "akb_update"]);
