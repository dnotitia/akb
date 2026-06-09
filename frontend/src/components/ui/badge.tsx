import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.08em] font-semibold",
  {
    variants: {
      variant: {
        /* neutral */
        default: "border-border bg-surface-muted text-foreground-muted",
        secondary: "border-border bg-surface text-foreground",
        outline: "border-border bg-transparent text-foreground-muted",
        destructive: "border-destructive bg-destructive text-destructive-foreground",
        success: "border-success bg-transparent text-success",
        // accent badges use accent-STRONG: outline badges need it for the
        // text (10px → no large-text exemption); filled badges for white-on-bg.
        info: "border-accent-strong bg-transparent text-accent-strong",
        spark: "border-accent-strong bg-accent-strong text-accent-strong-foreground",

        /* role badges — filled for write-authority, outline for read-only */
        owner: "border-accent-strong bg-accent-strong text-accent-strong-foreground",
        admin: "border-primary bg-primary text-primary-foreground",
        writer: "border-primary bg-transparent text-primary",
        reader: "border-border bg-surface-muted text-foreground-muted",

        /* doc status */
        active: "border-success bg-transparent text-success",
        draft: "border-foreground-muted bg-transparent text-foreground-muted",
        archived: "border-warning bg-transparent text-warning",

        /* system status */
        pending: "border-warning bg-transparent text-warning",
        syncing: "border-accent-strong bg-transparent text-accent-strong",
        error: "border-destructive bg-transparent text-destructive",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
