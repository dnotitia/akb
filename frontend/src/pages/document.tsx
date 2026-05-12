import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowLeft,
  CheckCircle2,
  ExternalLink,
  GitGraph,
  Loader2,
  Lock,
  Pencil,
  Trash2,
  Unlock,
} from "lucide-react";
import {
  deleteDocument,
  getDocument,
  getRelations,
  getVaultInfo,
  publishDoc,
  unpublishDoc,
} from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { parseHeadings, slugify } from "@/lib/markdown";
import { DocumentOutline } from "@/components/doc-outline";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { HistoryList } from "@/components/history-list";
import { FrontmatterEditDialog } from "@/components/frontmatter-edit-dialog";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { PublishOptionsDialog } from "@/components/publish-options-dialog";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";

const RELATION_COLOR: Record<string, string> = {
  implements: "text-good",
  depends_on: "text-info",
  references: "text-info",
  related_to: "text-foreground-muted",
  attached_to: "text-good",
  derived_from: "text-warning",
};

export default function DocumentPage() {
  const { name, id } = useParams<{ name: string; id: string }>();
  const navigate = useNavigate();
  const { refetchTree } = useVaultRefresh();
  const [doc, setDoc] = useState<any>(null);
  const [relations, setRelations] = useState<any[]>([]);
  const [provenance, setProvenance] = useState<any[]>([]);
  const [error, setError] = useState("");
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState("");
  const [copied, setCopied] = useState(false);
  const [articleEl, setArticleEl] = useState<HTMLElement | null>(null);
  const [vaultRole, setVaultRole] = useState<string | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const docId = id ? decodeURIComponent(id) : "";

  useEffect(() => {
    if (!name) return;
    getVaultInfo(name)
      .then((d) => setVaultRole(d?.role || null))
      .catch(() => setVaultRole(null));
  }, [name]);

  useEffect(() => {
    if (!name || !docId) return;
    setDoc(null);
    setError("");
    setProvenance([]);
    getDocument(name, docId)
      .then((d) => {
        setDoc(d);
        if (d.path && d.path !== docId) {
          navigate(`/vault/${name}/doc/${encodeURIComponent(d.path)}`, { replace: true });
        }
        if (d.id) {
          getRelations(name, d.id).then((r) => setRelations(r.relations || [])).catch(() => {});
        }
        if (d.path) {
          loadHistory(name, d.path);
        }
      })
      .catch((e) => setError(e.message));
  }, [name, docId]);

  // History = `git log -- <doc.path>` scoped to this document.
  // The /activity endpoint's `collection` query param is plumbed to
  // git.vault_log(path=...), so this is true per-doc commit history.
  async function loadHistory(vault: string, docPath: string) {
    const t = localStorage.getItem("akb_token") || "";
    try {
      const r = await fetch(
        `/api/v1/activity/${encodeURIComponent(vault)}?collection=${encodeURIComponent(docPath)}&limit=20`,
        { headers: { Authorization: `Bearer ${t}` } },
      );
      if (!r.ok) {
        setProvenance([]);
        return;
      }
      const d = await r.json();
      setProvenance(d.activity || []);
    } catch {
      setProvenance([]);
    }
  }

  const markdownComponents = useMemo(
    () => buildHeadingComponents(doc?.content || ""),
    [doc?.content],
  );

  if (error) {
    return (
      <div className="py-8 fade-up">
        <div className="coord-spark mb-2">⚠ ERROR</div>
        <p className="text-destructive mb-6 max-w-xl">{error}</p>
        <Button asChild variant="outline">
          <Link to={`/vault/${name}`}>
            <ArrowLeft className="h-4 w-4" aria-hidden />
            Back to {name}
          </Link>
        </Button>
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="py-8 coord">
        <Loader2 className="h-4 w-4 inline animate-spin mr-2" aria-hidden />
        Loading…
      </div>
    );
  }

  async function handleUnpublish() {
    setPublishing(true);
    setPublishError("");
    try {
      await unpublishDoc(name!, docId);
      setDoc({ ...doc, is_public: false, public_slug: null });
    } catch (e: any) {
      setPublishError(e?.message || "Failed to unpublish");
    }
    setPublishing(false);
  }

  async function copyPublicLink() {
    const url = `${location.origin}/p/${doc.public_slug}`;
    await navigator.clipboard.writeText(url);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  const commitShort = doc.current_commit?.slice(0, 7);

  // Grid fills the outlet so the right rail anchors to the outlet's
  // right edge — a stable viewport position regardless of whether the
  // vault tree is expanded or collapsed. When the tree hides, the
  // reclaimed space flows into the article column on the left (article
  // right edge and rail position stay put).
  return (
    <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_260px] gap-x-10 gap-y-6 fade-up">
      {/* ── Main column ─────────────────────────────────────── */}
      {/* Article is capped + centered within its grid cell so the body
          gains margin on both sides as the outlet widens (tree hidden),
          mirroring other pages' centering. Rail stays pinned to the
          outer outlet edge via the grid's second column. */}
      <article ref={setArticleEl} className="min-w-0 w-full max-w-[1020px] justify-self-center">
        {/* Mono meta line */}
        <div className="coord mb-2">
          DOC · akb://{name}/{doc.path}
          {commitShort && (
            <>
              {" · HEAD "}
              <span className="text-accent">{commitShort}</span>
            </>
          )}
        </div>

        {/* Serif display title */}
        <h1 className="font-serif text-[40px] leading-[1.05] tracking-[-0.02em] text-foreground mb-3">
          {doc.title}
        </h1>

        {/* Byline in serif italic */}
        {(doc.created_by || doc.updated_at) && (
          <div className="font-serif-italic text-[14px] text-foreground-muted mb-7">
            {doc.created_by && (
              <>
                Written by{" "}
                <span className="not-italic text-accent">{doc.created_by}</span>
                {doc.updated_at && <>, last changed {timeAgo(doc.updated_at)}</>}.
              </>
            )}
          </div>
        )}

        {/* Frontmatter card — mono metadata with semantic colors */}
        <FrontmatterCard doc={doc} />

        {doc.summary && (
          <p className="font-serif text-[17px] leading-[1.6] text-foreground mb-7 mt-6">
            {doc.summary}
          </p>
        )}

        {publishError && (
          <div
            role="alert"
            aria-live="polite"
            className="border border-destructive px-3 py-2 mb-6 text-xs font-mono uppercase tracking-wider text-destructive"
          >
            ⚠ {publishError.toUpperCase()}
          </div>
        )}

        {/* Body — stretched to the article column so it shares the title's
            right edge. 100% keeps prose inside the column (unlike `none`,
            which lets long inline tokens push the width past the grid cell
            and introduce a horizontal scrollbar). */}
        <div
          className="prose dark:prose-invert min-w-0"
          style={{ maxWidth: "100%" }}
        >
          <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {doc.content || ""}
          </Markdown>
        </div>
      </article>

      {/* ── Right rail ──────────────────────────────────────────
         Publish lives above the tabs as an always-visible strip —
         it's a top-level action, not peer to Outline/Relations.
         Tabs below keep the reading surface predictable: one
         secondary pane visible at a time. */}
      <aside className="xl:sticky xl:top-4 xl:self-start xl:max-h-[calc(100dvh-13rem)] flex flex-col text-sm min-h-0">
        {/* Owner/admin/writer actions */}
        {(vaultRole === "writer" || vaultRole === "admin" || vaultRole === "owner") && (
          <div className="shrink-0 pb-3 mb-3 border-b border-border space-y-2">
            <button
              onClick={() => setEditOpen(true)}
              className="w-full inline-flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs border border-border hover:border-accent hover:text-accent transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <Pencil className="h-3 w-3" aria-hidden />
              Edit details
            </button>
            <button
              onClick={() => setDeleteOpen(true)}
              className="w-full inline-flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs border border-border text-foreground-muted hover:border-destructive hover:text-destructive transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <Trash2 className="h-3 w-3" aria-hidden />
              Delete document
            </button>
          </div>
        )}

        {/* Publish strip — always visible */}
        <div className="shrink-0 pb-3 mb-3 border-b border-border">
          {doc.is_public && doc.public_slug ? (
            <div className="flex flex-col gap-1.5 text-xs">
              <div className="coord">§ PUBLISHED</div>
              <div className="font-mono text-[11px] text-foreground truncate">
                /p/{doc.public_slug}
              </div>
              <div className="flex items-center gap-3 text-[11px]">
                <button
                  onClick={copyPublicLink}
                  className="inline-flex items-center gap-1 text-foreground-muted hover:text-accent transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  {copied ? (
                    <CheckCircle2 className="h-3 w-3 text-accent" aria-hidden />
                  ) : (
                    <ExternalLink className="h-3 w-3" aria-hidden />
                  )}
                  {copied ? "Copied" : "Copy link"}
                </button>
                <button
                  onClick={handleUnpublish}
                  disabled={publishing}
                  className="inline-flex items-center gap-1 text-foreground-muted hover:text-destructive transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:opacity-50"
                >
                  {publishing ? (
                    <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                  ) : (
                    <Lock className="h-3 w-3" aria-hidden />
                  )}
                  {publishing ? "Unpublishing…" : "Unpublish"}
                </button>
              </div>
            </div>
          ) : (
            <button
              onClick={() => setPublishOpen(true)}
              disabled={publishing}
              className="w-full inline-flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs border border-border hover:border-accent hover:text-accent transition-colors disabled:opacity-50 cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <Unlock className="h-3 w-3" aria-hidden />
              Publish to /p/…
            </button>
          )}
        </div>

        <Tabs defaultValue="outline" className="flex flex-col min-h-0 flex-1">
          <TabsList className="shrink-0">
            <TabsTrigger value="outline" className="gap-1.5">
              Outline
              <span className="coord tabular-nums">[{headingCount(doc.content)}]</span>
            </TabsTrigger>
            <TabsTrigger value="relations" className="gap-1.5">
              Relations
              {relations.length > 0 && (
                <span className="coord tabular-nums">[{relations.length}]</span>
              )}
            </TabsTrigger>
            <TabsTrigger value="history" className="gap-1.5">
              History
              {provenance.length > 0 && (
                <span className="coord tabular-nums">[{provenance.length}]</span>
              )}
            </TabsTrigger>
          </TabsList>

          <TabsContent
            value="outline"
            className="flex-1 min-h-0 overflow-y-auto rail-scroll pr-1 pt-3"
          >
            <DocumentOutline markdown={doc.content || ""} articleEl={articleEl} />
          </TabsContent>

          <TabsContent
            value="relations"
            className="flex-1 min-h-0 overflow-y-auto rail-scroll pr-1 pt-3"
          >
            {relations.length > 0 ? (
              <>
                <ol className="font-mono text-[11px] leading-[1.9] space-y-0.5">
                  {relations.map((r, i) => {
                    const m = /^akb:\/\/([^/]+)\/(doc|table|file)\/(.+)$/.exec(r.uri || "");
                    const targetVault = m?.[1] ?? name;
                    const targetType = m?.[2] ?? r.resource_type;
                    const targetRef = m?.[3] ?? "";
                    let href = "#";
                    if (targetType === "doc") {
                      href = `/vault/${targetVault}/doc/${encodeURIComponent(targetRef)}`;
                    } else if (targetType === "table") {
                      href = `/vault/${targetVault}/table/${encodeURIComponent(targetRef)}`;
                    } else if (targetType === "file") {
                      href = `/vault/${targetVault}/file/${encodeURIComponent(targetRef)}`;
                    }
                    const label = r.name || targetRef || r.uri;
                    const relColor = RELATION_COLOR[r.relation] || "text-foreground-muted";
                    return (
                      <li key={i}>
                        <Link
                          to={href}
                          className="grid grid-cols-[88px_1fr] gap-1.5 py-0.5 group hover:bg-surface-muted -mx-1 px-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                        >
                          <span className={relColor}>{r.relation || "relates"}</span>
                          <span className="text-foreground truncate group-hover:text-accent">
                            → {label}
                          </span>
                        </Link>
                      </li>
                    );
                  })}
                </ol>
                <Link
                  to={`/vault/${name}/graph?focus=${encodeURIComponent(doc.id ? `akb://${name}/doc/${doc.path}` : "")}`}
                  className="mt-3 inline-flex items-center gap-1 text-xs text-accent hover:underline"
                >
                  <GitGraph className="h-3 w-3" aria-hidden /> Open in graph →
                </Link>
              </>
            ) : (
              <div className="coord">No relations yet.</div>
            )}
          </TabsContent>

          <TabsContent
            value="history"
            className="flex-1 min-h-0 overflow-hidden pr-1 pt-3"
          >
            <HistoryList entries={provenance as any} />
          </TabsContent>
        </Tabs>
      </aside>

      <FrontmatterEditDialog
        open={editOpen}
        onOpenChange={setEditOpen}
        vault={name!}
        docId={docId}
        doc={doc}
        onSaved={(next) => {
          setDoc({ ...doc, ...next });
          // Title/type/status changes are surfaced in the tree row
          // labels; refresh so the sidebar matches.
          refetchTree();
        }}
      />

      <PublishOptionsDialog
        open={publishOpen}
        onOpenChange={setPublishOpen}
        vault={name!}
        docId={docId}
        onPublished={(slug) => setDoc({ ...doc, is_public: true, public_slug: slug })}
      />

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`Delete "${doc.title || doc.path}"?`}
        description={
          "The document, its embeddings, and its publication links are removed.\nGit history of the file is preserved in the vault repo.\nThis cannot be undone from the UI."
        }
        confirmLabel="Delete document"
        variant="destructive"
        onConfirm={async () => {
          await deleteDocument(name!, docId);
          refetchTree();
          navigate(`/vault/${name}`);
        }}
      />
    </div>
  );
}

