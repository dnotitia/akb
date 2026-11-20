import { useId, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import * as Popover from "@radix-ui/react-popover";
import { Check, FileText, History, List, MoreHorizontal, RotateCcw, Search, X } from "lucide-react";
import { useReadingWorkspace } from "@/contexts/reading-workspace-context";
import { readingDocumentKey, type ReadingDocument } from "@/lib/reading-workspace";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TooltipText } from "@/components/ui/tooltip-text";
import { ReadingTabActionsMenu } from "@/components/reading-tab-actions-menu";

const searchable = (text: string) => text.normalize("NFC").toLocaleLowerCase();

export function ReadingTabManager({ active }: { active: string }) {
  const workspace = useReadingWorkspace()!;
  const navigate = useNavigate();
  const titleId = useId();
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"open" | "closed">("open");
  const [filter, setFilter] = useState("");
  const [notice, setNotice] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const query = searchable(filter.trim());
  const activeDoc = workspace.tabs.find(doc => readingDocumentKey(doc) === active);
  const items = (mode === "open" ? workspace.tabs : workspace.closed).filter(doc => searchable(`${doc.title} ${doc.path} ${doc.vault}`).includes(query));
  const visit = (doc: ReadingDocument) => { setOpen(false); navigate(doc.href); };
  const afterAction = (dismiss: boolean) => { if (dismiss) setOpen(false); else setNotice("Document tabs reordered"); };
  const focusRow = (index: number) => listRef.current?.querySelectorAll<HTMLButtonElement>("[data-tab-target]")[index]?.focus();
  const onListKey = (event: KeyboardEvent<HTMLUListElement>) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.nativeEvent.isComposing || !(event.target instanceof HTMLButtonElement) || !event.target.hasAttribute("data-tab-target")) return;
    const buttons = [...(listRef.current?.querySelectorAll<HTMLButtonElement>("[data-tab-target]") ?? [])];
    const index = buttons.indexOf(event.target);
    const next = event.key === "ArrowDown" ? Math.min(index + 1, buttons.length - 1) : event.key === "ArrowUp" ? index - 1 : event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : null;
    if (next !== null) { event.preventDefault(); if (next < 0) searchRef.current?.focus(); else focusRow(next); }
  };
  return <Popover.Root open={open} onOpenChange={next => { setOpen(next); if (next) setMode("open"); setFilter(""); }}>
    <Popover.Trigger asChild><Button variant="ghost" size="icon" className="reading-icon-button tab-manager-trigger" aria-label="Open document list" title="Manage tabs"><List size={18} aria-hidden /></Button></Popover.Trigger>
    <Popover.Portal><Popover.Content className="tab-manager" side="top" align="end" sideOffset={10} collisionPadding={12} aria-labelledby={titleId}
      onKeyDown={event => { if (event.altKey && event.shiftKey && !event.ctrlKey && !event.metaKey && !event.repeat && !event.nativeEvent.isComposing && event.code === "KeyT" && workspace.closed[0]) { event.preventDefault(); event.stopPropagation(); visit(workspace.closed[0]); } }}
      onOpenAutoFocus={event => { event.preventDefault(); searchRef.current?.focus(); }}>
      <header className="tab-manager-header"><h2 id={titleId}>Tabs</h2><div className="flex items-center gap-1">
        {activeDoc && <ReadingTabActionsMenu doc={activeDoc} active={active} global onAction={afterAction}><Button variant="ghost" size="icon" className="tab-manager-icon" aria-label="Tab list actions"><MoreHorizontal size={16} aria-hidden /></Button></ReadingTabActionsMenu>}
        <Popover.Close asChild><Button variant="ghost" size="icon" className="tab-manager-icon" aria-label="Close tab manager"><X size={16} aria-hidden /></Button></Popover.Close>
      </div></header>
      <Tabs value={mode} activationMode="manual" onValueChange={value => { setMode(value as typeof mode); setFilter(""); }} className="tab-manager-body">
        <TabsList className="tab-manager-modes" aria-label="Document list mode">
          <TabsTrigger value="open" aria-label={`Open (${workspace.tabs.length})`}>Open<span className="tab-manager-count">{workspace.tabs.length}</span></TabsTrigger>
          <TabsTrigger value="closed" aria-label={`Recently closed (${workspace.closed.length})`}>Recently closed<span className="tab-manager-count">{workspace.closed.length}</span></TabsTrigger>
        </TabsList>
        <div className="tab-manager-search"><Search size={15} aria-hidden /><Input ref={searchRef} type="search" aria-label={mode === "open" ? "Find open documents" : "Find recently closed documents"} placeholder="Search by title, path or Vault" value={filter} onChange={event => setFilter(event.target.value)}
          onKeyDown={event => { if (event.key === "ArrowDown" && !event.nativeEvent.isComposing && items.length) { event.preventDefault(); focusRow(0); } }} /></div>
        <TabsContent value={mode} className="tab-manager-panel">
          <ul ref={listRef} className="tab-manager-list rail-scroll" aria-label={mode === "open" ? "Open tabs" : "Recently closed tabs"} onKeyDown={onListKey}>
            {items.map(doc => {
              const key = readingDocumentKey(doc), current = key === active;
              return <li key={key} className="tab-manager-row" data-current={current && mode === "open"}>
                <button type="button" data-tab-target className="tab-manager-document" onClick={() => visit(doc)} aria-current={current && mode === "open" ? "page" : undefined}>
                  {mode === "open" ? <FileText size={17} aria-hidden /> : <RotateCcw size={17} aria-hidden />}
                  <span className="tab-manager-identity"><TooltipText className="tab-manager-title">{doc.title}</TooltipText><TooltipText id={`${titleId}-${encodeURIComponent(key)}`} className="tab-manager-path">{`${doc.vault} / ${doc.path}${doc.version ? ` · ${doc.version.slice(0, 8)}` : ""}`}</TooltipText></span>
                  {current && mode === "open" && <span className="tab-manager-current"><Check size={12} aria-hidden /><span>Current</span></span>}
                </button>
                {mode === "open" && <div className="tab-manager-actions">
                  <ReadingTabActionsMenu doc={doc} active={active} onAction={afterAction}><Button type="button" variant="ghost" size="icon" className="tab-manager-icon" aria-label={`Tab actions for ${doc.title}`} aria-describedby={`${titleId}-${encodeURIComponent(key)}`}><MoreHorizontal size={16} aria-hidden /></Button></ReadingTabActionsMenu>
                  <Button type="button" variant="ghost" size="icon" className="tab-manager-icon tab-manager-close" aria-label={`Close ${doc.title} from list`} aria-describedby={`${titleId}-${encodeURIComponent(key)}`} onClick={() => {
                    if (key === active) { setOpen(false); requestAnimationFrame(() => { document.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus({ preventScroll: true }); workspace.close(key, active); }); }
                    else { workspace.close(key, active); requestAnimationFrame(() => searchRef.current?.focus()); }
                  }}><X size={15} aria-hidden /></Button>
                </div>}
              </li>;
            })}
          </ul>
          {!items.length && <div className="tab-manager-empty" role="status">{query ? <Search size={22} aria-hidden /> : <History size={22} aria-hidden />}<p>{query ? "No matching tabs" : mode === "closed" ? "No recently closed tabs" : "No open tabs"}</p><span>{query ? "Try another title, path or Vault." : mode === "closed" ? "Closed tabs will appear here." : "Open a document to start reading."}</span></div>}
        </TabsContent>
      </Tabs>
      {!!workspace.closed.length && <footer className="tab-manager-footer"><Button type="button" variant="ghost" size="sm" onClick={() => visit(workspace.closed[0])}><RotateCcw size={14} aria-hidden />Reopen last closed</Button><span aria-hidden><kbd>Alt</kbd><kbd>⇧</kbd><kbd>T</kbd></span></footer>}
      <span role="status" className="sr-only">{notice}{query && ` ${items.length} matching tabs`}</span>
    </Popover.Content></Popover.Portal>
  </Popover.Root>;
}
