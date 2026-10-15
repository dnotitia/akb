import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceLocationProvider } from "@/contexts/resource-location-context";
import TablePage from "@/pages/table";

vi.mock("@/lib/api", () => {
  class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  }
  class TableRowConflictError extends Error {}
  return {
    ApiError,
    TableRowConflictError,
    createPublication: vi.fn(),
    createPublicationSnapshot: vi.fn(),
    deletePublication: vi.fn(),
    deleteVaultTable: vi.fn(),
    deleteVaultTableRow: vi.fn(),
    getVaultInfo: vi.fn(),
    getVaultTableRow: vi.fn(),
    insertVaultTableRow: vi.fn(),
    listPublications: vi.fn(),
    listVaultTableRows: vi.fn(),
    listVaultTables: vi.fn(),
    previewTablePublicationQuery: vi.fn(),
    updateVaultTableRow: vi.fn(),
  };
});

import { getVaultInfo, listVaultTableRows, listVaultTables } from "@/lib/api";

const vaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const listTablesMock = listVaultTables as unknown as ReturnType<typeof vi.fn>;
const listRowsMock = listVaultTableRows as unknown as ReturnType<typeof vi.fn>;

// Longer than the grid's 120-character display limit, so its cell is a button.
const notes =
  "Root cause: the gateway retried every failed request until the connection pool was exhausted, " +
  "so new sessions queued behind stale ones and the health check timed out before failover began.";
const CAPPED_NOTES = /^Root cause:.*\(show full value\)$/;
const COLUMNS = ["id", "title", "notes", "steps", "created_at"];
const reader = { role: "reader", is_archived: false, is_external_git: false };
const writer = { role: "writer", is_archived: false, is_external_git: false };
const KEY = "akb.tableColumnWidths.v1:ops/incidents";
const HINT = "Drag to resize · double-click to reset";

function row(id: string, title: string) {
  return {
    id,
    title,
    notes,
    steps: "Drain the gateway",
    created_at: "2026-08-31T04:45:00Z",
    updated_at: "2026-09-01T04:45:00Z",
  };
}

function renderTable(path = "/vault/ops/table/incidents") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <VaultRefreshProvider refetchVaults={vi.fn()} refetchTree={vi.fn()}>
          <CurrentUserProvider user={null} revision={0} checking={false}>
            <ResourceLocationProvider identity="test-account" revision={0} checking={false}>
              <Routes>
                <Route path="/vault/:name/table/:table" element={<TablePage />} />
              </Routes>
            </ResourceLocationProvider>
          </CurrentUserProvider>
        </VaultRefreshProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function gridReady() {
  await screen.findByText("API outage");
}

function separator(column: string) {
  return screen.getByRole("separator", { name: `Resize ${column} column` });
}

function headerCell(column: string) {
  return separator(column).closest("th") as HTMLTableCellElement;
}

/** The body cells of a column, one per row. */
function columnCells(column: string) {
  const index = headerCell(column).cellIndex;
  const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1) as HTMLTableRowElement[];
  return rows.map((bodyRow) => bodyRow.cells[index]);
}

const HANDLE = '[data-slot="table-column-resize-handle"]';

/** The resize handle on a body cell's right border. */
function bodyHandle(column: string, rowNumber = 1) {
  return columnCells(column)[rowNumber - 1].querySelector<HTMLElement>(HANDLE)!;
}

/** The header cell's share of the previous column's border. */
function headerGrip(column: string) {
  return headerCell(column).querySelector<HTMLElement>(HANDLE)!;
}

function guide() {
  return document.querySelector<HTMLElement>('[data-slot="table-column-resize-guide"]')!;
}

/** The column's header cell and every body cell carry exactly this width, or none. */
function expectColumnWidth(column: string, width: number | null) {
  const cells = [headerCell(column), ...columnCells(column)];
  expect(cells.length).toBeGreaterThan(1);
  const expected = width === null ? "" : `${width}px`;
  for (const cell of cells) {
    expect(cell.style.width).toBe(expected);
    expect(cell.style.minWidth).toBe(expected);
    expect(cell.style.maxWidth).toBe(expected);
  }
}

