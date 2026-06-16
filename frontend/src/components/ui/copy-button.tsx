import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Design-system primitive: a compact icon button that copies `value` to the
 * clipboard and flips to a check for 2s. Centralizes the copy affordance for
 * identifiers (akb:// URIs, doc ids, collection names) that were previously
 * plain text with no way to copy.
 *
 * Clipboard is undefined on insecure (plain-HTTP) origins — AKB ships an
 * `--insecure` deployment shape — so the write is guarded and fails silently
 * (the user can still select the text manually).
 */
export function CopyButton({
  value,
  label = "Copy",
  className,
  size = 14,
}: {
  value: string;
  /** Accessible label / tooltip verb. Defaults to "Copy". */
  label?: string;
  className?: string;
  /** Icon px size. */
  size?: number;
}) {
  const [copied, setCopied] = useState(false);

  async function copy(e: React.MouseEvent) {
    // These often sit inside a link/row — don't navigate or bubble.
    e.stopPropagation();
    e.preventDefault();
    try {
      await navigator.clipboard?.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard blocked on insecure origin — manual select still works */
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={copied ? `${label}: copied` : label}
      className={cn(
        "inline-flex items-center justify-center shrink-0 cursor-pointer transition-colors rounded-[var(--radius-sm)]",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        copied ? "text-success" : "text-foreground-muted hover:text-primary",
        className,
      )}
    >
      {copied ? (
        <Check style={{ width: size, height: size }} aria-hidden />
      ) : (
        <Copy style={{ width: size, height: size }} aria-hidden />
      )}
    </button>
  );
}
