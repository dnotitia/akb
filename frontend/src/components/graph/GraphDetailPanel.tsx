import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  ArrowDownLeft, ArrowUpRight, ChevronRight, Copy, Crosshair, ExternalLink,
  Eye, EyeOff, FileText, MoreHorizontal, Network, Paperclip, Pin, PinOff, Table2, X,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { TonalIcon } from "@/components/ui/tonal-icon";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { getDocument, getRelations, type RelationRow } from "@/lib/api";
import { parseUri } from "@/lib/uri";
import {
  ALL_NODE_KINDS, ALL_RELATIONS, RELATION_LABEL, kindToSegment,
  type NodeKind, type RelatedRef, type RelationKind,
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
  onOpen?: () => void;
  onPreview?: () => void;
  onExpand?: () => void;
  expanding?: boolean;
  expansionError?: string;
  onHide?: () => void;
}

const RELATION_SET = new Set<string>(ALL_RELATIONS);
const KIND_SET = new Set<string>(ALL_NODE_KINDS);
const RELATION_PAGE_SIZE = 25;
const controlClass = "h-11 w-11 shrink-0 @min-[48rem]/graph:h-9 @min-[48rem]/graph:w-9";
const actionClass = "min-h-11 h-auto whitespace-normal py-2 text-xs @min-[48rem]/graph:min-h-9";
const menuItemClass = "flex min-h-11 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover data-[highlighted]:text-link";

function iconForKind(kind: NodeKind) {
  return kind === "table" ? Table2 : kind === "file" ? Paperclip : FileText;
}

