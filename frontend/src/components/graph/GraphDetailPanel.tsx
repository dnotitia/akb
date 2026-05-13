// frontend/src/components/graph/GraphDetailPanel.tsx
import { useState } from "react";
import { ChevronDown, ChevronRight, ExternalLink, Pin, X } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { getDocument, getRelations, getProvenance, drillDown } from "@/lib/api";
import { type NodeKind, type RelationKind } from "./graph-types";

interface Props {
  vault: string;
  docId: string;
  kind: NodeKind;
  uri: string;
  onSelectUri: (uri: string) => void;
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

export function GraphDetailPanel({
  vault,
  docId,
  kind,
  uri,
  onSelectUri,
  onFitToNode,
  onClose,
  onTogglePin,
  pinned,
}: Props) {
  const [metaOpen, setMetaOpen] = useState(false);
  const [sectionsOpen, setSectionsOpen] = useState(false);

  const docQuery = useQuery<DocResponse>({
    queryKey: ["document", vault, docId],
    queryFn: () => getDocument(vault, docId) as Promise<DocResponse>,
  });
  const relQuery = useQuery({
    queryKey: ["relations", vault, docId],
    queryFn: () => getRelations(vault, docId),
  });
  const provQuery = useQuery({
    queryKey: ["provenance", docId],
    queryFn: () => getProvenance(docId),
    enabled: metaOpen,
  });
  const secQuery = useQuery({
    queryKey: ["drill", vault, docId],
    queryFn: () => drillDown(vault, docId),
    enabled: sectionsOpen,
  });

  const doc = docQuery.data;
  const preview = (doc?.content || "").split("\n").slice(0, PREVIEW_LINES).join("\n");

  const groupedRels = groupRelations(relQuery.data?.relations || [], uri);
  const totalRels =
    groupedRels.incoming.reduce((s, g) => s + g.rows.length, 0) +
    groupedRels.outgoing.reduce((s, g) => s + g.rows.length, 0);

  function openDoc() {
    const segment = kind === "table" ? "table" : kind === "file" ? "file" : "doc";
    const path = `/vault/${vault}/${segment}/${encodeURIComponent(docId)}`;
    window.location.assign(path);
  }

  return (
    <aside className="flex flex-col h-full overflow-y-auto border-l border-border bg-surface">
      <header className="flex items-center justify-between px-3 py-2 border-b border-border">
        <span className="coord">{(kind || "document").toUpperCase()} · {docId.toUpperCase()}</span>
        <button onClick={onClose} aria-label="Close detail" className="text-foreground-muted hover:text-foreground">
          <X className="h-3 w-3" />
        </button>
      </header>

      <div className="px-3 py-3 border-b border-border">
        <h2 className="font-serif text-2xl leading-tight mb-1">{doc?.title || "…"}</h2>
        <p className="coord text-foreground-muted truncate">{uri}</p>
        <div className="flex flex-wrap gap-1 mt-3">
          <Button size="sm" variant="accent" onClick={openDoc}>
            <ExternalLink className="h-3 w-3" /> Open
          </Button>
          <Button size="sm" variant="outline" onClick={() => navigator.clipboard.writeText(uri)}>
            Copy URI
          </Button>
          {onTogglePin && (
            <Button size="sm" variant={pinned ? "accent" : "outline"} onClick={onTogglePin}>
              <Pin className="h-3 w-3" /> {pinned ? "Pinned" : "Pin"}
            </Button>
          )}
        </div>
      </div>

      {doc?.summary && (
        <Section label="SUMMARY">
          <p className="text-[12px] leading-relaxed text-foreground">{doc.summary}</p>
        </Section>
      )}

      {kind === "table" && doc?.columns && (
        <Section label="COLUMNS">
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
        <Section label="FILE">
          <p className="coord">
            {doc?.mime_type || "—"} · {doc?.size_bytes ? `${doc.size_bytes} bytes` : "—"}
          </p>
        </Section>
      )}

      {doc?.tags && doc.tags.length > 0 && (
        <Section label="TAGS">
          <div className="flex flex-wrap gap-1">
            {doc.tags.map((t) => (
              <Badge key={t} variant="outline">{t}</Badge>
            ))}
          </div>
        </Section>
      )}

      <Section label={`RELATIONS [${totalRels}]`}>
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
                onSelectUri={onSelectUri}
                onFitToNode={onFitToNode}
              />
            ))}
            {groupedRels.incoming.map((g) => (
              <RelGroup
                key={`in-${g.relation}`}
                relation={g.relation}
                direction="in"
                rows={g.rows}
                onSelectUri={onSelectUri}
                onFitToNode={onFitToNode}
              />
            ))}
          </div>
        )}
      </Section>

      {kind === "document" && (
        <Section label="PREVIEW">
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
                <li key={i} className="coord truncate">‣ {s.heading || s.title || `section ${i}`}</li>
              ))}
            </ul>
          )}
          <pre className="font-mono text-[11px] leading-snug whitespace-pre-wrap text-foreground bg-background border border-border p-2 max-h-64 overflow-auto">
            {preview || "(empty)"}
          </pre>
        </Section>
      )}

      <Section
        label="META"
        rightAction={
          <button
            type="button"
            onClick={() => setMetaOpen((v) => !v)}
            aria-label="Toggle meta"
            aria-expanded={metaOpen}
            className="coord hover:text-foreground inline-flex items-center gap-1"
          >
            {metaOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          </button>
        }
      >
        {metaOpen && (
          <div className="flex flex-col gap-1 text-[11px]">
            <p>
              <span className="coord">author</span> · {doc?.author || "—"}
            </p>
            <p>
              <span className="coord">created</span> · {doc?.created_at || "—"}
            </p>
            <p>
              <span className="coord">updated</span> · {doc?.updated_at || "—"}
            </p>
            <p className="break-all">
              <span className="coord">provenance</span> · {JSON.stringify(provQuery.data?.provenance || "—")}
            </p>
          </div>
        )}
      </Section>
    </aside>
  );
}

