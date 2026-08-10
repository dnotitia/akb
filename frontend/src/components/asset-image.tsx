import { useEffect, useMemo, useState, type ImgHTMLAttributes } from "react";
import { ImageOff, Loader2 } from "lucide-react";
import { getAssetBlob, publicationAssetUrl } from "@/lib/api";
import { assetIdFromUrl } from "@/lib/image-assets";
import { cn, sanitizeLinkUrl } from "@/lib/utils";

export type AssetContext =
  | {
      mode: "authenticated";
      vault: string;
      document?: string;
      commit?: string;
    }
  | { mode: "publication"; slug: string };

function safeExternalImageUrl(src: string): string | null {
  const trimmed = src.trim();
  if (!trimmed || trimmed.startsWith("//")) return null;
  const safe = sanitizeLinkUrl(trimmed);
  if (safe !== "#") return safe;
  // `sanitizeLinkUrl` intentionally targets navigation and rejects bare
  // relative paths. Relative image paths are inert and remain useful for
  // existing imported markdown, provided they do not introduce a URI scheme.
  if (!/^[a-z][a-z0-9+.-]*:/i.test(trimmed)) return trimmed;
  return null;
}

export interface AssetImageProps
  extends Omit<ImgHTMLAttributes<HTMLImageElement>, "src"> {
  src?: string | null;
  assetContext?: AssetContext;
}

/**
 * Shared markdown image renderer. Private assets are fetched with Bearer auth
 * and displayed through a short-lived object URL; publication assets retain a
 * native image request so browser lazy-loading still works.
 */
export function AssetImage({
  src,
  alt = "",
  assetContext,
  className,
  onError,
  ...imgProps
}: AssetImageProps) {
  const assetId = assetIdFromUrl(src);
  const privateVault =
    assetContext?.mode === "authenticated" ? assetContext.vault : null;
  const privateDocument =
    assetContext?.mode === "authenticated" ? assetContext.document : undefined;
  const privateCommit =
    assetContext?.mode === "authenticated" ? assetContext.commit : undefined;
  const publicationSlug =
    assetContext?.mode === "publication" ? assetContext.slug : null;
  const assetKey = assetId && privateVault
    ? `${privateVault}:${privateDocument ?? "live"}:${privateCommit ?? "live"}:${assetId}`
    : null;
  const [loadState, setLoadState] = useState<{
    key: string;
    blobUrl: string | null;
    failed: boolean;
  } | null>(null);
  const currentLoadState = loadState?.key === assetKey ? loadState : null;
  const renderKey = assetId
    ? publicationSlug
      ? `publication:${publicationSlug}:${assetId}`
      : assetKey
    : src ?? null;
  const [imageErrorKey, setImageErrorKey] = useState<string | null>(null);
  const imageFailed = !!renderKey && imageErrorKey === renderKey;
  const isPrivateAsset = !!assetId && !!privateVault;

  useEffect(() => {
    if (!assetId || !privateVault || !assetKey) return;

    const controller = new AbortController();
    let objectUrl: string | null = null;
    setLoadState({ key: assetKey, blobUrl: null, failed: false });
    getAssetBlob(
      assetId,
      privateVault,
      controller.signal,
      privateDocument && privateCommit
        ? { document: privateDocument, commit: privateCommit }
        : undefined,
    )
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setLoadState({ key: assetKey, blobUrl: objectUrl, failed: false });
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setLoadState({ key: assetKey, blobUrl: null, failed: true });
        }
      });

    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [assetId, assetKey, privateCommit, privateDocument, privateVault]);

  const resolvedSrc = useMemo(() => {
    if (assetId) {
      if (privateVault) {
        return currentLoadState?.blobUrl ?? null;
      }
      if (publicationSlug) {
        return publicationAssetUrl(publicationSlug, assetId);
      }
      return null;
    }
    return src ? safeExternalImageUrl(src) : null;
  }, [assetId, currentLoadState?.blobUrl, privateVault, publicationSlug, src]);

  const frameClass = cn(
    "block my-4 rounded-[var(--radius-lg)] border border-border max-w-full h-auto",
    className,
  );

  if (isPrivateAsset && !currentLoadState?.blobUrl && !currentLoadState?.failed) {
    return (
      <div
        role="status"
        aria-label={alt ? `Loading image: ${alt}` : "Loading image"}
        className={cn(
          frameClass,
          "min-h-28 bg-surface-2 text-foreground-muted flex items-center justify-center gap-2 p-4 text-sm",
        )}
      >
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        Loading image…
      </div>
    );
  }

  if (currentLoadState?.failed || imageFailed || !resolvedSrc) {
    return (
      <div
        role="img"
        aria-label={alt ? `Image unavailable: ${alt}` : "Image unavailable"}
        className={cn(
          frameClass,
          "min-h-28 bg-destructive-soft text-destructive-soft-foreground flex items-center justify-center gap-2 p-4 text-sm",
        )}
      >
        <ImageOff className="h-4 w-4" aria-hidden />
        Image unavailable
      </div>
    );
  }

  return (
    <img
      {...imgProps}
      src={resolvedSrc}
      alt={alt}
      loading={imgProps.loading ?? "lazy"}
      decoding={imgProps.decoding ?? "async"}
      referrerPolicy="no-referrer"
      className={frameClass}
      onError={(event) => {
        setImageErrorKey(renderKey);
        onError?.(event);
      }}
    />
  );
}
