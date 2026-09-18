import type {
  MarkdownReferenceAdapter,
  MarkdownReferenceContext,
  MarkdownSearchAdapter,
  MarkdownSearchContext,
  MarkdownTargetKind,
  MarkdownTargetResolution,
  MarkdownUnavailableReason,
  MarkdownUploadContext,
} from "@akb/markdown-editor";
import {
  copyFileToAttachment,
  getAttachmentMetadata,
  getAttachmentRetentionPolicy,
  getAssetBlob,
  getDocument,
  getVaultFileDownloadUrl,
  searchDocs,
  uploadAsset,
} from "@/lib/api";
import {
  assetIdFromUrl,
  classifyEditorImageUploadFailure,
  prepareEditorImage,
} from "@/lib/image-assets";
import { docUri, fileUri, parseUri } from "@/lib/uri";

export interface AkbMarkdownUploadContext {
  vault: string;
  document?: string;
  commit?: string;
  draftId?: string;
  signal?: AbortSignal;
}

function unavailable(
  target: string,
  kind?: MarkdownTargetKind,
  reason: MarkdownUnavailableReason = "unknown",
): MarkdownTargetResolution {
  return { target, kind, status: "unavailable", reason };
}

/** Classify only the three targets owned by the Markdown resource contract. */
export function classifyAkbMarkdownTarget(target: string): MarkdownTargetKind | null {
  if (assetIdFromUrl(target)) return "attachment";
  const parsed = parseUri(target);
  if (parsed?.kind === "doc") return "document";
  if (parsed?.kind === "file") return "file";
  return null;
}

export function canonicalAkbMarkdownTarget(
  target: string,
  kind?: MarkdownTargetKind,
): string | null {
  const attachmentId = assetIdFromUrl(target);
  if (attachmentId && (!kind || kind === "attachment")) {
    return `/api/assets/${attachmentId}`;
  }
  const parsed = parseUri(target);
  if (!parsed || (kind && classifyAkbMarkdownTarget(target) !== kind)) return null;
  if (parsed.kind === "doc") return docUri(parsed.vault, parsed.id);
  if (parsed.kind === "file") return fileUri(parsed.vault, parsed.id, parsed.collection);
  return null;
}

function documentRuntimeUrl(vault: string, path: string, commit?: string): string {
  const query = commit ? `?commit=${encodeURIComponent(commit)}` : "";
  return `/vault/${encodeURIComponent(vault)}/doc/${encodeURIComponent(path)}${query}`;
}

export function createAkbMarkdownTargetResolver(
  defaults: Partial<AkbMarkdownUploadContext> = {},
) {
  return {
    async resolve(
      rawTarget: string,
      context: Partial<AkbMarkdownUploadContext> = {},
    ): Promise<MarkdownTargetResolution> {
      const target = canonicalAkbMarkdownTarget(rawTarget);
      const kind = classifyAkbMarkdownTarget(rawTarget) ?? undefined;
      if (!target || !kind) return unavailable(rawTarget, undefined, "unsupported");

      const vault = context.vault ?? defaults.vault;
      const parsed = parseUri(target);
      if (!vault) return unavailable(target, kind, "unknown");
      if (kind !== "attachment" && (!parsed || parsed.vault !== vault)) {
        return unavailable(
          target,
          kind,
          parsed && parsed.vault !== vault ? "cross-vault" : "unknown",
        );
      }

      try {
        if (kind === "document" && parsed?.kind === "doc") {
          await getDocument(vault, parsed.id, context.commit ?? defaults.commit);
          return {
            target,
            kind,
            status: "available",
            runtimeUrl: documentRuntimeUrl(vault, parsed.id, context.commit ?? defaults.commit),
          };
        }

        if (kind === "file" && parsed?.kind === "file") {
          const access = await getVaultFileDownloadUrl(vault, parsed.id);
          const expiresAt = typeof access.expires_in === "number" && access.expires_in > 0
            ? new Date(Date.now() + access.expires_in * 1000).toISOString()
            : undefined;
          return { target, kind, status: "available", runtimeUrl: access.download_url, expiresAt };
        }

        const attachmentId = assetIdFromUrl(target);
        if (attachmentId) {
          // The actual private-image request is made by AssetImage so it can
          // retain its bounded blob cache and historical source identity.
          // This resolver call verifies that the target has a usable runtime
          // address without placing a signed/blob URL in Markdown.
          const metadata = await getAttachmentMetadata(vault, attachmentId, {
            document: context.document ?? defaults.document,
            commit: context.commit ?? defaults.commit,
          });
          if (metadata.status === "expired") return unavailable(target, kind, "expired");
          const query = new URLSearchParams({ vault });
          if (context.document ?? defaults.document) {
            query.set("document", context.document ?? defaults.document!);
          }
          if (context.commit ?? defaults.commit) {
            query.set("commit", context.commit ?? defaults.commit!);
          }
          return {
            target,
            kind,
            status: "available",
            runtimeUrl: `/api/assets/${attachmentId}?${query}`,
          };
        }
      } catch {
        // The adapter deliberately collapses permission, absence, and remote
        // failures into one unavailable result. Callers retain the canonical
        // source text and therefore do not need an existence oracle.
        return unavailable(target, kind);
      }

      return unavailable(target, kind);
    },
  };
}

