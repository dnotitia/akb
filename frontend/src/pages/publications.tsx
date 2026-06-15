import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  CheckCircle2,
  Copy,
  ExternalLink,
  FileText,
  Globe,
  Loader2,
  Lock,
  Paperclip,
  Table as TableIcon,
  Trash2,
} from "lucide-react";
import { listPublications, deletePublication, getDocument, type Publication } from "@/lib/api";
import { parseDocUri, parseFileUri } from "@/lib/uri";
import { formatDate } from "@/lib/utils";
import { RelativeTime } from "@/components/ui/relative-time";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/empty-state";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

const RESOURCE_ICON: Record<Publication["resource_type"], React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>> = {
  document: FileText,
  table_query: TableIcon,
  file: Paperclip,
};

const RESOURCE_LABEL: Record<Publication["resource_type"], string> = {
  document: "doc",
  table_query: "table",
  file: "file",
};

export default function PublicationsPage() {
  const { name } = useParams<{ name: string }>();
  const [items, setItems] = useState<Publication[] | null>(null);
  const [error, setError] = useState("");
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<Publication | null>(null);

  useEffect(() => {
    if (!name) return;
    // Reset stale state from previous param before re-fetch resolves.
    setItems(null);
    setError("");
    load(name);
  }, [name]);

  async function load(vault: string) {
    // Clear any prior error up-front so a successful Retry / post-unpublish
    // reload isn't permanently masked by the exclusive error render branch.
    setError("");
    try {
      const d = await listPublications(vault);
      const pubs: Publication[] = d.publications || [];

      // The publications row stores its own `title`, but for doc/file
      // publications it's typically null — the underlying resource owns
      // the title. Resolve in-flight so the list shows something
      // meaningful instead of the slug.
      const enriched = await Promise.all(
        pubs.map(async (p) => {
          if (p.title || p.resource_type !== "document" || !p.resource_uri) {
            return p;
          }
          const docPath = parseDocUri(p.resource_uri)?.id;
          if (!docPath) return p;
          try {
            const doc = await getDocument(vault, docPath);
            return { ...p, title: doc.title || p.title };
          } catch {
            return p;
          }
        }),
      );
      setItems(enriched);
    } catch (e: any) {
      // Keep items null on error so the error branch renders a Retry instead
      // of the cheerful "No publications yet" empty state masking a fetch fail.
      setError(e?.message || "Failed to load publications");
    }
  }

  async function confirmRevoke() {
    if (!name || !pendingRevoke) return;
    setRevokingId(pendingRevoke.slug);
    try {
      await deletePublication(name, pendingRevoke.slug);
      await load(name);
    } finally {
      setRevokingId(null);
    }
  }

  async function copyLink(pub: Publication) {
    // clipboard is undefined on insecure (plain-HTTP) origins — guard with `?.`
    // so the click never throws an uncaught TypeError before the success state.
    try {
      await navigator.clipboard?.writeText(pub.share_url);
      setCopiedId(pub.slug);
      setTimeout(() => setCopiedId(null), 1500);
    } catch {
      /* clipboard blocked — the link is still reachable via Open */
    }
  }

  // Link back into the app for the underlying resource. The canonical
  // handle is `resource_uri` (akb://...) — we parse the tail to route.
  function resourceHref(pub: Publication): string {
    if (!name || !pub.resource_uri) return `/vault/${name ?? ""}`;
    if (pub.resource_type === "document") {
      // URL-encode the path so a hierarchical doc like
      // `incidents/foo.md` survives as a single React Router param.
      const docPath = parseDocUri(pub.resource_uri)?.id;
      return docPath ? `/vault/${name}/doc/${encodeURIComponent(docPath)}` : `/vault/${name}`;
    }
    if (pub.resource_type === "file") {
      const fileId = parseFileUri(pub.resource_uri)?.id;
      return fileId ? `/vault/${name}/file/${fileId}` : `/vault/${name}`;
    }
    return `/vault/${name}`;
  }

  return (
    <div className="fade-up max-w-[1280px] mx-auto">
      <div className="coord mb-3">
        Vault · <span className="font-mono">{name}</span> · Publications
      </div>

      <h1 className="font-display text-3xl font-semibold tracking-tight text-foreground mb-3">
        Published
      </h1>

      <p className="text-base leading-[1.55] text-foreground-muted mb-8 max-w-2xl">
        Public-read links for this vault. Unpublish any time — the /p/ URL stops
        resolving immediately.
      </p>

      <section aria-labelledby="pubs-heading">
        <div className="flex items-baseline gap-3 pb-3 border-b border-border mb-3">
          <span id="pubs-heading" className="coord-ink">Publications</span>
          <span className="coord tabular-nums">
            [{items === null ? "…" : items.length}]
          </span>
        </div>

        {error ? (
          <EmptyState
            title="Couldn't load publications"
            description={error}
            action={
              <Button variant="outline" size="sm" onClick={() => name && load(name)}>
                Retry
              </Button>
            }
          />
        ) : items === null ? (
          <div className="coord py-6" role="status" aria-live="polite">Loading…</div>
        ) : items.length === 0 ? (
          <EmptyState
            title="No publications yet"
            description="Publish a document or table from its page to create a public /p/ link."
          />
        ) : (
          <ol className="rounded-[var(--radius-lg)] border border-border bg-surface divide-y divide-border overflow-hidden shadow-sm">
            {items.map((p, i) => {
              const Icon = RESOURCE_ICON[p.resource_type];
              return (
                <li
                  key={p.slug}
                  className="grid grid-cols-[32px_1fr_auto] items-baseline gap-4 px-3 py-3"
                >
                  <span className="coord tabular-nums">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <div className="min-w-0">
                    <div className="flex items-baseline gap-2 min-w-0">
                      <Icon className="h-3.5 w-3.5 shrink-0 translate-y-0.5 text-foreground-muted" aria-hidden />
                      <Link
                        to={resourceHref(p)}
                        title={p.title || p.slug}
                        className="text-sm font-medium tracking-tight truncate text-foreground hover:text-link rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                      >
                        {p.title || p.slug}
                      </Link>
                      <span className="coord shrink-0">
                        {RESOURCE_LABEL[p.resource_type]}
                      </span>
                      {p.password_protected && (
                        <Lock
                          className="h-3 w-3 text-foreground-muted shrink-0"
                          aria-label="Password protected"
                        />
                      )}
                    </div>
                    <div title={`/p/${p.slug}`} className="coord truncate mt-1 font-mono">
                      /p/{p.slug}
                    </div>
                  </div>
                  <div className="flex items-baseline gap-4 shrink-0">
                    <span className="coord tabular-nums hidden md:inline">
                      Views {p.view_count ?? 0}
                      {p.max_views ? ` / ${p.max_views}` : ""}
                    </span>
                    <span className="coord tabular-nums hidden sm:inline">
                      {p.expires_at
                        ? `Expires ${formatDate(p.expires_at)}`
                        : "Evergreen"}
                    </span>
                    <RelativeTime iso={p.created_at} />
                    {/* Benign Copy/Open pair, then a separated destructive Unpub. */}
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => copyLink(p)}
                        aria-label={copiedId === p.slug ? "Public link copied" : "Copy public link"}
                        className="inline-flex items-center gap-1 px-2 h-7 rounded-[var(--radius-sm)] text-xs text-foreground-muted hover:text-primary hover:bg-surface-hover transition-token cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                      >
                        {copiedId === p.slug ? (
                          <>
                            <CheckCircle2 className="h-3 w-3 text-success" aria-hidden />
                            Copied
                          </>
                        ) : (
                          <>
                            <Copy className="h-3 w-3" aria-hidden />
                            Copy
                          </>
                        )}
                      </button>
                      <a
                        href={p.share_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        aria-label="Open public page"
                        className="inline-flex items-center gap-1 px-2 h-7 rounded-[var(--radius-sm)] text-xs text-foreground-muted hover:text-primary hover:bg-surface-hover transition-token cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                      >
                        <ExternalLink className="h-3 w-3" aria-hidden />
                        Open
                      </a>
                    </div>
                    <button
                      onClick={() => setPendingRevoke(p)}
                      disabled={revokingId === p.slug}
                      aria-label="Unpublish"
                      className="inline-flex items-center gap-1 px-2 h-7 rounded-[var(--radius-sm)] text-xs text-destructive hover:bg-surface-hover transition-token cursor-pointer disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    >
                      {revokingId === p.slug ? (
                        <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                      ) : (
                        <Trash2 className="h-3 w-3" aria-hidden />
                      )}
                      {revokingId === p.slug ? "Unpub…" : "Unpub"}
                    </button>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </section>

      {items !== null && items.length > 0 && (
        <p className="coord mt-6 flex items-center gap-2">
          <Globe className="h-3 w-3" aria-hidden />
          Public · Read-only · No auth required
        </p>
      )}

      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(o) => !o && setPendingRevoke(null)}
        title={
          pendingRevoke
            ? `Unpublish "${pendingRevoke.title || pendingRevoke.slug}"?`
            : ""
        }
        description={
          pendingRevoke
            ? `The link /p/${pendingRevoke.slug} will stop working immediately.\nThis cannot be undone.`
            : ""
        }
        confirmLabel="Unpublish"
        variant="destructive"
        onConfirm={confirmRevoke}
      />
    </div>
  );
}