// jsdom has no layout. Header cells sit side by side, HEADER_WIDTH wide, moved
// left by `scrolled` as the grid scrolls; every other element measures 0.
const HEADER_WIDTH = 200;
let scrolled = 0;
const layoutFreeRect = HTMLElement.prototype.getBoundingClientRect;
const setPointerCaptureDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, "setPointerCapture");
const setPointerCapture = vi.fn();

function headerRect(cell: HTMLTableCellElement): DOMRect {
  const left = cell.cellIndex * HEADER_WIDTH - scrolled;
  return {
    x: left,
    y: 0,
    left,
    right: left + HEADER_WIDTH,
    top: 0,
    bottom: 44,
    width: HEADER_WIDTH,
    height: 44,
    toJSON: () => ({}),
  } as DOMRect;
}

/** The element whose text is `text` is wider than its box; every other element fits. */
function stubOverflowingText(text: string) {
  const sizes = { scrollWidth: 320, clientWidth: 120 };
  const originals = Object.keys(sizes).map(
    (name) => [name, Object.getOwnPropertyDescriptor(HTMLElement.prototype, name)] as const,
  );
  for (const [name, size] of Object.entries(sizes)) {
    Object.defineProperty(HTMLElement.prototype, name, {
      configurable: true,
      get(this: HTMLElement) {
        return this.textContent === text ? size : 0;
      },
    });
  }
  return () => {
    for (const [name, descriptor] of originals) {
      if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
      else Reflect.deleteProperty(HTMLElement.prototype, name);
    }
  };
}

/** The records region, given a visible width and a horizontal scroll position. */
function layOutRecords(clientWidth: number, scrollLeft = 0) {
  const region = screen.getByRole("region", { name: "Records for incidents" });
  Object.defineProperty(region, "clientWidth", { configurable: true, value: clientWidth });
  Object.defineProperty(region, "scrollLeft", { configurable: true, value: scrollLeft });
  scrolled = scrollLeft;
}

beforeEach(() => {
  localStorage.clear();
  scrolled = 0;
  HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
    return this instanceof HTMLTableCellElement && this.tagName === "TH"
      ? headerRect(this)
      : layoutFreeRect.call(this);
  };
  setPointerCapture.mockReset();
  Object.defineProperty(Element.prototype, "setPointerCapture", {
    configurable: true,
    writable: true,
    value: setPointerCapture,
  });
  vaultInfoMock.mockReset();
  listTablesMock.mockReset();
  listRowsMock.mockReset();
  vaultInfoMock.mockResolvedValue(reader);
  listTablesMock.mockResolvedValue({
    items: [{
      name: "incidents",
      collection: "operations",
      row_count: 2,
      columns: [
        { name: "title", type: "text", required: true },
        { name: "notes", type: "text" },
        { name: "steps", type: "text" },
      ],
    }],
  });
  listRowsMock.mockResolvedValue({
    kind: "table_query",
    columns: COLUMNS,
    items: [
      row("6ab163e8-6ea4-4d20-8765-bf912716384c", "API outage"),
      row("7bc274f9-7fb5-4e31-9876-c0a23827495d", "Disk pressure"),
    ],
    total: 2,
  });
});

afterEach(() => {
  HTMLElement.prototype.getBoundingClientRect = layoutFreeRect;
  if (setPointerCaptureDescriptor) {
    Object.defineProperty(Element.prototype, "setPointerCapture", setPointerCaptureDescriptor);
  } else {
    Reflect.deleteProperty(Element.prototype, "setPointerCapture");
  }
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
  document.body.style.removeProperty("-webkit-user-select");
});

