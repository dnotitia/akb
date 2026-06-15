import type { HTMLAttributes, ReactNode } from "react";
import { AlertCircle, AlertTriangle, CheckCircle2, Info, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Design-system primitive: a tinted notice banner. Centralizes the
 * hand-rolled `border …/40 bg …/5` error/notice box that drifted across
 * ~16 sites. Backed by the semantic *-soft token quads so light/dark tint
 * correctly (opacity literals mis-tinted on the dark canvas).
 *
 * Role: destructive/warning announce assertively (`role=alert`); info/success
 * are polite (`role=status`). Pass an explicit `role`/`aria-live` to override.
 * Always pairs color with an icon + text (never color as the only signal).
 */
type AlertVariant = "destructive" | "warning" | "info" | "success";

const VARIANTS: Record<AlertVariant, { box: string; Icon: LucideIcon }> = {
  destructive: {
    box: "border-destructive/30 bg-destructive-soft text-destructive-soft-foreground",
    Icon: AlertCircle,
  },
  warning: {
    box: "border-warning/30 bg-warning-soft text-warning-soft-foreground",
    Icon: AlertTriangle,
  },
  info: {
    box: "border-info/30 bg-info-soft text-info-soft-foreground",
    Icon: Info,
  },
  success: {
    box: "border-success/30 bg-success-soft text-success-soft-foreground",
    Icon: CheckCircle2,
  },
};

export function Alert({
  variant = "destructive",
  title,
  icon = true,
  className,
  children,
  ...props
}: HTMLAttributes<HTMLDivElement> & {
  variant?: AlertVariant;
  title?: ReactNode;
  /** Set false to drop the leading icon (rare — color-only is discouraged). */
  icon?: boolean;
}) {
  const v = VARIANTS[variant];
  const Icon = v.Icon;
  const role = variant === "destructive" || variant === "warning" ? "alert" : "status";
  return (
    <div
      role={role}
      className={cn(
        "flex items-start gap-2 rounded-[var(--radius-md)] border px-3 py-2 text-sm",
        v.box,
        className,
      )}
      {...props}
    >
      {icon && <Icon className="h-4 w-4 shrink-0 mt-0.5" aria-hidden />}
      <div className="min-w-0 leading-relaxed">
        {title && <div className="font-semibold">{title}</div>}
        {children && <div className={cn(title && "mt-0.5")}>{children}</div>}
      </div>
    </div>
  );
}
