import { forwardRef, useId, type KeyboardEvent } from "react";
import { ChevronUp } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { formatCellValue, type TableCellValueText } from "@/lib/table-cell-value";
import { cn } from "@/lib/utils";

export interface TableCellExpandedProps {
  value: unknown;
  columnName: string;
  /** 1-based row number as shown in the grid's `#` column. */
  rowNumber: number;
  /** Return the cell to its truncated text: Collapse, or Escape anywhere inside. */
  onCollapse: () => void;
}

/**
 * One table cell's complete value, shown inside the cell itself in place of
 * its truncated text: the row grows, nothing covers the grid, and the text
 * stays selectable. A value taller than about half the viewport scrolls
 * inside the cell. The ref is the value region, the focus target on expand.
 */
export const TableCellExpanded = forwardRef<HTMLDivElement, TableCellExpandedProps>(
  function TableCellExpanded({ value, columnName, rowNumber, onCollapse }, ref) {
    const formatted = formatCellValue(value);
    // Several cells can be open at once, each with the same two buttons: they
    // are described by their value region, named after its column and row.
    const valueId = useId();

    function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
      if (event.key !== "Escape") return;
      // This cell owns the key: other Escape handlers on the page (such as
      // the schema panel's) must not act on it as well.
      event.preventDefault();
      event.stopPropagation();
      onCollapse();
    }

    return (
      <div className="flex flex-col" onKeyDown={handleKeyDown}>
        <div
          ref={ref}
          id={valueId}
          role="region"
          aria-label={`${columnName}, row ${rowNumber}`}
          tabIndex={0}
          className="max-h-[50vh] select-text overflow-auto px-3 py-2 rail-scroll rail-scroll-auto focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <CellValue value={formatted} />
        </div>
        {/* A column narrowed below both buttons wraps them, and Collapse keeps
            only its chevron (its name stays for assistive technology), so the
            buttons never spill out of the cell. */}
        <div className="@container flex flex-wrap items-center justify-end gap-1 px-1.5 pb-1.5">
          {/* The "Empty string" label is not copied: an empty string copies as "". */}
          <CopyButton
            value={formatted.text}
            label="Copy value"
            aria-describedby={valueId}
            className="h-8 w-8 focus-visible:ring-offset-surface"
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-expanded={true}
            aria-describedby={valueId}
            onClick={onCollapse}
            className="gap-1.5 px-2 focus-visible:ring-offset-surface"
          >
            <ChevronUp className="h-4 w-4" aria-hidden />
            <span className="@max-[7.5rem]:sr-only">Collapse</span>
          </Button>
        </div>
      </div>
    );
  },
);

/**
 * The value by kind: objects and arrays as indented JSON, strings with their
 * line breaks, and labels for null and the empty string. Lines are 20px, the
 * grid's text rhythm, so the first line sits level with the row's other cells.
 */
function CellValue({ value: { text, kind } }: { value: TableCellValueText }) {
  if (kind === "null") {
    return <p className="text-sm italic text-foreground-muted">null</p>;
  }
  if (kind === "json") {
    return (
      <pre className="whitespace-pre-wrap font-mono text-xs leading-5 text-foreground [overflow-wrap:anywhere]">
        {text}
      </pre>
    );
  }
  if (text === "") {
    return <p className="text-sm italic text-foreground-muted">Empty string</p>;
  }
  return (
    <p
      className={cn(
        "whitespace-pre-wrap text-sm text-foreground [overflow-wrap:anywhere]",
        kind === "number" && "tabular-nums",
      )}
    >
      {text}
    </p>
  );
}
