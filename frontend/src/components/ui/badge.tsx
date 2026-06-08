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
        info: "border-accent bg-transparent text-accent",
        spark: "border-accent bg-accent text-accent-foreground",

        /* role badges — filled for write-authority, outline for read-only */
        owner: "border-accent bg-accent text-accent-foreground",
        admin: "border-primary bg-primary text-primary-foreground",
        writer: "border-primary bg-transparent text-primary",
        reader: "border-border bg-surface-muted text-foreground-muted",

        /* doc status */
        active: "border-success bg-transparent text-success",
        draft: "border-foreground-muted bg-transparent text-foreground-muted",
        archived: "border-warning bg-transparent text-warning",

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
