import { useEffect, useMemo, useState } from "react";
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
import { assetIdFromUrl } from "@/lib/image-assets";
import { docUri, fileUri, parseUri } from "@/lib/uri";

export type AkbMarkdownTargetKind = "document" | "file" | "attachment";

export type AkbMarkdownTargetResolution =
  | {
      target: string;
      kind?: AkbMarkdownTargetKind;
      status: "available";
      runtimeUrl: string;
      label?: string;
      expiresAt?: string;
    }
  | {
      target: string;
      kind?: AkbMarkdownTargetKind;
      status: "unavailable";
      reason: "inaccessible" | "deleted" | "unsupported" | "cross-vault" | "expired" | "unknown";
      label?: string;
    };

export interface AkbMarkdownUploadContext {
  vault: string;
  document?: string;
  commit?: string;
  draftId?: string;
  signal?: AbortSignal;
}

function unavailable(
  target: string,
  kind?: AkbMarkdownTargetKind,
  reason: "inaccessible" | "deleted" | "unsupported" | "cross-vault" | "expired" | "unknown" = "unknown",
): AkbMarkdownTargetResolution {
  return { target, kind, status: "unavailable", reason };
}

/** Classify only the three targets owned by the Markdown resource contract. */
export function classifyAkbMarkdownTarget(target: string): AkbMarkdownTargetKind | null {
  if (assetIdFromUrl(target)) return "attachment";
  const parsed = parseUri(target);
  if (parsed?.kind === "doc") return "document";
  if (parsed?.kind === "file") return "file";
  return null;
}

