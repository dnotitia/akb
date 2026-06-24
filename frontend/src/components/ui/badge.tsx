import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  // whitespace-nowrap: a pill must never wrap its label — without it, a chip
  // like "indexing 1,234" breaks at the space when the row is tight and the
  // count drops onto a second line, ballooning the pill vertically.
  "inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] tracking-normal font-medium",
  {
    variants: {
      variant: {
        /* neutral */
        default: "border-border bg-surface-muted text-foreground-muted",
        secondary: "border-border bg-surface text-foreground",
        outline: "border-border bg-transparent text-foreground-muted",
        destructive: "border-destructive bg-destructive text-destructive-foreground",
        success: "border-success bg-transparent text-success",
        warning: "border-warning bg-transparent text-warning",
        // filled semantic variants (backed by the completed -foreground quads)
        // for chips that need a solid fill (PUBLISHED, degraded, indexed …).
        "success-solid": "border-success bg-success text-success-foreground",
        "warning-solid": "border-warning bg-warning text-warning-foreground",
        "info-solid": "border-info bg-info text-info-foreground",
        // accent badges use accent-STRONG: outline badges need it for the
        // text (10px → no large-text exemption); filled badges for white-on-bg.
        info: "border-accent-strong bg-transparent text-accent-strong",
        spark: "border-accent-strong bg-accent-strong text-accent-strong-foreground",
        // true informational/feature chip — teal-blue --color-info (AA as text),
        // NOT orange. Use for passive "this is configured/available" markers
        // (e.g. the vault Guide chip) so they don't spend the one-marquee-orange
        // budget. (The misnamed `info` above is an accent-strong ORANGE highlight.)
        "info-outline": "border-info bg-transparent text-info",

        /* role badges — filled for write-authority, outline for read-only */
        owner: "border-primary bg-surface-selected text-surface-selected-foreground",
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
