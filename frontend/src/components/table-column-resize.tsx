import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import {
  TABLE_COLUMN_MAX_WIDTH,
  TABLE_COLUMN_MIN_WIDTH,
  clampTableColumnWidth,
} from "@/hooks/use-table-column-widths";
import { cn } from "@/lib/utils";

/** Arrow keys change a column by this many pixels; with Shift, by the larger step. */
const KEY_STEP = 16;
const KEY_LARGE_STEP = 64;
/** The guide ends on the border it marks, so it never widens the scroll area. */
const GUIDE_WIDTH = 2;
const RESIZE_HINT = "Drag to resize · double-click to reset";

/**
 * A column's handles cover its right border and run the full height of their
 * cells, bottom border included, so they join into one unbroken line. The hit
 * area is 8px: 4px before the 1px border, the border, and 3px after it. At the
 * end of a row nothing follows and the cell has no right border, so all 8px
 * lie inside the cell rather than widen the scroll area. z-[1] lifts a handle
 * over the cell content it overlaps while keeping it below the sticky header
 * and columns (--z-raised): it never paints or takes the pointer over them.
 */
const HANDLE = "absolute top-0 -bottom-px z-[1] cursor-col-resize touch-none";

interface ColumnDrag {
  column: string;
  pointerId: number;
  /** The handle holding the pointer capture. */
  target: Element;
  /** The pointer at the press, in the scroll content's coordinates. */
  startX: number;
  /** The column's rendered width at the press. */
  startWidth: number;
  /** The column's right border at the press, in the scroll content's coordinates. */
  startEdge: number;
  /** The body's own cursor and text selection, restored when the drag ends. */
  cursor: string;
  userSelect: string;
  webkitUserSelect: string;
}

/**
 * Shared by every handle of one table. Hover and drag only move the guide,
 * directly on its element, so the grid never re-renders until a drag ends
 * with a new width.
 */
class ColumnResizeController {
  onResize: (column: string, width: number) => void = () => {};
  onReset: (column: string) => void = () => {};
  private readonly headerCells = new Map<string, HTMLTableCellElement>();
  private guide: HTMLElement | null = null;
  private drag: ColumnDrag | null = null;

  /** The header cell measures its column and anchors the guide. */
  registerHeaderCell(column: string, cell: HTMLTableCellElement): () => void {
    this.headerCells.set(column, cell);
    return () => {
      if (this.headerCells.get(column) === cell) this.headerCells.delete(column);
    };
  }

  registerGuide(guide: HTMLElement | null): () => void {
    this.guide = guide;
    return () => {
      if (this.guide === guide) this.guide = null;
    };
  }

  hover(column: string) {
    if (this.drag) return;
    const cell = this.headerCells.get(column);
    if (cell) this.placeGuide(column, this.toContentX(cell.getBoundingClientRect().right));
  }

  leave() {
    if (!this.drag) this.hideGuide();
  }

  begin(event: ReactPointerEvent<HTMLElement>, column: string) {
    const cell = this.headerCells.get(column);
    if (event.button !== 0 || this.drag || !cell) return;
    // The press belongs to the handle: it starts no text selection and does
    // not reach the cell, header or grid around it.
    event.preventDefault();
    event.stopPropagation();
    const target = event.currentTarget;
    try {
      target.setPointerCapture(event.pointerId);
    } catch {
      /* The window listeners below still follow the pointer inside the page. */
    }
    const rect = cell.getBoundingClientRect();
    const body = document.body.style;
    this.drag = {
      column,
      pointerId: event.pointerId,
      target,
      startX: this.toContentX(event.clientX),
      startWidth: rect.width,
      startEdge: this.toContentX(rect.right),
      cursor: body.cursor,
      userSelect: body.userSelect,
      webkitUserSelect: body.getPropertyValue("-webkit-user-select"),
    };
    body.cursor = "col-resize";
    body.userSelect = "none";
    // WebKit only honours the prefixed property, so lock selection there too.
    body.setProperty("-webkit-user-select", "none");
    // On the window, so a handle that unmounts mid-drag cannot strand the
    // drag, the body cursor or the selection lock. Losing the pointer capture
    // ends the drag without applying it, since a release outside the page
    // would no longer arrive; a removed handle loses it at the document,
    // which the window still hears.
    window.addEventListener("pointermove", this.handlePointerMove);
    window.addEventListener("pointerup", this.handlePointerUp);
    window.addEventListener("pointercancel", this.handlePointerCancel);
    window.addEventListener("lostpointercapture", this.handleLostPointerCapture);
    this.placeGuide(column, this.drag.startEdge);
  }

  /** Keyboard resizing: an automatic column starts from its rendered width. */
  resizeBy(column: string, delta: number, savedWidth: number | undefined) {
    const width = savedWidth ?? this.headerCells.get(column)?.getBoundingClientRect().width;
    if (width !== undefined) this.onResize(column, Math.round(width) + delta);
  }

  reset(column: string) {
    this.onReset(column);
  }

  /** Ends a drag in progress without applying it. */
  dispose() {
    this.end();
  }

