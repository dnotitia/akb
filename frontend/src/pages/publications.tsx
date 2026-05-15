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
import { listPublications, deletePublication, getDocument } from "@/lib/api";
import { parseDocUri, parseFileUri } from "@/lib/uri";
import { timeAgo } from "@/lib/utils";
import { EmptyState } from "@/components/empty-state";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

interface Publication {
  publication_id: string;
  slug: string;
  resource_type: "document" | "file" | "table_query";
  // Canonical handle (akb://{vault}/{doc|file}/{...}). Null for
  // table_query publications, which have no single addressable resource.
  resource_uri?: string | null;
  title?: string;
  created_at?: string;
  expires_at?: string | null;
  max_views?: number | null;
  view_count?: number;
  password_protected?: boolean;
  public_url: string;
  public_url_full?: string | null;
}

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
    load(name);
  }, [name]);

  async function load(vault: string) {
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
      setError(e?.message || "Failed to load publications");
      setItems([]);
    }
  }

  async function confirmRevoke() {
    if (!name || !pendingRevoke) return;
    setRevokingId(pendingRevoke.publication_id);
    try {
      await deletePublication(name, pendingRevoke.publication_id);
      await load(name);
    } finally {
      setRevokingId(null);
    }
  }

  function copyLink(pub: Publication) {
    // Prefer the absolute URL the server advertises so shared links work
    // regardless of where the viewer pastes them. Fall back to the relative
    // /p/<slug> path when AKB_PUBLIC_BASE_URL isn't configured server-side.
    const url = pub.public_url_full || `${location.origin}${pub.public_url}`;
    navigator.clipboard.writeText(url);
    setCopiedId(pub.publication_id);
    setTimeout(() => setCopiedId(null), 1500);
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
        VAULT · {name?.toUpperCase()} · PUBLICATIONS
      </div>

      <h1 className="font-serif text-[54px] leading-[0.95] tracking-[-0.03em] text-foreground mb-3">
        published<span className="text-foreground-muted">.</span>
      </h1>

      <p className="font-serif-italic text-[17px] leading-[1.55] text-foreground-muted mb-8">
        Public-read links for this vault. Unpublish any time — the /p/ URL stops
        resolving immediately.
      </p>

      <section aria-labelledby="pubs-heading">
        <div className="flex items-baseline gap-3 pb-3 border-b border-border mb-3">
          <span id="pubs-heading" className="coord-ink">§ PUBLICATIONS</span>
          <span className="coord tabular-nums">
            [{items === null ? "…" : items.length}]
          </span>
        </div>

        {error && (
          <div className="coord-spark mb-4">⚠ {error.toUpperCase()}</div>
        )}

        {items === null ? (
          <div className="coord py-6">LOADING…</div>
        ) : items.length === 0 ? (
          <EmptyState
            title="No publications yet"
            description="Publish a document or table from its page to create a public /p/ link."
          />
        ) : (
          <ol className="border border-border bg-surface divide-y divide-border">
            {items.map((p, i) => {
              const Icon = RESOURCE_ICON[p.resource_type];
              return (
                <li
                  key={p.publication_id}
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
                        className="text-sm font-medium tracking-tight truncate text-foreground hover:text-accent"
                      >
                        {p.title || p.slug}
                      </Link>
                      <span className="coord shrink-0">
                        {RESOURCE_LABEL[p.resource_type]}
                      </span>
                      {p.password_protected && (
                        <Lock
                          className="h-3 w-3 text-warning shrink-0"
                          aria-label="Password protected"
                        />
                      )}
                    </div>
                    <div className="coord truncate mt-1 font-mono">
                      /p/{p.slug}
                    </div>
                  </div>
                  <div className="flex items-baseline gap-4 shrink-0">
                    <span className="coord tabular-nums hidden md:inline">
                      VIEWS {p.view_count ?? 0}
                      {p.max_views ? ` / ${p.max_views}` : ""}
                    </span>
                    <span className="coord tabular-nums hidden sm:inline">
                      {p.expires_at
                        ? `EXPIRES ${timeAgo(p.expires_at).replace("ago", "").trim().toUpperCase()}`
                        : "EVERGREEN"}
                    </span>
                    <span className="coord tabular-nums">
                      {timeAgo(p.created_at).toUpperCase()}
                    </span>
                    <button
                      onClick={() => copyLink(p)}
                      aria-label="Copy public link"
                      className="inline-flex items-center gap-1 text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-accent transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    >
                      {copiedId === p.publication_id ? (
                        <>
                          <CheckCircle2 className="h-3 w-3 text-accent" aria-hidden />
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
                      href={p.public_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label="Open public page"
                      className="inline-flex items-center gap-1 text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-accent transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    >
                      <ExternalLink className="h-3 w-3" aria-hidden />
                      Open
                    </a>
                    <button
                      onClick={() => setPendingRevoke(p)}
                      disabled={revokingId === p.publication_id}
                      aria-label="Unpublish"
                      className="inline-flex items-center gap-1 text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-destructive transition-colors cursor-pointer disabled:opacity-40 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
                    >
                      {revokingId === p.publication_id ? (
                        <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                      ) : (
                        <Trash2 className="h-3 w-3" aria-hidden />
                      )}
                      {revokingId === p.publication_id ? "Unpub…" : "Unpub"}
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
          PUBLIC · READ-ONLY · NO AUTH REQUIRED
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
