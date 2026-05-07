import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] font-medium",
  {
    variants: {
      variant: {
        /* neutral */
        default: "border-border bg-foreground text-background",
        secondary: "border-border bg-surface text-foreground",
        outline: "border-border bg-transparent text-foreground-muted",
        destructive: "border-destructive bg-destructive text-destructive-foreground",
        success: "border-success bg-transparent text-success",
        info: "border-accent bg-transparent text-accent",
        spark: "border-accent bg-accent text-accent-foreground",

        /* role badges — filled for write-authority, outline for read-only */
        owner: "border-accent bg-accent text-accent-foreground",
        admin: "border-foreground bg-foreground text-background",
        writer: "border-foreground bg-transparent text-foreground",
        reader: "border-foreground-muted bg-surface-muted text-foreground-muted",

        /* doc status */
        active: "border-success bg-transparent text-success",
        draft: "border-foreground-muted bg-transparent text-foreground-muted",
        archived: "border-warning bg-transparent text-warning",
        superseded: "border-foreground-muted bg-transparent text-foreground-muted line-through",

        /* system status */
        pending: "border-warning bg-transparent text-warning",
        syncing: "border-accent bg-transparent text-accent",
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
