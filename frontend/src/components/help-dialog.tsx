import { useRef, useState, type RefObject } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, ArrowRight, ChevronRight, CircleHelp, FileText, PlugZap, Search } from "lucide-react";
import { useSettingsDialog } from "@/contexts/settings-dialog-context";
import { ConnectionTroubleshooting } from "@/components/connection-troubleshooting";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";

const TOPICS = [
  { id: "find", title: "Find a document", description: "Search across Vaults or look inside a document.", Icon: Search },
  { id: "write", title: "Write and organise", description: "Create a document and keep it in a Collection.", Icon: FileText },
  { id: "connect", title: "Use AKB in an AI tool", description: "Connect your tool to knowledge you can access.", Icon: PlugZap },
  { id: "trouble", title: "Solve a problem", description: "Check sign-in, missing Vaults or an AI connection.", Icon: CircleHelp },
] as const;
type Topic = typeof TOPICS[number]["id"];

/** Shared by the desktop sidebar and the account menu on narrow screens. */
export function HelpDialog({ open, onOpenChange, returnFocusRef }: {
  open: boolean; onOpenChange: (open: boolean) => void; returnFocusRef: RefObject<HTMLElement | null>;
}) {
  const settings = useSettingsDialog();
  const navigate = useNavigate();
  const pendingAction = useRef<(() => void) | null>(null);
  const leave = (action: () => void) => { pendingAction.current = action; onOpenChange(false); };
  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent className="help-dialog flex max-w-xl flex-col gap-0 overflow-hidden p-0" onCloseAutoFocus={event => {
      event.preventDefault();
      const action = pendingAction.current;
      pendingAction.current = null;
      if (action) requestAnimationFrame(action);
      else returnFocusRef.current?.focus({ preventScroll: true });
    }}>
      {open && <HelpContents onNavigate={href => leave(() => navigate(href))}
        onConnect={() => leave(() => settings ? settings.openSettings("tokens", returnFocusRef.current) : navigate("/settings?tab=tokens"))} />}
    </DialogContent>
  </Dialog>;
}

function HelpContents({ onNavigate, onConnect }: { onNavigate: (href: string) => void; onConnect: () => void }) {
  const [topic, setTopic] = useState<Topic | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const firstTopic = useRef<HTMLButtonElement>(null);
  const active = TOPICS.find(item => item.id === topic);
  const select = (next: Topic) => { setTopic(next); requestAnimationFrame(() => { if (body.current) body.current.scrollTop = 0; heading.current?.focus({ preventScroll: true }); }); };
  return <>
    <DialogHeader className="shrink-0 border-b border-border px-5 py-4 pr-14">
      {topic && <Button variant="ghost" size="sm" className="mb-1 -ml-2 w-fit text-link" onClick={() => { setTopic(null); requestAnimationFrame(() => { if (body.current) body.current.scrollTop = 0; firstTopic.current?.focus({ preventScroll: true }); }); }}><ArrowLeft className="h-4 w-4" aria-hidden />All help topics</Button>}
      <DialogTitle ref={heading} tabIndex={-1}>{active?.title ?? "Help"}</DialogTitle>
      <DialogDescription>{active?.description ?? "What would you like to do?"}</DialogDescription>
    </DialogHeader>
    <div ref={body} className="min-h-0 overflow-y-auto p-5 text-sm leading-relaxed">

      {!topic && <div className="space-y-2">
        {TOPICS.map(({ id, title, description, Icon }, index) => <button key={id} ref={index === 0 ? firstTopic : undefined} type="button" onClick={() => select(id)}
          className="flex min-h-16 w-full items-center gap-3 rounded-[var(--radius-md)] border border-border p-3 text-left transition-colors hover:border-border-strong hover:bg-surface-hover focus-ring-instant">
          <Icon className="h-5 w-5 shrink-0 text-link" aria-hidden /><span className="min-w-0 flex-1"><span className="block font-medium text-foreground">{title}</span><span className="mt-0.5 block text-xs leading-relaxed text-foreground-muted">{description}</span></span><ChevronRight className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
        </button>)}
        <p className="pt-3 text-xs text-foreground-muted">AKB organises knowledge in Vaults — shared spaces containing Collections, documents, tables and files. Your account determines what you can access.</p>
      </div>}
      {topic === "find" && <div className="space-y-4">
        <ol className="list-decimal space-y-3 pl-5"><li>Use <strong>Search knowledge</strong> in the top bar to search across the Vaults you can access.</li><li>Open a result to read it. In a document, use <strong>Find in document</strong> for matching words or <strong>Table of contents</strong> for a section.</li><li>To browse by location, open <strong>Vaults</strong>, choose a Vault, then a Collection.</li></ol>
        <p className="text-foreground-muted">A sidebar name filter narrows that sidebar’s entries. To search document content within a Collection, use <strong>Search in collection</strong> in its actions menu.</p>
        <Button onClick={() => onNavigate("/search")}>Open Search<ArrowRight className="h-4 w-4" aria-hidden /></Button>
        <details className="connection-inline-help"><summary>Keep something easy to find</summary><p>Star a Vault or pin a document or Collection for a personal shortcut. Watch a document to receive changes in Notifications. These actions keep your existing sharing permissions.</p></details>
      </div>}
      {topic === "write" && <div className="space-y-4">
        <ol className="list-decimal space-y-3 pl-5"><li>Open a Vault you can write to. A <strong>Collection</strong> groups related documents and other resources.</li><li>Choose the Collection and use its <strong>+</strong> menu to create a document.</li><li>Enter a title, write the content, then save. You can edit or rename the document later from its actions.</li></ol>
        <p className="text-foreground-muted">If writing controls are unavailable, ask the Vault owner for access. A local draft is recovery work saved in this browser; save the document to make your changes available in AKB.</p>
        <Button onClick={() => onNavigate("/vault")}>Open Vaults<ArrowRight className="h-4 w-4" aria-hidden /></Button>
        <details className="connection-inline-help"><summary>Share a document</summary><p>Open the document’s publishing controls if your role permits publishing. Review public access and expiration before sharing the link. Manage published resources in Publications.</p></details>
      </div>}
      {topic === "connect" && <div className="space-y-4">
        <p>Your AI tool can use AKB to find and work with knowledge your account can access. Reading and writing in this website is already available; connecting an AI tool is optional.</p>
        <ol className="list-decimal space-y-3 pl-5"><li>Choose the AI application you use, such as Claude Code or Cursor.</li><li>Add AKB using the instructions for that tool, then approve sign-in or provide a personal connection key.</li><li>Ask the AI tool to list your accessible Vaults and check its actual AKB response.</li></ol>
        <p className="text-foreground-muted">Signing in to this website is separate from authorising an AI tool. The setup guide explains where to run each action.</p>
        <Button onClick={onConnect}>Set up an AI connection<ArrowRight className="h-4 w-4" aria-hidden /></Button>
      </div>}
      {topic === "trouble" && <div className="space-y-4">
        <p className="text-foreground-muted">Choose the symptom you see. Keep the current account and the selected AI application in mind when checking access.</p>
        <ConnectionTroubleshooting />
        <Button variant="outline" onClick={onConnect}>Open AI connections</Button>
      </div>}
    </div>
  </>;
}