function Section({
  label,
  rightAction,
  children,
}: {
  label: string;
  rightAction?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="border-b border-border px-3 py-3">
      <div className="flex items-center justify-between mb-2">
        <span className="coord">§ {label}</span>
        {rightAction}
      </div>
      {children}
    </section>
  );
}

interface GroupedRel {
  relation: RelationKind;
  rows: Array<{
    other_uri: string;
    other_name: string;
    other_type: NodeKind;
    direction: "in" | "out";
  }>;
}

function groupRelations(
  rows: Array<{
    source: string;
    target: string;
    relation: string;
    other_uri?: string;
    other_name?: string;
    other_type?: string;
  }>,
  selfUri: string,
): { incoming: GroupedRel[]; outgoing: GroupedRel[] } {
  const out: Map<string, GroupedRel> = new Map();
  const inc: Map<string, GroupedRel> = new Map();
  for (const r of rows) {
    const rel = r.relation as RelationKind;
    const isOut = r.source === selfUri;
    const map = isOut ? out : inc;
    if (!map.has(rel)) map.set(rel, { relation: rel, rows: [] });
    map.get(rel)!.rows.push({
      other_uri: r.other_uri || (isOut ? r.target : r.source),
      other_name: r.other_name || "(unnamed)",
      other_type: (r.other_type as NodeKind) || "document",
      direction: isOut ? "out" : "in",
    });
  }
  return { outgoing: [...out.values()], incoming: [...inc.values()] };
}

function RelGroup({
  relation,
  direction,
  rows,
  onSelectUri,
  onFitToNode,
}: {
  relation: RelationKind;
  direction: "in" | "out";
  rows: GroupedRel["rows"];
  onSelectUri: (uri: string) => void;
  onFitToNode: (uri: string) => void;
}) {
  return (
    <div>
      <p className="coord mb-1">
        {direction === "out" ? "→" : "←"} {relation} ({rows.length})
      </p>
      <ul className="flex flex-col gap-px pl-2">
        {rows.map((r) => (
          <li key={r.other_uri} className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => onSelectUri(r.other_uri)}
              className="flex-1 text-left text-[11px] hover:text-accent truncate"
            >
              {r.other_name}
            </button>
            <button
              type="button"
              onClick={() => onFitToNode(r.other_uri)}
              aria-label="Center on node"
              className="text-foreground-muted hover:text-foreground"
            >
              ⌖
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