export function createAkbMarkdownAdapters(defaults: AkbMarkdownUploadContext) {
  const targetResolver = createAkbMarkdownTargetResolver(defaults);
  const search: MarkdownSearchAdapter = {
    async search(query: string, context?: MarkdownSearchContext) {
      const vault = context?.vault ?? defaults.vault;
      const response = await searchDocs(query, vault, 20, {}, { signal: context?.signal });
      // Degradation only hides a genuine zero-match when it left nothing to
      // show. A degraded response that still carries hits ran on one
      // retrieval leg alone: the hits are real, only the ranking is partial,
      // and discarding them costs most exactly when the backend kept them.
      if (response.degraded && !response.results?.length) {
        throw new Error("Search results are temporarily unavailable.");
      }
      return response.results
        .map((result) => {
          const target = typeof result?.uri === "string" ? canonicalAkbMarkdownTarget(result.uri) : null;
          if (!target) return null;
          const kind = classifyAkbMarkdownTarget(target);
          const parsed = parseUri(target);
          if ((kind !== "document" && kind !== "file") || parsed?.vault !== vault) return null;
          return {
            id: target,
            title: typeof result.title === "string" ? result.title : target,
            snippet: typeof result.matched_section === "string" ? result.matched_section : undefined,
            target,
            kind: kind ?? undefined,
          };
        })
        .filter((result): result is NonNullable<typeof result> => result !== null);
    },
  };
  const reference: MarkdownReferenceAdapter = {
    async search(query: string, context?: MarkdownReferenceContext) {
      const results = await search.search(query, context);
      return results
        .filter(
          (result): result is typeof result & { kind: "document" | "file" } =>
            result.kind === "document" || result.kind === "file",
        )
        .map(({ id, title, snippet, target, kind }) => ({
          id,
          title,
          snippet,
          target,
          kind,
        }));
    },
  };
  return {
    upload: {
      async upload(file: Blob, context?: MarkdownUploadContext) {
        const vault = context?.vault ?? defaults.vault;
        const isFile = typeof File !== "undefined" && file instanceof File;
        const name = isFile ? (file as File).name : "attachment";
        const uploadFile = isFile
          ? (file as File)
          : new File([file], name, { type: file.type || "application/octet-stream" });
        let prepared: File;
        try {
          prepared = (await prepareEditorImage(uploadFile)).file;
        } catch (error) {
          const failure = classifyEditorImageUploadFailure(error, uploadFile);
          throw Object.assign(new Error(failure.message), {
            code: "invalid",
            retryable: failure.retryable,
          });
        }

        let uploaded: Awaited<ReturnType<typeof uploadAsset>>;
        try {
          uploaded = await uploadAsset(vault, prepared, context?.signal);
        } catch (error) {
          if (error instanceof DOMException && error.name === "AbortError") throw error;
          const failure = classifyEditorImageUploadFailure(error, uploadFile);
          throw Object.assign(new Error(failure.message), {
            code: failure.retryable ? "unavailable" : "invalid",
            retryable: failure.retryable,
          });
        }

        const target = canonicalAkbMarkdownTarget(
          uploaded.target ?? uploaded.url,
          "attachment",
        );
        if (!target) {
          throw Object.assign(new Error("The image upload returned an invalid asset URL."), {
            code: "invalid",
            retryable: false,
          });
        }
        return {
          kind: "attachment" as const,
          id: uploaded.id,
          target,
          alt: name.replace(/\.[^.]+$/, "") || "Image",
          expiresAt: uploaded.unclaimed_expires_at ?? undefined,
        };
      },
    },
    search,
    reference,
    targetResolver,
    /** Explicit action for embedding an existing standalone image File. */
    copyFileToAttachment: (fileId: string) => copyFileToAttachment(defaults.vault, fileId),
    getRetentionPolicy: () => getAttachmentRetentionPolicy(defaults.vault),
    /** Kept as an adapter-level capability for consumers that need the bytes. */
    getAttachmentBlob: (fileId: string, signal?: AbortSignal) =>
      getAssetBlob(fileId, defaults.vault, signal),
  };
}