  private readonly handlePointerMove = (event: PointerEvent) => {
    const drag = this.drag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    this.placeGuide(drag.column, drag.startEdge + this.widthAt(drag, event) - drag.startWidth);
  };

  private readonly handlePointerUp = (event: PointerEvent) => {
    const drag = this.drag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    const moved = this.toContentX(event.clientX) !== drag.startX;
    const width = this.widthAt(drag, event);
    this.end();
    if (moved) this.onResize(drag.column, width);
  };

  private readonly handlePointerCancel = (event: PointerEvent) => {
    if (this.drag && event.pointerId === this.drag.pointerId) this.end();
  };

  /**
   * Ends the drag without applying it. A release never gets here: pointerup
   * comes first, applies the width and stops listening.
   */
  private readonly handleLostPointerCapture = (event: PointerEvent) => {
    if (this.drag && event.pointerId === this.drag.pointerId) this.end();
  };

  /** The width the drag gives the column with the pointer at `event`. */
  private widthAt(drag: ColumnDrag, event: PointerEvent): number {
    const width = drag.startWidth + this.toContentX(event.clientX) - drag.startX;
    return Math.min(TABLE_COLUMN_MAX_WIDTH, Math.max(TABLE_COLUMN_MIN_WIDTH, width));
  }

  private end() {
    const drag = this.drag;
    if (!drag) return;
    this.drag = null;
    window.removeEventListener("pointermove", this.handlePointerMove);
    window.removeEventListener("pointerup", this.handlePointerUp);
    window.removeEventListener("pointercancel", this.handlePointerCancel);
    window.removeEventListener("lostpointercapture", this.handleLostPointerCapture);
    const body = document.body.style;
    body.cursor = drag.cursor;
    body.userSelect = drag.userSelect;
    if (drag.webkitUserSelect) body.setProperty("-webkit-user-select", drag.webkitUserSelect);
    else body.removeProperty("-webkit-user-select");
    if (drag.target.hasPointerCapture(drag.pointerId)) {
      drag.target.releasePointerCapture(drag.pointerId);
    }
    this.hideGuide();
  }

  /** A viewport x in the coordinates of the scroll content that holds the guide. */
  private toContentX(clientX: number): number {
    const container = this.guide?.parentElement;
    if (!container) return clientX;
    return clientX - container.getBoundingClientRect().left - container.clientLeft + container.scrollLeft;
  }

  /**
   * Shows the guide on a column's right border at `edge`, over the table's
   * full height. The guide sits above the header row so the whole line shows,
   * which would also put it over the frozen row-number and row-actions
   * columns: while the border is under one of them, or out of view, the guide
   * hides instead.
   */
  private placeGuide(column: string, edge: number) {
    const guide = this.guide;
    const container = guide?.parentElement;
    const cell = this.headerCells.get(column);
    const table = cell?.closest("table");
    if (!guide || !container || !cell || !table) return;
    const [start, end] = this.visibleRange(cell, container);
    // 1px of tolerance: cell rectangles are fractional, clientWidth is rounded.
    if (edge - GUIDE_WIDTH < start - 1 || edge > end + 1) {
      this.hideGuide();
      return;
    }
    guide.style.left = `${edge - GUIDE_WIDTH}px`;
    guide.style.top = `${table.offsetTop}px`;
    guide.style.height = `${table.offsetHeight}px`;
    guide.style.display = "block";
  }

  /**
   * The part of the scroll content in view and not under a frozen column: the
   * header row's cells before the first and after the last resizable column
   * (the row number and the row actions) stay in place while the grid scrolls.
   */
  private visibleRange(cell: HTMLTableCellElement, container: HTMLElement): [number, number] {
    let start = container.scrollLeft;
    let end = container.scrollLeft + container.clientWidth;
    const row = cell.parentElement;
    if (row instanceof HTMLTableRowElement && row.cells.length > 0) {
      const first = row.cells[0];
      const last = row.cells[row.cells.length - 1];
      if (!this.isHeaderCell(first)) start += first.getBoundingClientRect().width;
      if (!this.isHeaderCell(last)) end -= last.getBoundingClientRect().width;
    }
    return [start, end];
  }

  private isHeaderCell(cell: HTMLTableCellElement): boolean {
    for (const registered of this.headerCells.values()) if (registered === cell) return true;
    return false;
  }

  private hideGuide() {
    if (this.guide) this.guide.style.display = "";
  }
}

const ColumnResizeContext = createContext<ColumnResizeController | null>(null);

function useColumnResize(): ColumnResizeController {
  const controller = useContext(ColumnResizeContext);
  if (!controller) throw new Error("Table column resize controls must be inside <TableColumnResize>.");
  return controller;
}

/**
 * Lets a person resize a table's columns by dragging the vertical line between
 * them, in the header or anywhere in the body, as in a document editor. Hover
 * shows a guide over the whole line; a drag moves the guide and applies the
 * width once, on release. A double-click restores the automatic width. Header
 * separators also resize from the keyboard.
 *
 * Wrap the table's scroll container. Inside it: one <TableColumnResizeGuide>,
 * one <TableColumnResizeSeparator> per resizable header cell, and a
 * <TableColumnResizeHandle> per body cell of a resizable column.
 */