export function canonicalAkbMarkdownTarget(
  target: string,
  kind?: AkbMarkdownTargetKind,
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

/** Extract only link targets that need a resource resolver; image attachments
 * keep their own bounded byte loader and are intentionally not duplicated. */
export function extractAkbMarkdownLinkTargets(markdown: string): string[] {
  const targets: string[] = [];
  const seen = new Set<string>();
  const source = markdown.replace(/```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`/g, " ");
  const normalizeReferenceLabel = (label: string) => label.trim().replace(/\s+/g, " ").toLowerCase();
  const add = (raw: string) => {
    const target = canonicalAkbMarkdownTarget(raw.trim());
    if (!target || classifyAkbMarkdownTarget(target) === "attachment" || seen.has(target)) return;
    seen.add(target);
    targets.push(target);
  };
  const definitions = new Map<string, string>();
  for (const match of source.matchAll(/^\s{0,3}\[([^\]]+)\]:\s*(<[^>]+>|[^\s]+).*$/gm)) {
    definitions.set(normalizeReferenceLabel(match[1]), (match[2] ?? "").replace(/^<|>$/g, ""));
  }

  const pattern = /!?\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/g;
  for (const match of source.matchAll(pattern)) {
    const raw = (match[1] ?? "").replace(/^<|>$/g, "");
    add(raw);
  }
  for (const match of source.matchAll(/!?\[([^\]]*)\]\[([^\]]*)\]/g)) {
    const label = normalizeReferenceLabel(match[2] || match[1]);
    const target = definitions.get(label);
    if (target) add(target);
  }
  // CommonMark shortcut references use one bracket pair: `[Guide]`, with a
  // matching `[Guide]: target` definition. Skip images, inline links, full /
  // collapsed references, and the definition declaration itself.
  for (const match of source.matchAll(/\[([^\]]+)\](?![ \t]*(?:\[|\(|:))/g)) {
    const index = match.index ?? 0;
    if (index > 0 && source[index - 1] === "!") continue;
    const target = definitions.get(normalizeReferenceLabel(match[1]));
    if (target) add(target);
  }
  return targets;
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
    ): Promise<AkbMarkdownTargetResolution> {
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

const EMPTY_TARGET_RESOLUTIONS: ReadonlyMap<string, AkbMarkdownTargetResolution> = new Map();

/** Resolve editor/viewer links and refresh short-lived runtime URLs in place. */
export function useAkbMarkdownTargetResolutions(
  markdown: string,
  defaults: Partial<AkbMarkdownUploadContext> = {},
): ReadonlyMap<string, AkbMarkdownTargetResolution> {
  const { commit, document, vault } = defaults;
  const targetStrings = useMemo(
    () => extractAkbMarkdownLinkTargets(markdown),
    [markdown],
  );
  const targetResolver = useMemo(
    () => (vault ? createAkbMarkdownTargetResolver({ vault, document, commit }) : undefined),
    [commit, document, vault],
  );
  const targetResolutionKey = useMemo(
    () => [vault ?? "", document ?? "", commit ?? "", ...targetStrings].join("\u0000"),
    [commit, document, targetStrings, vault],
  );
  const [targetResolutionState, setTargetResolutionState] = useState<{
    key: string;
    values: ReadonlyMap<string, AkbMarkdownTargetResolution>;
  }>({ key: "", values: EMPTY_TARGET_RESOLUTIONS });
  const [refreshNonce, setRefreshNonce] = useState(0);

  useEffect(() => {
    if (!targetResolver || targetStrings.length === 0) return;
    const controller = new AbortController();
    void Promise.all(
      targetStrings.map(async (target) => [
        target,
        await targetResolver.resolve(target, {
          vault,
          document,
          commit,
          signal: controller.signal,
        }),
      ] as const),
    ).then((entries) => {
      if (!controller.signal.aborted) {
        setTargetResolutionState({ key: targetResolutionKey, values: new Map(entries) });
      }
    });
    return () => controller.abort();
  }, [commit, document, refreshNonce, targetResolutionKey, targetResolver, targetStrings, vault]);

  const targetResolutions =
    targetResolver && targetResolutionState.key === targetResolutionKey
      ? targetResolutionState.values
      : EMPTY_TARGET_RESOLUTIONS;
  const nextRefreshAt = useMemo(() => {
    let earliest = Number.POSITIVE_INFINITY;
    for (const resolution of targetResolutions.values()) {
      if (resolution.status !== "available" || !resolution.expiresAt) continue;
      const timestamp = Date.parse(resolution.expiresAt);
      if (Number.isFinite(timestamp)) earliest = Math.min(earliest, timestamp);
    }
    return Number.isFinite(earliest) ? earliest : null;
  }, [targetResolutions]);

  useEffect(() => {
    if (nextRefreshAt === null) return;
    const delay = Math.max(0, nextRefreshAt - Date.now() - 1_000);
    const timer = window.setTimeout(() => setRefreshNonce((value) => value + 1), delay);
    return () => window.clearTimeout(timer);
  }, [nextRefreshAt]);

  return targetResolutions;
}

export function createAkbMarkdownAdapters(defaults: AkbMarkdownUploadContext) {
  const targetResolver = createAkbMarkdownTargetResolver(defaults);
  return {
    upload: {
      async upload(file: Blob, context?: AkbMarkdownUploadContext) {
        const vault = context?.vault ?? defaults.vault;
        const isFile = typeof File !== "undefined" && file instanceof File;
        const name = isFile ? (file as File).name : "attachment";
        const uploadFile = isFile
          ? (file as File)
          : new File([file], name, { type: file.type || "application/octet-stream" });
        const uploaded = await uploadAsset(vault, uploadFile, context?.signal);
        return {
          kind: "attachment" as const,
          id: uploaded.id,
          target: canonicalAkbMarkdownTarget(uploaded.target ?? uploaded.url, "attachment") ?? uploaded.url,
          alt: name.replace(/\.[^.]+$/, "") || "Image",
          expiresAt: uploaded.unclaimed_expires_at ?? undefined,
        };
      },
    },
    search: {
      async search(query: string) {
        const response = await searchDocs(query, defaults.vault, 20);
        return response.results
          .map((result) => {
            const target = typeof result?.uri === "string" ? canonicalAkbMarkdownTarget(result.uri) : null;
            if (!target) return null;
            const kind = classifyAkbMarkdownTarget(target);
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
    },
    targetResolver,
    /** Explicit action for embedding an existing standalone image File. */
    copyFileToAttachment: (fileId: string) => copyFileToAttachment(defaults.vault, fileId),
    getRetentionPolicy: () => getAttachmentRetentionPolicy(defaults.vault),
    /** Kept as an adapter-level capability for consumers that need the bytes. */
    getAttachmentBlob: (fileId: string, signal?: AbortSignal) =>
      getAssetBlob(fileId, defaults.vault, signal),
  };
}
