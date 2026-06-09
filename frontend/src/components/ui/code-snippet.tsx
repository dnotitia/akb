import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

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
  async function copy() {
    // clipboard is undefined on insecure (plain-HTTP) origins — and AKB ships
    // an `--insecure` snippet, so that deployment shape is real. Guard so a
    // copy never throws an uncaught TypeError with no user feedback.
    try {
      await navigator.clipboard?.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked — user can still select the text manually */
    }
  }
  return (
    <div className={cn("rounded-[var(--radius-md)] border border-border overflow-hidden", className)}>
      <div className="flex items-center justify-between gap-2 border-b border-border bg-surface-2 px-2 py-1">
        <span className="font-mono text-[9px] uppercase tracking-wider text-foreground-muted truncate">
          {filename || "snippet"}
        </span>
        <button
          onClick={copy}
          aria-label={copied ? "Snippet copied" : "Copy snippet"}
          className={cn(
            "inline-flex items-center gap-1 font-mono text-[9px] uppercase tracking-wider cursor-pointer shrink-0 transition-colors rounded-[var(--radius-sm)]",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-2",
            copied ? "text-success" : "text-foreground-muted hover:text-primary",
          )}
        >
          {copied ? <Check className="h-3 w-3" aria-hidden /> : <Copy className="h-3 w-3" aria-hidden />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="font-mono text-[10px] leading-snug p-2.5 overflow-x-auto bg-surface text-foreground whitespace-pre-wrap break-all">
        {code}
      </pre>
    </div>
  );
}
