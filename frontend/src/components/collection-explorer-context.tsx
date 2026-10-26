import { useId, useRef, type RefObject } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { ArrowLeft, ChevronDown } from "lucide-react";
import type { TreeNode } from "@/hooks/use-vault-tree";
import { Button } from "@/components/ui/button";

/** This changes the explorer's root, never the document currently being read. */
export function CollectionExplorerContext({ path, trail, onNavigate, backRef }: {
  path: string;
  trail: TreeNode[];
  onNavigate: (path: string) => void;
  backRef: RefObject<HTMLButtonElement | null>;
}) {
  const parentPath = path.split("/").slice(0, -1).join("/");
  const current = trail.find(node => node.path === path);
  const ancestors = trail.filter(node => node.path !== path);
  const parent = ancestors.find(node => node.path === parentPath);
  const backLabel = parentPath ? `Back to collection ${parentPath}` : "Back to all collections";
  const pendingDestination = useRef<string | null>(null);
  const descriptionId = useId();
  return <nav aria-label="Collection exploration" data-slot="collection-exploration" className="flex min-w-0 shrink-0 items-center gap-1 px-2 pb-2">
    <Button ref={backRef} type="button" variant="ghost" size="icon" aria-label={backLabel}
      title={parentPath ? `Back to ${parent?.name || "parent collection"}` : "Back to all collections"}
      className="h-9 w-9 shrink-0 text-link max-sm:h-11 max-sm:w-11 pointer-coarse:h-11 pointer-coarse:w-11"
      onClick={() => onNavigate(parentPath)}><ArrowLeft className="h-4 w-4" aria-hidden /></Button>
    <DropdownMenu.Root modal={false} onOpenChange={open => { if (open) pendingDestination.current = null; }}>
      <DropdownMenu.Trigger asChild>
        <Button type="button" variant="ghost" aria-label="Collection exploration path" aria-describedby={descriptionId} title={path}
          className="h-auto min-h-9 min-w-0 flex-1 justify-start gap-2 px-2 py-1 text-ui max-sm:min-h-11 pointer-coarse:min-h-11">
          <span className="min-w-0 flex-1 truncate text-left font-medium">{current?.name || "Collection unavailable"}</span>
          <span id={descriptionId} className="sr-only">Current collection: {path}</span>
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal><DropdownMenu.Content align="start" sideOffset={4}
        onCloseAutoFocus={event => {
          if (pendingDestination.current === null) return;
          event.preventDefault();
          const destination = pendingDestination.current;
          pendingDestination.current = null;
          onNavigate(destination);
        }}
        className="z-[var(--z-popover)] max-h-72 min-w-48 max-w-[min(90vw,20rem)] overflow-y-auto rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md">
        <DropdownMenu.Label className="max-w-72 break-words px-2.5 py-2 text-xs leading-relaxed text-foreground-muted [overflow-wrap:anywhere]">{path}</DropdownMenu.Label>
        <DropdownMenu.Separator className="my-1 h-px bg-border" />
        <DropdownMenu.Item onSelect={() => { pendingDestination.current = ""; }} className={itemClass}>All collections</DropdownMenu.Item>
        {ancestors.map(node => <DropdownMenu.Item key={node.path} aria-label={`Explore collection ${node.path}`} title={node.path}
          onSelect={() => { pendingDestination.current = node.path; }} className={itemClass}><span className="min-w-0 break-words [overflow-wrap:anywhere]">{node.name}</span></DropdownMenu.Item>)}
      </DropdownMenu.Content></DropdownMenu.Portal>
    </DropdownMenu.Root>
  </nav>;
}

const itemClass = "flex min-h-9 cursor-pointer select-none items-center rounded-[var(--radius-sm)] px-2.5 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover max-sm:min-h-11 pointer-coarse:min-h-11";
