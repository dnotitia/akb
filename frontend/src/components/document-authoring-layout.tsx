import { createElement, type HTMLAttributes, type ReactNode, type Ref } from "react";
import { cn } from "@/lib/utils";

export interface DocumentAuthoringLayoutProps {
  children: ReactNode;
  details: ReactNode;
  writingAs?: "main" | "div";
  writingProps?: HTMLAttributes<HTMLElement> & { "data-testid"?: string };
  writingRef?: Ref<HTMLElement>;
  detailsId?: string;
  detailsHeadingId?: string;
}

/** Shared create/edit canvas; narrow workspaces retain one continuous scroll. */
export function DocumentAuthoringLayout({
  children,
  details,
  writingAs = "div",
  writingProps,
  writingRef,
  detailsId,
  detailsHeadingId,
}: DocumentAuthoringLayoutProps) {
  return (
    <div className="document-authoring-layout @container/authoring flex min-h-0 min-w-0 flex-1 flex-col bg-surface">
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain rail-scroll @[52rem]/authoring:overflow-hidden">
        <div className="grid min-h-full grid-cols-1 @[52rem]/authoring:h-full @[52rem]/authoring:min-h-0 @[52rem]/authoring:grid-cols-[minmax(0,1fr)_18rem] @[72rem]/authoring:grid-cols-[minmax(0,1fr)_20rem]">
          {createElement(writingAs, {
            ...writingProps,
            ref: writingRef,
            className: cn(
              "document-authoring-writing min-h-0 min-w-0 bg-surface @[52rem]/authoring:overflow-y-auto @[52rem]/authoring:overscroll-contain @[52rem]/authoring:rail-scroll",
              writingProps?.className,
            ),
          }, children)}
          <aside
            id={detailsId}
            aria-labelledby={detailsHeadingId}
            className="document-authoring-details min-h-0 min-w-0 border-t border-border bg-surface @[52rem]/authoring:overflow-y-auto @[52rem]/authoring:overscroll-contain @[52rem]/authoring:border-l @[52rem]/authoring:border-t-0 @[52rem]/authoring:rail-scroll"
          >
            {details}
          </aside>
        </div>
      </div>
    </div>
  );
}
