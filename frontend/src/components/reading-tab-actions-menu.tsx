import { useRef, type ReactNode } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as ContextMenu from "@radix-ui/react-context-menu";
import { ArrowLeft, ArrowRight, Columns2, ListX, RotateCcw, X } from "lucide-react";
import { useReadingWorkspace } from "@/contexts/reading-workspace-context";
import { readingDocumentKey, type ReadingDocument } from "@/lib/reading-workspace";
import { readingTabCloseKeys, type ReadingTabCloseScope } from "@/lib/reading-tab-actions";
import { isModalOpen } from "@/lib/modal-visibility";

interface Props { doc: ReadingDocument; active: string; children: ReactNode; global?: boolean; onAction?: (dismiss: boolean) => void }

/** Release menu/popover focus before a navigation guard can open its dialog. */
function useMenuAction(onAction?: (dismiss: boolean) => void) {
  const trigger = useRef<HTMLElement | null>(null);
  const pending = useRef<{ run: () => void; dismiss: boolean } | null>(null);
  const select = (action: () => void, dismiss = true) => {
    if (!dismiss) { onAction?.(false); action(); return; }
    pending.current = { run: action, dismiss };
  };
  const close = (event: Event, context = false) => {
    const action = pending.current; pending.current = null;
    if (!action && !context) return;
    event.preventDefault();
    const focus = () => {
      if (isModalOpen()) return;
      const target = trigger.current?.matches('button') ? trigger.current : trigger.current?.querySelector<HTMLElement>('[role="tab"]');
      (target?.isConnected ? target : document.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]') ?? document.getElementById("main"))?.focus({ preventScroll: true });
    };
    if (!action) { if (document.activeElement === document.body) focus(); return; }
    onAction?.(action.dismiss);
    requestAnimationFrame(() => {
      focus(); action.run();
      requestAnimationFrame(() => { if (document.activeElement === document.body) focus(); });
    });
  };
  return { trigger, select, close };
}

function MenuItems({ doc, active, global = false, kind, select }: Omit<Props, "children" | "onAction"> & { kind: "context" | "dropdown"; select: (action: () => void, dismiss?: boolean) => void }) {
  const workspace = useReadingWorkspace()!;
  const Menu = kind === "context" ? ContextMenu : DropdownMenu;
  const key = readingDocumentKey(doc);
  const index = workspace.tabs.findIndex(tab => readingDocumentKey(tab) === key);
  const close = (scope: ReadingTabCloseScope) => select(() => workspace.closeGroup(scope, key, active));
  const disabled = (scope: ReadingTabCloseScope) => readingTabCloseKeys(workspace.tabs, scope, key).length === 0 || workspace.restoring;
  const move = (delta: number) => select(() => {
    const target = workspace.tabs[index + delta];
    if (target) workspace.reorder(key, readingDocumentKey(target));
  }, false);
  return <>
    {!global && <Menu.Item onSelect={() => select(() => workspace.close(key, active))}><X size={15} aria-hidden />Close tab</Menu.Item>}
    <Menu.Item disabled={disabled("others")} onSelect={() => close("others")}><ListX size={15} aria-hidden />Close other tabs</Menu.Item>
    <Menu.Item disabled={disabled("left")} onSelect={() => close("left")}><ArrowLeft size={15} aria-hidden />Close tabs to the left</Menu.Item>
    <Menu.Item disabled={disabled("right")} onSelect={() => close("right")}><ArrowRight size={15} aria-hidden />Close tabs to the right</Menu.Item>
    <Menu.Item disabled={!workspace.tabs.length} onSelect={() => close("all")}><ListX size={15} aria-hidden />Close all tabs</Menu.Item>
    {!global && <>
      <Menu.Separator />
      <Menu.Item onSelect={() => select(() => workspace.openBeside(doc))} aria-label={`Read ${doc.title} beside`}><Columns2 size={15} aria-hidden />Read side by side</Menu.Item>
      <Menu.Item disabled={index <= 0} onSelect={() => move(-1)} aria-label={`Move ${doc.title} left`}><ArrowLeft size={15} aria-hidden />Move left</Menu.Item>
      <Menu.Item disabled={index < 0 || index === workspace.tabs.length - 1} onSelect={() => move(1)} aria-label={`Move ${doc.title} right`}><ArrowRight size={15} aria-hidden />Move right</Menu.Item>
    </>}
    {!!workspace.undoCount && <><Menu.Separator /><Menu.Item onSelect={() => select(workspace.undoClose)}><RotateCcw size={15} aria-hidden />Undo close {workspace.undoCount === 1 ? "tab" : `${workspace.undoCount} tabs`}</Menu.Item></>}
  </>;
}

export function ReadingTabActionsMenu({ children, onAction, ...props }: Props) {
  const action = useMenuAction(onAction);
  return <DropdownMenu.Root modal={false}>
    <DropdownMenu.Trigger asChild ref={element => { action.trigger.current = element; }}>{children}</DropdownMenu.Trigger>
    <DropdownMenu.Portal><DropdownMenu.Content aria-label={props.global ? "Tab list actions" : `Tab actions for ${props.doc.title}`} className="tab-manager-action-menu" side="top" align="end" sideOffset={4} collisionPadding={12} onCloseAutoFocus={event => action.close(event)}>
      <MenuItems {...props} kind="dropdown" select={action.select} />
    </DropdownMenu.Content></DropdownMenu.Portal>
  </DropdownMenu.Root>;
}

export function ReadingTabContextMenu({ children, onAction, ...props }: Props) {
  const action = useMenuAction(onAction);
  return <ContextMenu.Root modal={false}>
    <ContextMenu.Trigger asChild ref={element => { action.trigger.current = element; }} onKeyDown={event => {
      if (event.defaultPrevented || event.nativeEvent.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        event.preventDefault();
        const bounds = event.currentTarget.getBoundingClientRect();
        event.currentTarget.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true, button: 2,
          clientX: bounds.left + 8, clientY: bounds.bottom - 4 }));
      }
    }}>{children}</ContextMenu.Trigger>
    <ContextMenu.Portal><ContextMenu.Content aria-label={`Tab actions for ${props.doc.title}`} className="tab-manager-action-menu" collisionPadding={12} onCloseAutoFocus={event => action.close(event, true)}>
      <MenuItems {...props} kind="context" select={action.select} />
    </ContextMenu.Content></ContextMenu.Portal>
  </ContextMenu.Root>;
}
