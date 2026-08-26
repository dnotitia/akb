import {
  ArrowDownLeft,
  ArrowUpRight,
  ChevronDown,
  ChevronRight,
  Crosshair,
  ExternalLink,
  File,
  FileText,
  Network,
  Pin,
  Table2,
  X,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { Skeleton } from "@/components/ui/skeleton";
import { drillDown, getDocument, getProvenance, getRelations } from "@/lib/api";
import { parseUri } from "@/lib/uri";
import {
  ALL_NODE_KINDS,
  ALL_RELATIONS,
  RELATION_LABEL,
  kindToSegment,
  type NodeKind,
  type RelatedRef,
  type RelationKind,
} from "./graph-types";

interface Props {
  vault: string;
  docId: string;
  name: string;
  kind: NodeKind;
  uri: string;
  onSelectRelated: (relation: RelatedRef) => void;
  onFitToNode: (uri: string) => void;
  onClose: () => void;
  onTogglePin?: () => void;
  pinned?: boolean;
  onFocus?: () => void;
}

interface ResourceResponse {
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

interface GroupedRelation {
  relation: RelationKind;
  rows: Array<{ uri: string; name: string; kind: NodeKind }>;
}

const RELATION_SET = new Set<string>(ALL_RELATIONS);
const KIND_SET = new Set<string>(ALL_NODE_KINDS);
const PREVIEW_LINES = 24;

function iconForKind(kind: NodeKind) {
  if (kind === "table") return Table2;
  if (kind === "file") return File;
  return FileText;
}

export function GraphDetailPanel({
  vault,
  docId,
  name,
  kind,
  uri,
  onSelectRelated,
  onFitToNode,
  onClose,
  onTogglePin,
  pinned,
  onFocus,
}: Props) {
  const [metadataOpen, setMetadataOpen] = useState(false);
  const [outlineOpen, setOutlineOpen] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const Icon = iconForKind(kind);
  const parsedUri = parseUri(uri);
  const location = parsedUri?.collection
    ? `${parsedUri.collection} · ${parsedUri.vault}`
    : parsedUri?.vault || vault;

  useEffect(() => {
    titleRef.current?.focus();
  }, [docId]);

  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  const resourceQuery = useQuery<ResourceResponse>({
    queryKey: ["graph-resource", vault, docId],
    queryFn: () => getDocument(vault, docId) as Promise<ResourceResponse>,
    enabled: kind === "document",
  });
  const relationsQuery = useQuery({
    queryKey: ["relations", vault, uri],
    queryFn: () => getRelations(vault, uri),
  });
  const provenanceQuery = useQuery({
    queryKey: ["provenance", vault, docId],
    queryFn: () => getProvenance(vault, docId),
    enabled: metadataOpen && kind === "document",
  });
  const outlineQuery = useQuery({
    queryKey: ["drill", vault, docId],
    queryFn: () => drillDown(vault, docId),
    enabled: outlineOpen && kind === "document",
  });

  const resource = resourceQuery.data;
  const relations = groupRelations(relationsQuery.data?.relations || []);
  const relationCount =
    relations.outgoing.reduce((count, group) => count + group.rows.length, 0) +
    relations.incoming.reduce((count, group) => count + group.rows.length, 0);
  const preview = (resource?.content || "").split("\n").slice(0, PREVIEW_LINES).join("\n");
  const outlineSections = (outlineQuery.data?.sections || []) as Array<{
    heading?: string;
    title?: string;
  }>;

  function openResource() {
    window.location.assign(`/vault/${vault}/${kindToSegment(kind)}/${encodeURIComponent(docId)}`);
  }

  return (
    <aside
      aria-label={`Inspector for ${resource?.title || name}`}
      className="absolute inset-y-3 right-3 z-[var(--z-overlay)] flex w-[min(23rem,calc(100%-1.5rem))] flex-col overflow-hidden rounded-[var(--radius-lg)] border border-border-strong bg-surface shadow-lg max-sm:inset-x-2 max-sm:w-auto"
    >
      <header className="shrink-0 border-b border-border bg-surface-2 px-3 py-3">
        <div className="flex items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-md)] border border-border bg-surface text-primary">
            <Icon className="h-4 w-4" aria-hidden />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-xs font-medium capitalize text-foreground-muted">{kind}</p>
            <h2
              ref={titleRef}
              tabIndex={-1}
              className="mt-0.5 truncate font-display text-lg font-semibold tracking-tight text-foreground focus:outline-none"
              title={resource?.title || name}
            >
              {resource?.title || name}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close inspector"
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:bg-surface-hover hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
        <p className="mt-2 truncate text-xs text-foreground-muted" title={location}>{location}</p>
      </header>

      {kind === "document" && resourceQuery.isLoading ? (
        <div className="flex flex-col gap-3 p-4" role="status" aria-label="Loading resource details">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-32 w-full" />
        </div>
      ) : kind === "document" && resourceQuery.isError ? (
        <div className="p-3">
          <Alert variant="destructive">
            <div>
              <p className="font-medium">Couldn't load this resource.</p>
              <p className="mt-1 text-xs">
                {resourceQuery.error instanceof Error
                  ? resourceQuery.error.message
                  : String(resourceQuery.error)}
              </p>
              <Button type="button" size="sm" variant="outline" className="mt-2" onClick={() => resourceQuery.refetch()}>
                Retry
              </Button>
            </div>
          </Alert>
        </div>
      ) : (
        <>
          <div className="shrink-0 border-b border-border p-3">
            <div className="grid grid-cols-2 gap-2">
              <Button type="button" size="sm" onClick={openResource}>
                <ExternalLink className="h-3.5 w-3.5" aria-hidden />
                Open resource
              </Button>
              {onFocus ? (
                <Button type="button" size="sm" variant="outline" onClick={onFocus}>
                  <Crosshair className="h-3.5 w-3.5" aria-hidden />
                  Focus here
                </Button>
              ) : (
                <Button type="button" size="sm" variant="outline" onClick={() => onFitToNode(uri)}>
                  <Crosshair className="h-3.5 w-3.5" aria-hidden />
                  Center
                </Button>
              )}
            </div>
            <div className="mt-2 flex gap-2">
              <CopyButton value={uri} label="Copy URI" />
              {onTogglePin && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={onTogglePin}
                  aria-pressed={!!pinned}
                  className={pinned ? "border-primary bg-surface-selected text-surface-selected-foreground" : undefined}
                >
                  <Pin className="h-3.5 w-3.5" aria-hidden />
                  {pinned ? "Pinned" : "Pin node"}
                </Button>
              )}
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto rail-scroll">
            <InspectorSection
              title="Relationships"
              description={`${relationCount} direct connection${relationCount === 1 ? "" : "s"}`}
              icon={<Network className="h-4 w-4" aria-hidden />}
            >
              {relationsQuery.isLoading ? (
                <div className="space-y-2" role="status" aria-label="Loading relationships">
                  <Skeleton className="h-9 w-full" />
                  <Skeleton className="h-9 w-full" />
                </div>
              ) : relationCount === 0 ? (
                <div className="rounded-[var(--radius-md)] border border-dashed border-border bg-background px-3 py-4 text-center">
                  <p className="text-sm font-medium text-foreground">No direct relationships</p>
                  <p className="mt-1 text-xs leading-relaxed text-foreground-muted">
                    This resource can still be opened, but there is nothing else to traverse from here yet.
                  </p>
                </div>
              ) : (
                <div className="space-y-3">
                  {relations.outgoing.map((group) => (
                    <RelationGroup key={`out:${group.relation}`} group={group} direction="outgoing" onSelect={onSelectRelated} onFit={onFitToNode} />
                  ))}
                  {relations.incoming.map((group) => (
                    <RelationGroup key={`in:${group.relation}`} group={group} direction="incoming" onSelect={onSelectRelated} onFit={onFitToNode} />
                  ))}
                </div>
              )}
            </InspectorSection>

            {(resource?.summary || resource?.tags?.length) && (
              <InspectorSection title="About">
                {resource.summary && <p className="text-sm leading-relaxed text-foreground">{resource.summary}</p>}
                {!!resource.tags?.length && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {resource.tags.map((tag) => <Badge key={tag} variant="outline">{tag}</Badge>)}
                  </div>
                )}
              </InspectorSection>
            )}

            {kind === "table" && !!resource?.columns?.length && (
              <InspectorSection title="Columns" description={`${resource.columns.length} fields`}>
                <div className="flex flex-wrap gap-1.5">
                  {resource.columns.map((column) => <Badge key={column} variant="secondary">{column}</Badge>)}
                </div>
              </InspectorSection>
            )}

            {kind === "file" && (
              <InspectorSection title="File details">
                <dl className="grid grid-cols-[5rem_1fr] gap-x-3 gap-y-2 text-xs">
                  <dt className="text-foreground-muted">Type</dt>
                  <dd className="truncate text-foreground">{resource?.mime_type || "Not provided"}</dd>
                  <dt className="text-foreground-muted">Size</dt>
                  <dd className="tabular-nums text-foreground">
                    {resource?.size_bytes != null ? `${resource.size_bytes.toLocaleString()} bytes` : "Not provided"}
                  </dd>
                </dl>
              </InspectorSection>
            )}

            {kind === "document" && (
              <InspectorSection title="Document preview">
                <button
                  type="button"
                  onClick={() => setOutlineOpen((open) => !open)}
                  aria-expanded={outlineOpen}
                  className="mb-2 inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-sm)] px-2 text-xs font-medium text-link hover:bg-surface-hover hover:text-link-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {outlineOpen ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
                  {outlineOpen ? "Hide outline" : "Show outline"}
                </button>
                {outlineOpen && (
                  <ul className="mb-3 space-y-1 border-l border-border pl-3 text-xs text-foreground-muted">
                    {outlineSections.map((section, index) => (
                      <li key={`${section.heading || section.title || "section"}:${index}`} className="truncate">
                        {section.heading || section.title || `Section ${index + 1}`}
                      </li>
                    ))}
                    {!outlineQuery.isLoading && !outlineSections.length && <li>No headings found.</li>}
                  </ul>
                )}
                <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-[var(--radius-md)] border border-border bg-background p-3 font-mono text-xs leading-relaxed text-foreground rail-scroll">
                  {preview || "This document has no previewable text."}
                </pre>
              </InspectorSection>
            )}

            <section className="border-t border-border p-3">
              <button
                type="button"
                onClick={() => setMetadataOpen((open) => !open)}
                aria-expanded={metadataOpen}
                aria-label="Toggle metadata"
                className="flex min-h-9 w-full items-center justify-between gap-3 rounded-[var(--radius-sm)] px-2 text-left text-sm font-medium text-foreground transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Metadata
                {metadataOpen ? <ChevronDown className="h-4 w-4 text-foreground-muted" aria-hidden /> : <ChevronRight className="h-4 w-4 text-foreground-muted" aria-hidden />}
              </button>
              {metadataOpen && (
                <dl className="mt-2 grid grid-cols-[5rem_1fr] gap-x-3 gap-y-2 px-2 text-xs">
                  <dt className="text-foreground-muted">Author</dt>
                  <dd className="text-foreground">{resource?.author || "Not provided"}</dd>
                  <dt className="text-foreground-muted">Created</dt>
                  <dd className="text-foreground">{resource?.created_at || "Not provided"}</dd>
                  <dt className="text-foreground-muted">Updated</dt>
                  <dd className="text-foreground">{resource?.updated_at || "Not provided"}</dd>
                  <dt className="text-foreground-muted">Provenance</dt>
                  <dd className="break-all text-foreground">
                    {kind !== "document"
                      ? "Available for documents"
                      : provenanceQuery.isLoading
                      ? "Loading…"
                      : provenanceQuery.data?.provenance
                        ? JSON.stringify(provenanceQuery.data.provenance)
                        : "Not provided"}
                  </dd>
                </dl>
              )}
            </section>
          </div>
        </>
      )}
    </aside>
  );
}

