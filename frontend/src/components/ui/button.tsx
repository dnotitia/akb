import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap font-medium tracking-tight",
    "rounded-[var(--radius-md)] transition-token",
    "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
    // Flat, legible disabled state (the :disabled specificity overrides each
    // variant's fill) — a faded teal/orange slab read as broken, not inactive.
    "disabled:pointer-events-none disabled:bg-surface-muted disabled:text-foreground-muted disabled:border-border disabled:shadow-none",
    "cursor-pointer",
  ].join(" "),
  {
    variants: {
      variant: {
        default:
          "bg-primary text-primary-foreground border border-primary shadow-sm hover:bg-primary/90",
        // Filled accent CTA — uses accent-STRONG so white text clears WCAG AA
        // (4.83:1). The bright --color-accent is reserved for non-text accent.
        accent:
          "bg-accent-strong text-accent-strong-foreground border border-accent-strong shadow-sm hover:bg-accent-strong/90",
        outline:
          "bg-surface text-foreground border border-border hover:bg-surface-muted hover:border-border-strong",
        secondary:
          "bg-surface-muted text-foreground border border-border hover:bg-surface",
        ghost:
          "bg-transparent text-foreground hover:bg-surface-muted",
        destructive:
          "bg-destructive text-destructive-foreground border border-destructive shadow-sm hover:bg-destructive/90",
        link:
          "bg-transparent text-link underline-offset-4 hover:underline hover:text-link-hover h-auto px-0",
      },
      size: {
        sm: "h-8 px-3 text-xs",
        md: "h-9 px-4 text-sm",
        default: "h-9 px-4 text-sm",
        lg: "h-11 px-5 text-base",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  /** Show a spinner, disable the button, and announce aria-busy. Ignored when
   *  `asChild` is set (Slot requires a single child). */
  loading?: boolean;
}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, loading = false, disabled, children, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    if (asChild) {
      return (
        <Comp ref={ref} className={cn(buttonVariants({ variant, size, className }))} {...props}>
          {children}
        </Comp>
      );
    }
    return (
      <Comp
        ref={ref}
        className={cn(buttonVariants({ variant, size, className }))}
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        {...props}
      >
        {loading && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
        {children}
      </Comp>
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
