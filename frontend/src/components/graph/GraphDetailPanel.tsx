// frontend/src/components/graph/GraphDetailPanel.tsx
import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Crosshair, ExternalLink, Pin, X } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
import { getDocument, getRelations, getProvenance, drillDown } from "@/lib/api";
import { ALL_NODE_KINDS, ALL_RELATIONS, type NodeKind, type RelationKind, type RelatedRef, kindToSegment } from "./graph-types";
import { Section } from "./Section";

interface Props {
  vault: string;
  docId: string;
  kind: NodeKind;
  uri: string;
  /** Select (highlight) the related resource's node in the graph — adding it
   *  to the graph first if it isn't currently rendered. */
  onSelectRelated: (rel: RelatedRef) => void;
  onFitToNode: (uri: string) => void;
  onClose: () => void;
  onTogglePin?: () => void;
  pinned?: boolean;
}

interface DocResponse {
  doc_id: string;
  title?: string;
  summary?: string;
  tags?: string[];
  content?: string;
  type?: string;
  columns?: string[];
  mime_type?: string;
  size_bytes?: number;
  author?: string;
  created_at?: string;
  updated_at?: string;
}

const PREVIEW_LINES = 40;

const REL_SET = new Set<string>(ALL_RELATIONS);
const KIND_SET = new Set<string>(ALL_NODE_KINDS);

