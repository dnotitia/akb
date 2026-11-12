import { type MouseEvent, type RefObject } from "react";
import { ArrowDownLeft, ArrowUpRight, FileText, List, Network } from "lucide-react";
import type { RelationRow } from "@/lib/api";
import { readingReference, type ReadingDocument } from "@/lib/reading-workspace";
import { parseUri } from "@/lib/uri";
import { hrefFor } from "@/components/relations/relation-row-utils";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";

/** Document relationships belong beside the outline, with one shared preview flow. */
export function DocumentReferences({ vault, title, relations, loading, error, historical, onRetry, onOpen, onManage, manageRef, mode, onModeChange }: {
  vault: string; title: string; relations: RelationRow[]; loading: boolean; error: boolean; historical: boolean;
  onRetry: () => void; onOpen: (target: ReadingDocument, opener?: HTMLElement) => void;
  onManage: (opener: HTMLButtonElement) => void; manageRef?: RefObject<HTMLButtonElement | null>;
  mode: "map" | "list"; onModeChange: (mode: "map" | "list") => void;
}) {
  const graph = relations.slice(0, 6);
  const choose = (event: MouseEvent<HTMLAnchorElement>, row: RelationRow) => {
    if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const target = readingReference(hrefFor(row, vault), window.location.origin);
    if (!target) return;
    event.preventDefault(); onOpen(target, event.currentTarget);
  };
  return <section className="document-references" aria-label="Document references">
    <div className="document-references-heading"><h2 tabIndex={-1}>References</h2>
      {!loading && !error && <span className="document-reference-count" role="status" aria-label={`${relations.length} ${relations.length === 1 ? "connection" : "connections"}`}>{relations.length}</span>}
      <div className="document-reference-views" role="group" aria-label="Reference view">
        <Button variant="ghost" size="icon" aria-pressed={mode === "map"} aria-label="Reference map" title="Reference map" onClick={() => onModeChange("map")}><Network size={13} aria-hidden /></Button>
        <Button variant="ghost" size="icon" aria-pressed={mode === "list"} aria-label="Reference list" title="Reference list" onClick={() => onModeChange("list")}><List size={13} aria-hidden /></Button>
      </div>
    </div>
    <div className="document-reference-body rail-scroll">
    {historical && <p className="reading-lens-note">References from the latest document.</p>}
    {loading ? <LoadingState label="Loading references" delayMs={200}><Skeleton className="m-4 h-24" /></LoadingState>
      : error ? <div className="reading-lens-empty">References could not be loaded.<Button size="sm" variant="ghost" onClick={onRetry}>Try again</Button></div>
        : !relations.length ? <p className="reading-lens-empty">No connected resources yet. Document links and added relations appear here.</p> : <>
          {mode === "map" && <div className="reading-reference-map" role="group" aria-label={`Direct connections to ${title}`}>
            <div className="reading-map-center"><FileText size={14} aria-hidden /><span title={title}>{title}</span></div>
            <div className="reading-map-neighbors">{graph.map((row, index) => <a key={`${row.uri}:${row.relation}:${row.direction}:${index}`} data-reference-link data-reference-key={`${row.direction}:${row.relation}:${row.uri}`} href={hrefFor(row, vault)} onClick={event => choose(event, row)}
              aria-label={`${row.direction === "incoming" ? "From" : "To"}: ${row.name || parseUri(row.uri)?.id}. ${row.relation.replaceAll("_", " ")}`}>
              {row.direction === "incoming" ? <ArrowDownLeft size={13} aria-hidden /> : <ArrowUpRight size={13} aria-hidden />}<span>{row.name || parseUri(row.uri)?.id}</span></a>)}</div>
          </div>}
          {mode === "list" && <div className="reading-reference-list">{(["outgoing", "incoming"] as const).map(direction => {
            const rows = relations.filter(row => row.direction === direction);
            return !!rows.length && <div key={direction}><p className="reading-caption">{direction === "outgoing" ? "From this document" : "To this document"} · {rows.length}</p>
              {rows.map((row, index) => <a key={`${row.uri}:${row.relation}:${index}`} data-reference-link data-reference-key={`${row.direction}:${row.relation}:${row.uri}`} href={hrefFor(row, vault)} onClick={event => choose(event, row)} className="reading-reference-row">
                {direction === "incoming" ? <ArrowDownLeft size={14} aria-hidden /> : <ArrowUpRight size={14} aria-hidden />}<span>{row.name || parseUri(row.uri)?.id}<small>{row.relation.replaceAll("_", " ")} · {row.kind === "implicit" ? "From document" : "Added relation"}</small></span>
              </a>)}</div>;
          })}</div>}
          {mode === "map" && relations.length > graph.length && <button type="button" className="reading-map-more" onClick={() => onModeChange("list")}>Show all {relations.length} connections</button>}
        </>}
    </div>
    <div className="reading-connections-entry"><Button ref={manageRef} variant="ghost" size="sm" onClick={event => onManage(event.currentTarget)}>Connection details</Button></div>
  </section>;
}
