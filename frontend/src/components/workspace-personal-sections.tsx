import { useEffect, useId, useReducer, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { ChevronDown, FileText, Folder, Pencil, X } from "lucide-react";
import { TooltipText } from "@/components/ui/tooltip-text";
import { cn } from "@/lib/utils";
import { readRecentDocumentViews, RECENT_DOCUMENT_VIEWS_EVENT, removeRecentDocumentView } from "@/lib/recent-document-views";
import { listWorkspaceDrafts, workspaceDraftHref, WORKSPACE_DRAFTS_EVENT } from "@/lib/document-draft";
import { readWorkspaceShortcuts, removeWorkspaceShortcut, workspaceShortcutHref, WORKSPACE_SHORTCUTS_EVENT } from "@/lib/workspace-shortcuts";

const focus = "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset";

export function WorkspacePersonalSections({ userId, vaults }: { userId: string; vaults: { name: string; role?: string }[] }) {
  const [, refresh] = useReducer(value => value + 1, 0);
  const { pathname, search } = useLocation();
  useEffect(() => {
    const events = ["storage", "focus", RECENT_DOCUMENT_VIEWS_EVENT, WORKSPACE_DRAFTS_EVENT, WORKSPACE_SHORTCUTS_EVENT];
    for (const event of events) window.addEventListener(event, refresh);
    // Edit drafts expire even if this page is left open.
    const timer = window.setInterval(refresh, 60_000);
    return () => {
      for (const event of events) window.removeEventListener(event, refresh);
      window.clearInterval(timer);
    };
  }, []);
  const allowed = new Set(vaults.map(v => v.name));
  const writable = new Set(vaults.filter(v => ["writer", "admin", "owner"].includes(v.role ?? "")).map(v => v.name));
  const recent = readRecentDocumentViews(userId).filter(item => allowed.has(item.vault));
  const drafts = listWorkspaceDrafts(userId).filter(item => writable.has(item.vault));
  const shortcuts = readWorkspaceShortcuts(userId).filter(item => allowed.has(item.vault));
  const rows = {
    recent: recent.map(item => ({ key: `${item.vault}:${item.path}`, title: item.title, context: `${item.vault} / ${item.path}`, to: `/vault/${encodeURIComponent(item.vault)}/doc/${encodeURIComponent(item.path)}`, icon: <FileText className="h-3.5 w-3.5" />, remove: () => removeRecentDocumentView(userId, item.vault, item.path) })),
    drafts: drafts.map(item => ({ key: `${item.kind}:${item.vault}:${item.document ?? "new"}`, title: item.title.trim() || "Untitled document", context: `${item.vault} · Saved ${new Date(item.updatedAt).toLocaleString()}`, to: workspaceDraftHref(item), icon: <Pencil className="h-3.5 w-3.5" /> })),
    pinned: shortcuts.map(item => ({ key: `${item.kind}:${item.vault}:${item.path}`, title: item.title, context: `${item.vault} / ${item.path}`, to: workspaceShortcutHref(item), icon: item.kind === "collection" ? <Folder className="h-3.5 w-3.5" /> : <FileText className="h-3.5 w-3.5" />, remove: () => removeWorkspaceShortcut(userId, item) })),
  };
  return <div className="space-y-2 px-2 pb-3">
    <PersonalSection title="Recently viewed" rows={rows.recent} current={pathname + search} />
    <PersonalSection title="Drafts" rows={rows.drafts} current={pathname + search} description="Saved in this browser. Edit drafts expire after 24 hours." />
    <PersonalSection title="Pinned" rows={rows.pinned} current={pathname + search} description="Your document and Collection shortcuts" />
  </div>;
}

interface PersonalRow { key: string; title: string; context: string; to: string; icon: ReactNode; remove?: () => void | boolean }

function PersonalSection({ title, rows, current, description }: { title: string; rows: PersonalRow[]; current: string; description?: string }) {
  const id = useId();
  const summary = useRef<HTMLElement>(null);
  const [all, setAll] = useState(false);
  const [error, setError] = useState(false);
  if (!rows.length) return null;
  return <details className="group/section" onToggle={event => { if (!event.currentTarget.open) setAll(false); }}>
    <summary ref={summary} className={cn("flex h-9 cursor-pointer list-none items-center justify-between rounded-[var(--radius-sm)] px-3 text-xs font-medium text-foreground-muted hover:bg-surface-hover [&::-webkit-details-marker]:hidden", focus)}>
      {title}<ChevronDown className="h-3 w-3 -rotate-90 group-open/section:rotate-0" aria-hidden />
    </summary>
    <nav aria-label={title} aria-describedby={description ? id : undefined}>
      {description && <p id={id} className="pl-6 pr-3 pb-2 text-xs text-foreground-muted">{description}</p>}
      <ul className="space-y-0.5">{(all ? rows : rows.slice(0, 3)).map(row => <li key={row.key} className={cn("group/row flex min-w-0 items-center rounded-[var(--radius-sm)]", current === row.to ? "bg-surface-selected text-surface-selected-foreground" : "text-foreground-muted hover:bg-surface-hover")}>
        <Link to={row.to} aria-current={current === row.to ? "page" : undefined} className={cn("flex min-h-9 min-w-0 flex-1 items-center gap-2 rounded-[var(--radius-sm)] pl-6 pr-1 text-sm", focus)}>
          <span className="shrink-0" aria-hidden>{row.icon}</span>
          <TooltipText tip={`${row.title} — ${row.context}`} className="min-w-0 truncate">{row.title}</TooltipText>
        </Link>
        {row.remove && <button type="button" aria-label={`Remove ${row.title} from ${title.toLowerCase()}`} onClick={() => {
          const failed = row.remove?.() === false;
          setError(failed);
          if (!failed) requestAnimationFrame(() => (summary.current ?? document.getElementById("workspace-vaults-link"))?.focus());
        }} className={cn("mr-1 flex h-8 w-7 shrink-0 items-center justify-center rounded-[var(--radius-sm)] opacity-0 group-hover/row:opacity-100 group-focus-within/row:opacity-100 hover:bg-surface-active", focus)}><X className="h-3 w-3" aria-hidden /></button>}
      </li>)}</ul>
      {rows.length > 3 && <button type="button" onClick={() => setAll(value => !value)} className={cn("flex h-9 w-full items-center rounded-[var(--radius-sm)] pl-6 pr-3 text-xs text-link hover:bg-surface-hover", focus)}>{all ? "Show less" : `Show ${rows.length - 3} more`}</button>}
      {error && <p role="alert" className="px-3 py-2 text-xs text-destructive">Could not remove this shortcut. Browser storage may be unavailable.</p>}
    </nav>
  </details>;
}