export function GraphDetailPanel({
  vault,
  docId,
  kind,
  uri,
  onSelectRelated,
  onFitToNode,
  onClose,
  onTogglePin,
  pinned,
}: Props) {
  const [metaOpen, setMetaOpen] = useState(false);
  const [sectionsOpen, setSectionsOpen] = useState(false);
  // Move focus into the panel when a new node is selected so AT users land on
  // the freshly-revealed detail (the panel appears with no other notification).
  const titleRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    titleRef.current?.focus();
  }, [docId]);

  const docQuery = useQuery<DocResponse>({
    queryKey: ["document", vault, docId],
    queryFn: () => getDocument(vault, docId) as Promise<DocResponse>,
  });
  const relQuery = useQuery({
    queryKey: ["relations", vault, docId],
    queryFn: () => getRelations(vault, docId),
  });
  const provQuery = useQuery({
    queryKey: ["provenance", vault, docId],
    queryFn: () => getProvenance(vault, docId),
    enabled: metaOpen,
  });
  const secQuery = useQuery({
    queryKey: ["drill", vault, docId],
    queryFn: () => drillDown(vault, docId),
    enabled: sectionsOpen,
  });

  const doc = docQuery.data;
  const preview = (doc?.content || "").split("\n").slice(0, PREVIEW_LINES).join("\n");

  const groupedRels = groupRelations(relQuery.data?.relations || []);
  const totalRels =
    groupedRels.incoming.reduce((s, g) => s + g.rows.length, 0) +
    groupedRels.outgoing.reduce((s, g) => s + g.rows.length, 0);

  function openDoc() {
    const segment = kindToSegment(kind);
    window.location.assign(`/vault/${vault}/${segment}/${encodeURIComponent(docId)}`);
  }

  return (
    <aside
      className="flex flex-col h-full overflow-y-auto border-l border-border bg-surface"
      role="region"
      aria-label={`Details for ${kind} ${docId}`}
    >
      <header className="flex items-center justify-between px-3 py-2 border-b border-border">
        <span className="coord">{kind} · {docId}</span>
        <button onClick={onClose} aria-label="Close detail" className="text-foreground-muted hover:text-foreground">
          <X className="h-3 w-3" />
        </button>
      </header>

      {docQuery.isLoading ? (
        // Progressive-loading: a skeleton stands in for the fetch so the
        // panel never reads as "nothing showed up" while data is in flight.
        <div className="px-3 py-3 flex flex-col gap-3" aria-busy="true">
          <Skeleton className="h-7 w-3/4" />
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-5/6" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : docQuery.isError ? (
        // Error-recovery: reuse the app's standard EmptyState (same shape the
        // graph canvas uses for its load failure). A node can point at a
        // deleted/renamed doc, or the fetch can transiently fail — surface it
        // with a retry path instead of rendering blank.
        <EmptyState
          title="Couldn't load this resource."
          description={String((docQuery.error as Error)?.message || docQuery.error || "unknown error")}
          action={
            <Button size="sm" variant="outline" onClick={() => docQuery.refetch()}>
              Retry
            </Button>
          }
        />
      ) : (
        <>
      <div className="px-3 py-3 border-b border-border">
        <h2
          ref={titleRef}
          tabIndex={-1}
          className="font-semibold tracking-[-0.015em] text-2xl leading-tight mb-1 focus:outline-none"
        >
          {doc?.title || "…"}
        </h2>
        <p className="coord text-foreground-muted truncate" title={uri}>{uri}</p>
        <div className="flex flex-wrap gap-1 mt-3">
          <Button size="sm" variant="accent" onClick={openDoc}>
            <ExternalLink className="h-3 w-3" /> Open
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => navigator.clipboard?.writeText(uri).catch(() => {})}
          >
            Copy URI
          </Button>
          {onTogglePin && (
            // Pin is a toggle, not a CTA — when pinned it reads as a teal
            // selected control, not a second filled-orange marquee.
            <Button
              size="sm"
              variant="outline"
              onClick={onTogglePin}
              aria-pressed={!!pinned}
              className={pinned ? "bg-surface-selected text-surface-selected-foreground border-primary" : undefined}
            >
              <Pin className="h-3 w-3" /> {pinned ? "Pinned" : "Pin"}
            </Button>
          )}
        </div>
      </div>

      {doc?.summary && (
        <Section label="Summary" className="px-3">
          <p className="text-[12px] leading-relaxed text-foreground">{doc.summary}</p>
        </Section>
      )}

      {kind === "table" && doc?.columns && (
        <Section label="Columns" className="px-3">
          <ul className="flex flex-wrap gap-1">
            {doc.columns.map((c) => (
              <li key={c}>
                <Badge variant="outline">{c}</Badge>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {kind === "file" && (
        <Section label="File" className="px-3">
          <p className="coord">
            {doc?.mime_type || "—"} · {doc?.size_bytes ? `${doc.size_bytes} bytes` : "—"}
          </p>
        </Section>
      )}

      {doc?.tags && doc.tags.length > 0 && (
        <Section label="Tags" className="px-3">
          <div className="flex flex-wrap gap-1">
            {doc.tags.map((t) => (
              <Badge key={t} variant="outline">{t}</Badge>
            ))}
          </div>
        </Section>
      )}

      <Section label={`Relations [${totalRels}]`} className="px-3">
        {totalRels === 0 ? (
          <p className="coord text-foreground-muted">none</p>
        ) : (
          <div className="flex flex-col gap-2">
            {groupedRels.outgoing.map((g) => (
              <RelGroup
                key={`out-${g.relation}`}
                relation={g.relation}
                direction="out"
                rows={g.rows}
                onSelectRelated={onSelectRelated}
                onFitToNode={onFitToNode}
              />
            ))}
            {groupedRels.incoming.map((g) => (
              <RelGroup
                key={`in-${g.relation}`}
                relation={g.relation}
                direction="in"
                rows={g.rows}
                onSelectRelated={onSelectRelated}
                onFitToNode={onFitToNode}
              />
            ))}
          </div>
        )}
      </Section>

      {kind === "document" && (
        <Section label="Preview" className="px-3">
          <button
            type="button"
            onClick={() => setSectionsOpen((v) => !v)}
            className="coord hover:text-foreground inline-flex items-center gap-1 mb-2"
            aria-expanded={sectionsOpen}
          >
            {sectionsOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            show sections
          </button>
          {sectionsOpen && (
            <ul className="mb-2 flex flex-col gap-px">
              {(secQuery.data?.sections || []).map((s: any, i: number) => (
                <li key={i} title={s.heading || s.title || `section ${i}`} className="coord truncate">‣ {s.heading || s.title || `section ${i}`}</li>
              ))}
            </ul>
          )}
          <pre className="font-mono text-[11px] leading-snug whitespace-pre-wrap text-foreground bg-background border border-border p-2 max-h-64 overflow-auto">
            {preview || "(empty)"}
          </pre>
        </Section>
      )}

      <Section
        label="Meta"
        className="px-3"
        rightAction={
          <button
            type="button"
            onClick={() => setMetaOpen((v) => !v)}
            aria-label="Toggle meta"
            aria-expanded={metaOpen}
            className="coord hover:text-foreground inline-flex items-center gap-1"
          >
            {metaOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            {metaOpen ? "hide metadata" : "show metadata"}
          </button>
        }
      >
        {metaOpen && (
          <div className="flex flex-col gap-1 text-[11px]">
            <p>
              <span className="coord">Author</span> · {doc?.author || "—"}
            </p>
            <p>
              <span className="coord">Created</span> · {doc?.created_at || "—"}
            </p>
            <p>
              <span className="coord">Updated</span> · {doc?.updated_at || "—"}
            </p>
            <p className="break-all">
              <span className="coord">Provenance</span> · {JSON.stringify(provQuery.data?.provenance || "—")}
            </p>
          </div>
        )}
      </Section>
        </>
      )}
    </aside>
  );
}

/** Humanize a snake_case relation kind for display: "depends_on" → "Depends on". */
function humanizeRelation(relation: string): string {
  const words = relation.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

interface GroupedRel {
  relation: RelationKind;
  rows: Array<{
    other_uri: string;
    other_name: string;
    other_type: NodeKind;
  }>;
}

function groupRelations(
  rows: Array<{ direction: "outgoing" | "incoming"; relation: string; uri: string; name?: string; resource_type?: string }>,
): { incoming: GroupedRel[]; outgoing: GroupedRel[] } {
  const out: Map<string, GroupedRel> = new Map();
  const inc: Map<string, GroupedRel> = new Map();
  for (const r of rows) {
    if (!REL_SET.has(r.relation)) continue;
    const rel = r.relation as RelationKind;
    const map = r.direction === "outgoing" ? out : inc;
    if (!map.has(rel)) map.set(rel, { relation: rel, rows: [] });
    map.get(rel)!.rows.push({
      other_uri: r.uri,
      other_name: r.name || "(unnamed)",
      other_type: KIND_SET.has(r.resource_type ?? "")
        ? (r.resource_type as NodeKind)
        : "document",
    });
  }
  return { outgoing: [...out.values()], incoming: [...inc.values()] };
}

function RelGroup({
  relation,
  direction,
  rows,
  onSelectRelated,
  onFitToNode,
}: {
  relation: RelationKind;
  direction: "in" | "out";
  rows: GroupedRel["rows"];
  onSelectRelated: (rel: RelatedRef) => void;
  onFitToNode: (uri: string) => void;
}) {
  return (
    <div>
      <p className="coord mb-1">
        {direction === "out" ? "→" : "←"} {humanizeRelation(relation)} ({rows.length})
      </p>
      <ul className="flex flex-col gap-px pl-2">
        {rows.map((r) => (
          <li key={r.other_uri} className="group flex items-center gap-1">
            {/* Click selects + centers the related node in the graph (added
                to the graph first if it isn't currently rendered) rather than
                navigating away. */}
            <button
              type="button"
              onClick={() => {
                onSelectRelated({
                  uri: r.other_uri,
                  name: r.other_name,
                  kind: r.other_type,
                  relation,
                  direction: direction === "out" ? "outgoing" : "incoming",
                });
                onFitToNode(r.other_uri);
              }}
              title={`${r.other_name} — select in graph`}
              className="flex-1 inline-flex items-center gap-1 min-w-0 text-left text-[11px] text-foreground hover:text-link cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span className="truncate">{r.other_name}</span>
              <Crosshair
                className="h-2.5 w-2.5 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
                aria-hidden
              />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