describe("table column resizing", () => {
  it("puts one separator on each data column header and a handle on each data cell", async () => {
    vaultInfoMock.mockResolvedValue(writer);
    renderTable();
    await screen.findByRole("button", { name: "Actions for row 2" });

    const separators = within(screen.getByRole("table")).getAllByRole("separator");
    expect(separators.map((item) => item.getAttribute("aria-label"))).toEqual(
      COLUMNS.map((column) => `Resize ${column} column`),
    );
    for (const item of separators) {
      expect(item).toHaveAttribute("aria-orientation", "vertical");
      expect(item).toHaveAttribute("aria-valuemin", "80");
      expect(item).toHaveAttribute("aria-valuemax", "960");
      // An automatic column reports its rendered width.
      expect(item).toHaveAttribute("aria-valuenow", String(HEADER_WIDTH));
      expect(item).toHaveAttribute("tabindex", "0");
      expect(item).toHaveAttribute("title", HINT);
    }

    // None for the row number or the row actions, in the header or the body.
    const headers = screen.getAllByRole("columnheader");
    expect(headers).toHaveLength(COLUMNS.length + 2);
    for (const cell of [headers[0], headers[headers.length - 1]]) {
      expect(within(cell).queryByRole("separator")).not.toBeInTheDocument();
      expect(cell.querySelector(HANDLE)).toBeNull();
    }
    for (const rowNumber of [1, 2]) {
      const cells = within(screen.getAllByRole("row")[rowNumber]).getAllByRole("cell");
      expect(cells).toHaveLength(COLUMNS.length + 2);
      expect(cells[0].querySelector(HANDLE)).toBeNull();
      expect(cells[cells.length - 1].querySelector(HANDLE)).toBeNull();
      // Body handles are pointer-only: hidden from assistive technology and unfocusable.
      for (const cell of cells.slice(1, -1)) {
        const handles = cell.querySelectorAll(HANDLE);
        expect(handles).toHaveLength(1);
        expect(handles[0]).toHaveAttribute("aria-hidden", "true");
        expect(handles[0]).not.toHaveAttribute("tabindex");
        expect(handles[0]).not.toHaveAttribute("role");
        // After the cell's content box, which stays its first child.
        expect(cell.lastElementChild).toBe(handles[0]);
      }
    }
    // Every data header but the first holds the previous column's share of their border.
    expect(headerCell("id").querySelector(HANDLE)).toBeNull();
    for (const column of COLUMNS.slice(1)) {
      expect(headerGrip(column)).toHaveAttribute("aria-hidden", "true");
    }
    // Nothing is fixed until a person resizes.
    for (const column of COLUMNS) expectColumnWidth(column, null);
    expect(guide()).toHaveAttribute("aria-hidden", "true");
    expect(guide()).toHaveClass("pointer-events-none", "bg-primary");
  });

  it("resizes from the keyboard in 16px and 64px steps, within 80–960px", async () => {
    const user = userEvent.setup();
    renderTable();
    await gridReady();
    const titleSeparator = separator("title");
    titleSeparator.focus();

    await user.keyboard("{ArrowRight}");
    expectColumnWidth("title", 216);
    expect(titleSeparator).toHaveAttribute("aria-valuenow", "216");
    await user.keyboard("{Shift>}{ArrowRight}{/Shift}");
    expectColumnWidth("title", 280);
    await user.keyboard("{ArrowLeft}");
    expectColumnWidth("title", 264);
    await user.keyboard("{Shift>}{ArrowLeft}{/Shift}");
    expectColumnWidth("title", 200);

    await user.keyboard("{Shift>}{ArrowLeft}{ArrowLeft}{/Shift}");
    expectColumnWidth("title", 80);
    await user.keyboard("{ArrowLeft}");
    expectColumnWidth("title", 80);
    expect(titleSeparator).toHaveAttribute("aria-valuenow", "80");

    for (let step = 0; step < 15; step += 1) await user.keyboard("{Shift>}{ArrowRight}{/Shift}");
    expectColumnWidth("title", 960);
    expect(titleSeparator).toHaveAttribute("aria-valuenow", "960");
    // Only that column.
    for (const column of ["id", "notes", "steps", "created_at"]) expectColumnWidth(column, null);

    await user.keyboard("{Enter}");
    expectColumnWidth("title", null);
    expect(titleSeparator).toHaveAttribute("aria-valuenow", String(HEADER_WIDTH));
    expect(titleSeparator).toHaveFocus();
  });

  it("keeps the arrow keys from scrolling the grid, and leaves modified keys alone", async () => {
    renderTable();
    await gridReady();
    const notesSeparator = separator("notes");

    // fireEvent returns false when the default action was prevented.
    expect(fireEvent.keyDown(notesSeparator, { key: "ArrowRight" })).toBe(false);
    expectColumnWidth("notes", 216);
    expect(fireEvent.keyDown(notesSeparator, { key: "ArrowLeft", altKey: true })).toBe(true);
    expect(fireEvent.keyDown(notesSeparator, { key: "ArrowLeft", metaKey: true })).toBe(true);
    expect(fireEvent.keyDown(notesSeparator, { key: "ArrowUp" })).toBe(true);
    expectColumnWidth("notes", 216);
  });

  it("places each separator right after its column's sort button in the tab order", async () => {
    const user = userEvent.setup();
    renderTable();
    await gridReady();
    screen.getByRole("button", { name: /^Sort by title/ }).focus();

    await user.tab();
    expect(separator("title")).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: /^Sort by notes/ })).toHaveFocus();
  });

  it("applies a body handle drag once, on release, to the header and every cell", async () => {
    renderTable();
    await gridReady();
    const handle = bodyHandle("notes", 2);

    fireEvent.pointerDown(handle, { pointerId: 1, button: 0, clientX: 100 });
    expect(setPointerCapture).toHaveBeenCalledWith(1);
    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");
    // WebKit only honours the prefixed property.
    expect(document.body.style.getPropertyValue("-webkit-user-select")).toBe("none");

    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 140 });
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 180 });
    // Nothing applies while the pointer moves.
    expectColumnWidth("notes", null);
    expect(localStorage.getItem(KEY)).toBeNull();

    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 180 });
    expectColumnWidth("notes", HEADER_WIDTH + 80);
    expect(separator("notes")).toHaveAttribute("aria-valuenow", "280");
    expect(localStorage.getItem(KEY)).toBe('{"notes":280}');
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    expect(document.body.style.getPropertyValue("-webkit-user-select")).toBe("");
    for (const column of ["id", "title", "steps", "created_at"]) expectColumnWidth(column, null);
  });

  it("commits nothing for a cancelled drag or a press without movement", async () => {
    renderTable();
    await gridReady();
    const handle = bodyHandle("steps");

    fireEvent.pointerDown(handle, { pointerId: 2, clientX: 100 });
    fireEvent.pointerMove(handle, { pointerId: 2, clientX: 300 });
    fireEvent.pointerCancel(handle, { pointerId: 2, clientX: 300 });
    expectColumnWidth("steps", null);
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    // The cancelled drag is over: a later release does nothing.
    fireEvent.pointerUp(handle, { pointerId: 2, clientX: 300 });
    expectColumnWidth("steps", null);

    fireEvent.pointerDown(handle, { pointerId: 3, clientX: 100 });
    fireEvent.pointerUp(handle, { pointerId: 3, clientX: 100 });
    expectColumnWidth("steps", null);
    expect(localStorage.getItem(KEY)).toBeNull();

    // Only the primary button drags.
    fireEvent.pointerDown(handle, { pointerId: 4, button: 2, clientX: 100 });
    expect(document.body.style.cursor).toBe("");
    fireEvent.pointerUp(handle, { pointerId: 4, button: 2, clientX: 180 });
    expectColumnWidth("steps", null);
  });

  it("follows only the pointer that started the drag", async () => {
    renderTable();
    await gridReady();
    const handle = bodyHandle("steps");

    fireEvent.pointerDown(handle, { pointerId: 5, clientX: 100 });
    fireEvent.pointerUp(handle, { pointerId: 6, clientX: 400 });
    expectColumnWidth("steps", null);
    expect(document.body.style.cursor).toBe("col-resize");

    fireEvent.pointerUp(handle, { pointerId: 5, clientX: 120 });
    expectColumnWidth("steps", HEADER_WIDTH + 20);
  });

  it("resizes the column the header separator or the next header cell's share belongs to", async () => {
    renderTable();
    await gridReady();

    fireEvent.pointerDown(separator("title"), { pointerId: 1, clientX: 500 });
    fireEvent.pointerUp(separator("title"), { pointerId: 1, clientX: 450 });
    expectColumnWidth("title", HEADER_WIDTH - 50);

    // The notes header holds the 3px of title's border past the border itself.
    fireEvent.pointerDown(headerGrip("notes"), { pointerId: 2, clientX: 500 });
    fireEvent.pointerUp(headerGrip("notes"), { pointerId: 2, clientX: 900 });
    expectColumnWidth("title", HEADER_WIDTH + 400);
    expectColumnWidth("notes", null);
    expect(headerGrip("notes")).toHaveAttribute("title", HINT);
  });

  it("restores the automatic width on a double-click on any handle", async () => {
    const user = userEvent.setup();
    renderTable();
    await gridReady();
    const widenNotes = async () => {
      separator("notes").focus();
      await user.keyboard("{ArrowRight}");
      expectColumnWidth("notes", 216);
    };

    await widenNotes();
    await user.dblClick(bodyHandle("notes", 2));
    expectColumnWidth("notes", null);

    await widenNotes();
    await user.dblClick(separator("notes"));
    expectColumnWidth("notes", null);

    // The steps header's share of the notes border.
    await widenNotes();
    await user.dblClick(headerGrip("steps"));
    expectColumnWidth("notes", null);
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("restores saved widths when the page mounts again, for that table only", async () => {
    const user = userEvent.setup();
    const first = renderTable();
    await gridReady();
    separator("title").focus();
    await user.keyboard("{ArrowRight}");
    first.unmount();

    const second = renderTable();
    await gridReady();
    expectColumnWidth("title", 216);
    expect(separator("title")).toHaveAttribute("aria-valuenow", "216");
    second.unmount();

    renderTable("/vault/ops/table/archive");
    await gridReady();
    expectColumnWidth("title", null);
  });

  it("never expands a cell or sorts from a handle", async () => {
    const user = userEvent.setup();
    renderTable();
    await gridReady();
    const notesButton = screen.getAllByRole("button", { name: CAPPED_NOTES })[0];
    const sortTitle = screen.getByRole("button", { name: /^Sort by title/ });
    // Siblings, never inside the buttons they border.
    expect(notesButton).not.toContainElement(bodyHandle("notes"));
    expect(sortTitle).not.toContainElement(separator("title"));
    const requests = listRowsMock.mock.calls.length;

    // The notes cell's own border, and title's border, whose handle overlaps the notes cell.
    await user.click(bodyHandle("notes"));
    await user.click(bodyHandle("title"));
    fireEvent.pointerDown(bodyHandle("notes"), { pointerId: 1, clientX: 100 });
    fireEvent.pointerUp(bodyHandle("notes"), { pointerId: 1, clientX: 160 });
    expect(screen.queryByRole("region", { name: "notes, row 1" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: CAPPED_NOTES })[0]).toHaveAttribute("aria-expanded", "false");

    await user.click(separator("title"));
    await user.click(headerGrip("notes"));
    fireEvent.pointerDown(separator("title"), { pointerId: 2, clientX: 100 });
    fireEvent.pointerUp(separator("title"), { pointerId: 2, clientX: 160 });
    expect(headerCell("title")).toHaveAttribute("aria-sort", "none");
    expect(listRowsMock).toHaveBeenCalledTimes(requests);
  });

  it("lets a reader resize: a view preference, not a write", async () => {
    renderTable();
    await gridReady();
    expect(screen.getByText("Read only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions for row 1" })).not.toBeInTheDocument();

    fireEvent.pointerDown(bodyHandle("created_at"), { pointerId: 1, clientX: 100 });
    fireEvent.pointerUp(bodyHandle("created_at"), { pointerId: 1, clientX: 140 });
    expectColumnWidth("created_at", HEADER_WIDTH + 40);
  });

  it("keeps the last column's full 8px handles inside the table when no row actions follow", async () => {
    renderTable();
    await gridReady();
    // Straddling the border would widen the grid's scroll area past the table.
    for (const handle of [separator("created_at"), bodyHandle("created_at", 1), bodyHandle("created_at", 2)]) {
      expect(handle).toHaveClass("right-0", "w-2");
      expect(handle).not.toHaveClass("-right-px", "-right-1", "w-[5px]");
    }
    // Elsewhere the separator stops at the border: the next header cell holds the rest.
    expect(separator("steps")).toHaveClass("-right-px", "w-[5px]");
    expect(bodyHandle("steps")).toHaveClass("-right-1", "w-2");
  });

  it("straddles the last column's border when the row actions follow it", async () => {
    vaultInfoMock.mockResolvedValue(writer);
    renderTable();
    await screen.findByRole("button", { name: "Actions for row 1" });
    expect(separator("created_at")).toHaveClass("-right-px", "w-[5px]");
    expect(bodyHandle("created_at")).toHaveClass("-right-1", "w-2");
  });

  it("ends a drag without applying it when the handle loses the pointer capture", async () => {
    renderTable();
    await gridReady();
    layOutRecords(2000);
    const handle = bodyHandle("steps");

    fireEvent.pointerDown(handle, { pointerId: 7, clientX: 100 });
    fireEvent.pointerMove(handle, { pointerId: 7, clientX: 300 });
    expect(guide().style.display).toBe("block");
    // Another pointer's capture is not this drag's.
    fireEvent.lostPointerCapture(handle, { pointerId: 8 });
    expect(document.body.style.cursor).toBe("col-resize");

    // A handle removed mid-drag loses the capture at the document.
    fireEvent.lostPointerCapture(document, { pointerId: 7 });
    expectColumnWidth("steps", null);
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    expect(guide().style.display).toBe("");
    // The drag is over: a later release applies nothing.
    fireEvent.pointerUp(window, { pointerId: 7, clientX: 300 });
    expectColumnWidth("steps", null);
    expect(localStorage.getItem(KEY)).toBeNull();

    // Lost at the handle itself, before any release.
    fireEvent.pointerDown(handle, { pointerId: 9, clientX: 100 });
    fireEvent.pointerMove(handle, { pointerId: 9, clientX: 180 });
    fireEvent.lostPointerCapture(handle, { pointerId: 9 });
    expect(document.body.style.cursor).toBe("");
    fireEvent.pointerUp(handle, { pointerId: 9, clientX: 180 });
    expectColumnWidth("steps", null);

    // A release still applies: it comes before the capture is lost.
    fireEvent.pointerDown(handle, { pointerId: 10, clientX: 100 });
    fireEvent.pointerUp(handle, { pointerId: 10, clientX: 140 });
    fireEvent.lostPointerCapture(handle, { pointerId: 10 });
    expectColumnWidth("steps", HEADER_WIDTH + 40);
    expect(localStorage.getItem(KEY)).toBe('{"steps":240}');
  });

  it("names each data column header after its column alone", async () => {
    vaultInfoMock.mockResolvedValue(writer);
    renderTable();
    await screen.findByRole("button", { name: "Actions for row 1" });

    for (const column of COLUMNS) {
      const header = screen.getByRole("columnheader", { name: column });
      expect(header).toBe(headerCell(column));
      // The sort state and the controls inside keep their own names.
      const sorted = column === "created_at";
      expect(header).toHaveAttribute("aria-sort", sorted ? "descending" : "none");
      expect(within(header).getByRole("button", {
        name: `Sort by ${column}, ${sorted ? "desc" : "not currently sorted"}`,
      })).toBeInTheDocument();
      expect(within(header).getByRole("separator", { name: `Resize ${column} column` })).toBeInTheDocument();
    }
    expect(screen.getByRole("columnheader", { name: "#" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Row actions" })).toBeInTheDocument();
  });

  it("shows a column name cut by a narrow column in full on hover", async () => {
    const restore = stubOverflowingText("created_at");
    try {
      const user = userEvent.setup();
      renderTable();
      await gridReady();

      // A name that fits shows nothing, even once its hover delay has passed.
      const fits = within(headerCell("title")).getByText("title");
      await user.hover(fits);
      await waitFor(() => expect(fits.getAttribute("data-state")).toMatch(/open$/));
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();

      const cut = within(headerCell("created_at")).getByText("created_at");
      await user.hover(cut);
      const tooltip = await screen.findByRole("tooltip");
      expect(tooltip).toHaveTextContent("created_at");
      // Portaled: never inside the sort button, whose name stays its own.
      expect(headerCell("created_at")).not.toContainElement(tooltip);
      expect(within(headerCell("created_at")).getByRole("button", { name: "Sort by created_at, desc" }))
        .toBeInTheDocument();
      // The type line is unchanged.
      expect(within(headerCell("created_at")).getByText("timestamp")).toHaveClass("truncate");
    } finally {
      restore();
    }
  });

  it("shows one guide on the column's border on hover and while dragging", async () => {
    const user = userEvent.setup();
    renderTable();
    await gridReady();
    layOutRecords(2000);
    const title = guide();
    // The title header spans 400–600px; the guide ends on its border.
    const onTitleBorder = `${3 * HEADER_WIDTH - 2}px`;

    for (const handle of [bodyHandle("title", 2), separator("title"), headerGrip("notes")]) {
      await user.hover(handle);
      expect(title.style.display).toBe("block");
      expect(title.style.left).toBe(onTitleBorder);
      await user.unhover(handle);
      expect(title.style.display).toBe("");
    }

    const handle = bodyHandle("title");
    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 100 });
    expect(title.style.display).toBe("block");
    expect(title.style.left).toBe(onTitleBorder);
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 180 });
    expect(title.style.left).toBe(`${3 * HEADER_WIDTH + 80 - 2}px`);
    // Clamped to the narrowest column.
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: -500 });
    expect(title.style.left).toBe(`${2 * HEADER_WIDTH + 80 - 2}px`);
    // Leaving the handle mid-drag keeps the guide.
    fireEvent.pointerOut(handle);
    expect(title.style.display).toBe("block");
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: -500 });
    expect(title.style.display).toBe("");
    expectColumnWidth("title", 80);
  });

  it("hides the guide while the border is under the frozen row number column", async () => {
    vaultInfoMock.mockResolvedValue(writer);
    const user = userEvent.setup();
    renderTable();
    await screen.findByRole("button", { name: "Actions for row 1" });
    // Scrolled 500px with 1000px in view: title's border (600px) sits under the
    // row number column (500–700px); the row actions cover 1300–1500px.
    layOutRecords(1000, 500);

    await user.hover(bodyHandle("title"));
    expect(guide().style.display).toBe("");
    await user.hover(bodyHandle("notes"));
    expect(guide().style.display).toBe("block");
    expect(guide().style.left).toBe(`${4 * HEADER_WIDTH - 2}px`);

    // Dragged under the row actions column, which stays at the right edge.
    const handle = bodyHandle("notes");
    fireEvent.pointerDown(handle, { pointerId: 1, clientX: 100 });
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 700 });
    expect(guide().style.display).toBe("");
    fireEvent.pointerMove(handle, { pointerId: 1, clientX: 140 });
    expect(guide().style.display).toBe("block");
    fireEvent.pointerUp(handle, { pointerId: 1, clientX: 140 });
    expectColumnWidth("notes", HEADER_WIDTH + 40);
  });

  it("keeps a saved width when a cell expands and when text wraps", async () => {
    const user = userEvent.setup();
    renderTable();
    await gridReady();
    separator("notes").focus();
    await user.keyboard("{Shift>}{ArrowRight}{/Shift}");
    expectColumnWidth("notes", 264);

    await user.click(screen.getAllByRole("button", { name: CAPPED_NOTES })[0]);
    expect(screen.getByRole("region", { name: "notes, row 1" })).toBeInTheDocument();
    expectColumnWidth("notes", 264);
    // The expanded cell keeps its handle.
    expect(bodyHandle("notes")).toHaveAttribute("aria-hidden", "true");

    await user.click(screen.getByRole("button", { name: "Wrap text" }));
    expectColumnWidth("notes", 264);
    for (const cell of columnCells("notes")) expect(cell).toHaveAttribute("data-wrap", "true");
  });

  it("restores the body cursor when the page unmounts mid-drag", async () => {
    const view = renderTable();
    await gridReady();
    fireEvent.pointerDown(bodyHandle("title"), { pointerId: 1, clientX: 100 });
    expect(document.body.style.cursor).toBe("col-resize");

    view.unmount();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    // The drag ended with the page: nothing is listening any more.
    fireEvent.pointerUp(window, { pointerId: 1, clientX: 300 });
    expect(localStorage.getItem(KEY)).toBeNull();
  });
});