function InspectorSection({ title, description, icon, children }: { title: string; description?: string; icon?: ReactNode; children: ReactNode }) {
  return (
    <section className="border-b border-border p-3">
      <div className="mb-3 flex items-start gap-2">
        {icon && <span className="mt-0.5 text-primary">{icon}</span>}
        <div>
          <h3 className="text-sm font-semibold text-foreground">{title}</h3>
          {description && <p className="mt-0.5 text-xs text-foreground-muted">{description}</p>}
        </div>
      </div>
      {children}
    </section>
  );
}

function groupRelations(rows: Array<{ direction: "outgoing" | "incoming"; relation: string; uri: string; name?: string; resource_type?: string }>): { incoming: GroupedRelation[]; outgoing: GroupedRelation[] } {
  const incoming = new Map<RelationKind, GroupedRelation>();
  const outgoing = new Map<RelationKind, GroupedRelation>();

  for (const row of rows) {
    if (!RELATION_SET.has(row.relation)) continue;
    const relation = row.relation as RelationKind;
    const target = row.direction === "outgoing" ? outgoing : incoming;
    if (!target.has(relation)) target.set(relation, { relation, rows: [] });
    target.get(relation)!.rows.push({
      uri: row.uri,
      name: row.name || "Untitled resource",
      kind: KIND_SET.has(row.resource_type || "") ? (row.resource_type as NodeKind) : "document",
    });
  }

  return { incoming: [...incoming.values()], outgoing: [...outgoing.values()] };
}

function RelationGroup({ group, direction, onSelect, onFit }: { group: GroupedRelation; direction: "incoming" | "outgoing"; onSelect: (relation: RelatedRef) => void; onFit: (uri: string) => void }) {
  const DirectionIcon = direction === "outgoing" ? ArrowUpRight : ArrowDownLeft;
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-foreground-muted">
        <DirectionIcon className="h-3.5 w-3.5" aria-hidden />
        <span>{RELATION_LABEL[group.relation]}</span>
        <span className="tabular-nums">{group.rows.length}</span>
      </div>
      <ul className="overflow-hidden rounded-[var(--radius-md)] border border-border divide-y divide-border">
        {group.rows.map((row) => (
          <li key={row.uri}>
            <button
              type="button"
              aria-label={row.name}
              onClick={() => {
                onSelect({ uri: row.uri, name: row.name, kind: row.kind, relation: group.relation, direction });
                onFit(row.uri);
              }}
              className="flex min-h-10 w-full items-center gap-2 px-2.5 py-2 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
            >
              <span className="min-w-0 flex-1 truncate text-sm text-foreground">{row.name}</span>
              <span className="text-xs capitalize text-foreground-muted">{row.kind}</span>
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
