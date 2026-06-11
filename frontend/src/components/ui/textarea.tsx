import { forwardRef, type TextareaHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        "flex min-h-[80px] w-full rounded-[var(--radius-md)] border border-border bg-surface px-3 py-2 text-sm text-foreground",
        "placeholder:text-foreground-muted",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
        "aria-[invalid=true]:border-destructive aria-[invalid=true]:focus-visible:ring-destructive",
        "disabled:opacity-50 disabled:cursor-not-allowed",
        // Read-only keeps full text contrast (unlike disabled) but signals
        // "not editable" via a muted fill + default cursor.
        "read-only:bg-surface-muted read-only:cursor-default",
        "transition-colors duration-150",
        "resize-y",
        className,
      )}
      {...props}
    />
  ),
);
Textarea.displayName = "Textarea";

export { Textarea };