export function GraphDetailPanel({
  vault, docId, name, kind, uri, onSelectRelated, onFitToNode, onClose,
  onTogglePin, pinned, onFocus, onOpen, onPreview, onExpand, expanding = false,
  expansionError, onHide,
}: Props) {
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const canQuery = !!user?.user_id && !checking;
  const [menuOpen, setMenuOpen] = useState(false);
  const [copyStatus, setCopyStatus] = useState<"copied" | "failed" | null>(null);
  const panelRef = useRef<HTMLElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const Icon = iconForKind(kind);
  const parsedUri = parseUri(uri);
  const collection = parsedUri?.collection;

  useEffect(() => {
    const active = document.activeElement;
    if (active instanceof HTMLElement && active !== document.body && !panelRef.current?.contains(active)) {
      triggerRef.current = active;
    }
    titleRef.current?.focus({ preventScroll: true });
  }, [uri]);

  // Optional description enrichment must never gate navigation or relationships.
  const resourceQuery = useQuery({
    queryKey: ["graph-resource", user?.user_id, revision, vault, docId],
    queryFn: () => getDocument(vault, docId),
    enabled: canQuery && kind === "document",
    retry: false,
  });
  const relationsQuery = useQuery({
    queryKey: ["relations", user?.user_id, revision, vault, uri],
    queryFn: () => getRelations(vault, uri),
    enabled: canQuery,
    retry: false,
  });
  const relations = useMemo(() => normalizeRelations(relationsQuery.data?.relations ?? []), [relationsQuery.data]);
  const title = (kind === "document" && resourceQuery.data?.title) || name;
  const summary = kind === "document" ? resourceQuery.data?.summary : undefined;
  const relationCount = relations.outgoing.length + relations.incoming.length;

  function closeInspector() {
    if (triggerRef.current?.isConnected) triggerRef.current.focus({ preventScroll: true });
    onClose();
  }

  function openResource() {
    if (onOpen) return onOpen();
    window.location.assign(`/vault/${encodeURIComponent(vault)}/${kindToSegment(kind)}/${encodeURIComponent(docId)}`);
  }

  async function copyUri() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(uri);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("failed");
    }
  }

  return (
    <aside
      ref={panelRef}
      aria-label={`Inspector for ${title}`}
      onKeyDown={(event) => {
        // Radix portals bubble through React; only this inspector's own DOM
        // handles Escape. A preview or menu owns its own dismissal.
        if (!event.currentTarget.contains(event.target as Node)) return;
        event.stopPropagation();
        if (event.key === "Escape" && !event.defaultPrevented && !menuOpen) {
          event.preventDefault();
          closeInspector();
        }
      }}
      className="absolute inset-x-2 bottom-14 z-[var(--z-overlay)] flex max-h-[min(68%,32rem,calc(100%-var(--graph-tools-bottom,0px)-4rem))] flex-col overflow-hidden rounded-[var(--radius-lg)] border border-border-strong bg-surface shadow-md @min-[48rem]/graph:inset-x-auto @min-[48rem]/graph:bottom-auto @min-[48rem]/graph:right-3 @min-[48rem]/graph:top-[var(--graph-tools-bottom,0.75rem)] @min-[48rem]/graph:max-h-[calc(100%-var(--graph-tools-bottom,0px)-3rem)] @min-[48rem]/graph:w-[22rem]"
    >
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5">
        <TonalIcon tone={kind === "table" ? "data" : kind === "file" ? "file" : "knowledge"} size="sm"><Icon aria-hidden /></TonalIcon>
        <div className="min-w-0 flex-1 text-xs text-foreground-muted">
          <span className="capitalize">{kind}</span>
          {collection && <><span className="mx-1.5" aria-hidden>·</span><span className="break-words">{collection}</span></>}
        </div>
        <DropdownMenu.Root modal={false} open={menuOpen} onOpenChange={(open) => { setMenuOpen(open); if (open) setCopyStatus(null); }}>
          <DropdownMenu.Trigger asChild>
            <Button type="button" variant="ghost" size="icon" aria-label="Resource actions" className={controlClass}><MoreHorizontal className="h-4 w-4" aria-hidden /></Button>
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content
              align="end" sideOffset={4}
              onEscapeKeyDown={(event) => event.stopPropagation()}
              className="z-[var(--z-popover)] max-w-[min(20rem,calc(100vw-2rem))] rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
            >
              {onTogglePin && <DropdownMenu.Item className={menuItemClass} onSelect={onTogglePin}>
                {pinned ? <PinOff className="h-4 w-4" aria-hidden /> : <Pin className="h-4 w-4" aria-hidden />}
                {pinned ? "Unpin position" : "Pin position"}
              </DropdownMenu.Item>}
              <DropdownMenu.Item className={menuItemClass} onSelect={() => onFitToNode(uri)}><Crosshair className="h-4 w-4" aria-hidden />Center on resource</DropdownMenu.Item>
              {onHide && <DropdownMenu.Item className={menuItemClass} onSelect={onHide}><EyeOff className="h-4 w-4" aria-hidden />Hide from this view</DropdownMenu.Item>}
              <DropdownMenu.Separator className="my-1 h-px bg-border" />
              <DropdownMenu.Item className={menuItemClass} onSelect={(event) => { event.preventDefault(); void copyUri(); }}><Copy className="h-4 w-4" aria-hidden />Copy URI</DropdownMenu.Item>
              {copyStatus && <p role="status" className="px-3 py-2 text-xs text-foreground-muted">{copyStatus === "copied" ? "URI copied" : "Couldn't copy. Select the URI below."}</p>}
              {copyStatus === "failed" && <code className="block select-all break-all px-3 pb-2 text-xs text-foreground">{uri}</code>}
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
        <Button type="button" variant="ghost" size="icon" onClick={closeInspector} aria-label="Close inspector" className={controlClass}><X className="h-4 w-4" aria-hidden /></Button>
      </div>

      <div className="min-h-0 overflow-y-auto overscroll-contain rail-scroll">
        <header className="p-4 pb-3">
          <h2 ref={titleRef} id={titleId} tabIndex={-1} className="break-words font-display text-lg font-semibold leading-snug tracking-tight text-foreground focus:outline-none">{title}</h2>
          {summary && <p className="mt-2 line-clamp-3 break-words text-sm leading-relaxed text-foreground-muted">{summary}</p>}
          <div className="mt-3 flex flex-wrap gap-2">
            {kind === "document" && onPreview && <Button id="graph-preview-trigger" type="button" size="sm" onClick={onPreview} className={`flex-1 ${actionClass}`}><Eye className="h-3.5 w-3.5 shrink-0" aria-hidden />Preview</Button>}
            <Button type="button" size="sm" variant={kind === "document" && onPreview ? "outline" : "default"} onClick={openResource} className={`flex-1 ${actionClass}`}><ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden />Open in vault</Button>
          </div>
          {(onFocus || onExpand) && <div className="mt-2 flex flex-col gap-1">
            {onFocus && <Button type="button" variant="ghost" size="sm" onClick={onFocus} className={`justify-start text-link ${actionClass}`}><Network className="h-3.5 w-3.5 shrink-0" aria-hidden />Explore connections</Button>}
            {onExpand && <Button type="button" variant="ghost" size="sm" onClick={onExpand} loading={expanding} className={`justify-start text-link ${actionClass}`}>
              {expanding ? "Loading connections…" : expansionError ? "Retry loading connections" : "Load connections"}
            </Button>}
          </div>}
          {expansionError && <Alert variant="destructive" className="mt-2 text-xs">{expansionError}</Alert>}
        </header>

        <section aria-label="Connections" className="border-t border-border px-4 py-3">
          <div className="mb-3 flex items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-foreground">Connections</h3>
            {relationsQuery.isSuccess && <span className="text-xs tabular-nums text-foreground-muted">{relationCount}</span>}
          </div>
          {relationsQuery.isLoading ? (
            <LoadingState label="Loading connections" className="space-y-2"><Skeleton className="h-10 w-full" /><Skeleton className="mt-2 h-10 w-full" /></LoadingState>
          ) : relationsQuery.isError ? (
            <Alert variant="destructive"><div>
              <p className="font-medium">Couldn't load connections.</p>
              <p className="mt-1 break-words text-xs">{relationsQuery.error instanceof Error ? relationsQuery.error.message : "Please try again."}</p>
              <Button type="button" size="sm" variant="outline" className="mt-2" onClick={() => relationsQuery.refetch()}>Retry connections</Button>
            </div></Alert>
          ) : relationCount === 0 ? (
            <p className="py-2 text-sm text-foreground-muted">No direct connections</p>
          ) : (
            <div className="space-y-4" key={uri}>
              {!!relations.outgoing.length && <RelationList rows={relations.outgoing} direction="outgoing" onSelect={onSelectRelated} onFit={onFitToNode} />}
              {!!relations.incoming.length && <RelationList rows={relations.incoming} direction="incoming" onSelect={onSelectRelated} onFit={onFitToNode} />}
            </div>
          )}
        </section>
      </div>
    </aside>
  );
}

