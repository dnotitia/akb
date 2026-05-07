import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap font-medium tracking-tight",
    "transition-colors duration-150",
    "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
    "disabled:pointer-events-none disabled:opacity-50",
    "cursor-pointer",
  ].join(" "),
  {
    variants: {
      variant: {
        default:
          "bg-foreground text-background border border-foreground hover:bg-foreground/90",
        accent:
          "bg-accent text-accent-foreground border border-accent hover:bg-accent/90",
        outline:
          "bg-surface text-foreground border border-border hover:bg-surface-muted",
        secondary:
          "bg-surface-muted text-foreground border border-border hover:bg-surface",
        ghost:
          "bg-transparent text-foreground hover:bg-surface-muted",
        destructive:
          "bg-destructive text-destructive-foreground border border-destructive hover:bg-destructive/90",
        link:
          "bg-transparent text-accent underline-offset-4 hover:underline h-auto px-0",
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
}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        className={cn(buttonVariants({ variant, size, className }))}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
