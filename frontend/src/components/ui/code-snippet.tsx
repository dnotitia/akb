import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";
import { TooltipText } from "@/components/ui/tooltip-text";

/**
 * Design-system primitive: a copyable code block with a soft header bar.
 * Centralizes the "drop snippet" pattern (home CONNECT + settings setup),
 * replacing the old harsh dark-slab headers with a token-driven surface.
 */
export function CodeSnippet({
  code,
  filename,
  className,
}: {
  code: string;
  filename?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  async function copy() {
    // clipboard is undefined on insecure (plain-HTTP) origins — and AKB ships
    // an `--insecure` snippet, so that deployment shape is real. Guard so a
    // copy never throws an uncaught TypeError with no user feedback.
    try {
      setCopied(false);
      setCopyError(false);
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopyError(true);
    }
  }
  return (
    <div className={cn("rounded-[var(--radius-md)] border border-border overflow-hidden", className)}>
      <div className="flex items-center justify-between gap-2 border-b border-border bg-surface-2 px-2 py-1">
        <TooltipText tip={filename || "snippet"} className="font-mono text-[11px] text-foreground-muted truncate">
          {filename || "snippet"}
        </TooltipText>
        <button
          onClick={copy}
          aria-label={copied ? "Snippet copied" : "Copy snippet"}
          className={cn(
            "inline-flex min-h-9 items-center gap-1 px-2 text-xs font-medium cursor-pointer shrink-0 transition-colors rounded-[var(--radius-sm)]",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-2",
            copied ? "text-success" : "text-foreground-muted hover:text-primary",
          )}
        >
          {copied ? <Check className="h-3 w-3" aria-hidden /> : <Copy className="h-3 w-3" aria-hidden />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre tabIndex={0} aria-label={filename || "Configuration snippet"} className="font-mono text-xs leading-relaxed p-3 overflow-x-auto bg-surface text-foreground whitespace-pre-wrap break-words focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
        {code}
      </pre>
      {copyError && <p role="status" className="border-t border-border px-3 py-2 text-xs text-foreground-muted">Copy was blocked. Select the text above and copy it manually.</p>}
    </div>
  );
}