function normalizeRelations(rows: RelationRow[]): { incoming: RelatedRef[]; outgoing: RelatedRef[] } {
  const groups: { incoming: RelatedRef[]; outgoing: RelatedRef[] } = { incoming: [], outgoing: [] };
  for (const row of rows) {
    if (!RELATION_SET.has(row.relation)) continue;
    const parsedKind = parseUri(row.uri)?.kind;
    const inferredKind = parsedKind === "file" || parsedKind === "table" ? parsedKind : "document";
    groups[row.direction].push({
      uri: row.uri, name: row.name || "Untitled resource",
      kind: KIND_SET.has(row.resource_type || "") ? row.resource_type as NodeKind : inferredKind,
      relation: row.relation as RelationKind, direction: row.direction,
      source: row.kind,
    });
  }
  return groups;
}

function RelationList({ rows, direction, onSelect, onFit }: {
  rows: RelatedRef[];
  direction: "incoming" | "outgoing";
  onSelect: (relation: RelatedRef) => void;
  onFit: (uri: string) => void;
}) {
  const [visibleCount, setVisibleCount] = useState(RELATION_PAGE_SIZE);
  const headingId = useId();
  const DirectionIcon = direction === "outgoing" ? ArrowUpRight : ArrowDownLeft;
  const label = direction === "outgoing" ? "Outgoing connections" : "Incoming connections";
  return (
    <section aria-label={label}>
      <h4 id={headingId} className="mb-1 flex items-center gap-1.5 text-xs font-medium text-foreground-muted"><DirectionIcon className="h-3.5 w-3.5" aria-hidden />{direction === "outgoing" ? "Outgoing" : "Incoming"}<span className="ml-auto tabular-nums">{rows.length}</span></h4>
      <ul aria-labelledby={headingId} className="divide-y divide-border">
        {rows.slice(0, visibleCount).map((row, index) => {
          const Icon = iconForKind(row.kind);
          return <li key={`${row.uri}:${row.relation}:${row.source}:${index}`}>
            <button
              type="button" aria-label={row.name}
              onClick={() => {
                onSelect(row);
                onFit(row.uri);
              }}
              className="flex min-h-11 w-full items-center gap-2 rounded-[var(--radius-sm)] px-1 py-2 text-left transition-token hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
            >
              <Icon className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
              <span className="min-w-0 flex-1"><span className="block line-clamp-2 break-words text-sm text-foreground">{row.name}</span><span className="mt-0.5 block text-xs text-foreground-muted"><span className="capitalize">{RELATION_LABEL[row.relation]}</span>{row.source && <span> · {row.source === "implicit" ? "Body link" : "Explicit relation"}</span>}</span></span>
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
            </button>
          </li>;
        })}
      </ul>
      {visibleCount < rows.length && <Button type="button" variant="ghost" size="sm" className={`mt-1 w-full text-link ${actionClass}`} onClick={() => setVisibleCount((count) => count + RELATION_PAGE_SIZE)}>Show more ({rows.length - visibleCount} remaining)</Button>}
    </section>
  );
}
