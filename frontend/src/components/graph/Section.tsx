import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

interface Props {
  label: string;
  rightAction?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function Section({ label, rightAction, children, className }: Props) {
  return (
    <section className={cn("border-b border-border py-3", className)}>
      <div className="flex items-center justify-between mb-2">
        <span className="coord">§ {label}</span>
        {rightAction}
      </div>
      {children}
    </section>
  );
}