export function TableColumnResize({
  onResize,
  onReset,
  children,
}: {
  /** A new width for the column, unrounded and unclamped. */
  onResize: (column: string, width: number) => void;
  /** Restore the column's automatic width. */
  onReset: (column: string) => void;
  children: ReactNode;
}) {
  const [controller] = useState(() => new ColumnResizeController());
  useLayoutEffect(() => {
    controller.onResize = onResize;
    controller.onReset = onReset;
  }, [controller, onReset, onResize]);
  useEffect(() => () => controller.dispose(), [controller]);
  return <ColumnResizeContext.Provider value={controller}>{children}</ColumnResizeContext.Provider>;
}

/**
 * The line that marks a column's border while one of its handles is hovered
 * or dragged. Render it once, as a direct child of the table's scroll
 * container, which must be positioned (`relative`): the guide is placed in
 * that container's scroll content. It sits on the header cells' layer, later
 * in the document, so it shows across the sticky header too.
 */
export function TableColumnResizeGuide() {
  const controller = useColumnResize();
  const ref = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => controller.registerGuide(ref.current), [controller]);
  return (
    <div
      ref={ref}
      aria-hidden="true"
      data-slot="table-column-resize-guide"
      className="pointer-events-none absolute left-0 top-0 z-[var(--z-raised)] hidden w-[2px] bg-primary"
    />
  );
}

/**
 * A header cell's own resize control, on its right border: drag it, or focus
 * it and press ArrowLeft / ArrowRight (16px, with Shift 64px); Enter or a
 * double-click restores the automatic width. Render it as a direct child of
 * the column's `<th>`, after the cell's other controls: it measures that cell,
 * which also anchors the column for the body handles and the guide.
 */
export function TableColumnResizeSeparator({
  column,
  width,
  rowEnd = false,
}: {
  column: string;
  /** The column's chosen width, or undefined while it sizes automatically. */
  width: number | undefined;
  /** The cell ends its row: it has no right border, and all 8px stay inside it. */
  rowEnd?: boolean;
}) {
  const controller = useColumnResize();
  const ref = useRef<HTMLDivElement | null>(null);
  const [renderedWidth, setRenderedWidth] = useState<number | null>(null);

  useLayoutEffect(() => {
    const cell = ref.current?.parentElement;
    if (!(cell instanceof HTMLTableCellElement)) return;
    const unregister = controller.registerHeaderCell(column, cell);
    const measure = () => setRenderedWidth(Math.round(cell.getBoundingClientRect().width));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(cell);
    return () => {
      observer.disconnect();
      unregister();
    };
  }, [column, controller]);

  function handleKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      // Also keeps the scroll container from scrolling sideways.
      event.preventDefault();
      const step = event.shiftKey ? KEY_LARGE_STEP : KEY_STEP;
      controller.resizeBy(column, event.key === "ArrowRight" ? step : -step, width);
    } else if (event.key === "Enter") {
      event.preventDefault();
      controller.reset(column);
    }
  }

  return (
    <div
      ref={ref}
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${column} column`}
      aria-valuemin={TABLE_COLUMN_MIN_WIDTH}
      aria-valuemax={TABLE_COLUMN_MAX_WIDTH}
      aria-valuenow={width ?? clampTableColumnWidth(renderedWidth ?? TABLE_COLUMN_MIN_WIDTH)}
      tabIndex={0}
      title={RESIZE_HINT}
      onPointerDown={(event) => controller.begin(event, column)}
      onPointerEnter={() => controller.hover(column)}
      onPointerLeave={() => controller.leave()}
      onDoubleClick={() => controller.reset(column)}
      onKeyDown={handleKeyDown}
      className={cn(
        HANDLE,
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
        // The border and the 4px before it. The next header cell paints over
        // anything past the border, so it holds the rest of the line. At the
        // end of a row, the cell's last 8px.
        rowEnd ? "right-0 w-2" : "-right-px w-[5px]",
      )}
    />
  );
}

/**
 * A pointer-only stretch of a column's border; keyboard users resize from the
 * header separator. `edge="end"` covers a body cell's right border.
 * `edge="start"` is a header cell's share of the previous column's border:
 * the 3px past that border, which the previous header cell's separator cannot
 * reach because this cell paints over it.
 */
export function TableColumnResizeHandle({
  column,
  edge,
  rowEnd = false,
}: {
  /** The column this handle resizes. */
  column: string;
  edge: "start" | "end";
  /** The cell ends its row: it has no right border, and all 8px stay inside it. */
  rowEnd?: boolean;
}) {
  const controller = useColumnResize();
  return (
    <div
      aria-hidden="true"
      data-slot="table-column-resize-handle"
      title={edge === "start" ? RESIZE_HINT : undefined}
      onPointerDown={(event) => controller.begin(event, column)}
      onPointerEnter={() => controller.hover(column)}
      onPointerLeave={() => controller.leave()}
      onDoubleClick={() => controller.reset(column)}
      className={cn(
        HANDLE,
        edge === "start" ? "left-0 w-[3px]" : rowEnd ? "right-0 w-2" : "-right-1 w-2",
      )}
    />
  );
}
