import { useEffect, useRef, useState } from "react";
import { Check, Link2 } from "lucide-react";
import { Portal as TooltipPortal } from "@radix-ui/react-tooltip";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export function VaultAddressAction({ name }: { name: string }) {
  return <AddressAction key={name} value={`akb://${name}`} />;
}

function AddressAction({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const [manual, setManual] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 2000);
    } catch { setManual(true); }
  }
  return <>
    <TooltipProvider delayDuration={250}><Tooltip>
      <TooltipTrigger asChild>
        <Button ref={triggerRef} type="button" variant="ghost" size="icon" aria-label={`Copy ${value}`}
          onClick={() => void copy()} className="h-11 w-11 text-foreground-muted sm:h-8 sm:w-8 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11">
          {copied ? <Check className="h-4 w-4 text-success" aria-hidden /> : <Link2 className="h-4 w-4" aria-hidden />}
        </Button>
      </TooltipTrigger>
      <TooltipPortal><TooltipContent side="bottom" className="max-w-[min(28rem,90vw)] break-words font-normal tracking-normal">
        <span className="block">{value}</span><span className="block text-foreground-muted">{copied ? "Address copied" : "Copy vault address"}</span>
      </TooltipContent></TooltipPortal>
    </Tooltip></TooltipProvider>
    <span role="status" className="sr-only">{copied ? "Vault address copied" : ""}</span>
    <Dialog open={manual} onOpenChange={setManual}>
      <DialogContent onCloseAutoFocus={event => { event.preventDefault(); triggerRef.current?.focus(); }}>
        <DialogTitle>Copy vault address</DialogTitle>
        <DialogDescription>Clipboard access is unavailable. Select and copy the address below.</DialogDescription>
        <input readOnly value={value} aria-label="Vault address" onFocus={event => event.currentTarget.select()}
          className="h-11 w-full rounded-[var(--radius-sm)] border border-border bg-surface px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
      </DialogContent>
    </Dialog>
  </>;
}
