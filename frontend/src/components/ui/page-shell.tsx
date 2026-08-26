import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

const CONTENT_WIDTH = {
  narrow: "max-w-3xl",
  compact: "max-w-4xl",
  wide: "max-w-[1280px]",
  full: "max-w-none",
} as const;

type PageShellWidth = keyof typeof CONTENT_WIDTH;

/**
 * Standard application-page composition.
 *
 * The masthead spans the app shell's full available width, while the working
 * content keeps a page-appropriate reading width. Keeping those responsibilities
 * separate prevents individual routes from narrowing their title together with
 * a form, table, or result list.
 */
export function PageShell({
  header,
  children,
  contentWidth = "wide",
  className,
  contentClassName,
}: {
  header: ReactNode;
  children: ReactNode;
  contentWidth?: PageShellWidth;
  className?: string;
  contentClassName?: string;
}) {
  return (
    <div className={cn("w-full fade-up", className)}>
      {header}
      <div className={cn("mx-auto w-full", CONTENT_WIDTH[contentWidth], contentClassName)}>
        {children}
      </div>
    </div>
  );
}