// ── Frontmatter metadata card ─────────────────────────────────
function FrontmatterCard({ doc }: { doc: any }) {
  const rows: Array<[string, React.ReactNode]> = [];
  if (doc.type) rows.push(["type", <span className="text-foreground">{doc.type}</span>]);
  if (doc.status) {
    const statusColor =
      doc.status === "active" ? "text-good" :
      doc.status === "archived" || doc.status === "superseded" ? "text-warning" :
      "text-foreground-muted";
    rows.push(["status", <span className={statusColor}>{doc.status}</span>]);
  }
  if (doc.domain) rows.push(["domain", <span className="text-foreground">{doc.domain}</span>]);
  if (doc.tags?.length) {
    rows.push([
      "tags",
      <span className="text-info">{doc.tags.map((t: string) => `#${t}`).join(" ")}</span>,
    ]);
  }
  if (doc.depends_on?.length) {
    rows.push([
      "depends_on",
      <span className="text-foreground-muted">{doc.depends_on.join(", ")}</span>,
    ]);
  }
  if (doc.related_to?.length) {
    rows.push([
      "related_to",
      <span className="text-foreground-muted">{doc.related_to.join(", ")}</span>,
    ]);
  }
  if (doc.is_public) {
    rows.push([
      "published",
      <span className="text-accent">
        /p/{doc.public_slug}
      </span>,
    ]);
  }

  if (rows.length === 0) return null;

  return (
    <div className="border border-border bg-surface px-4 py-3 font-mono text-[11px] leading-[1.85]">
      {rows.map(([k, v], i) => (
        <div key={i}>
          <span className="text-foreground-muted">{k}:</span> {v}
        </div>
      ))}
    </div>
  );
}

function buildHeadingComponents(markdown: string) {
  const slugQueue = parseHeadings(markdown).map((h) => h.slug);
  let cursor = 0;
  const make = (level: 1 | 2 | 3 | 4 | 5 | 6) => (props: any) => {
    const id = slugQueue[cursor++] ?? slugify(flattenText(props.children)) ?? `heading-${level}`;
    const Tag = `h${level}` as any;
    return <Tag id={id} {...props} />;
  };
  return {
    h1: make(1), h2: make(2), h3: make(3), h4: make(4), h5: make(5), h6: make(6),
  };
}

function flattenText(children: any): string {
  if (typeof children === "string") return children;
  if (Array.isArray(children)) return children.map(flattenText).join("");
  if (children?.props?.children) return flattenText(children.props.children);
  return "";
}

function headingCount(markdown: string | undefined): number {
  return parseHeadings(markdown || "").length;
}
