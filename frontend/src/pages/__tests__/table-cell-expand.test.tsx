import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceLocationProvider } from "@/contexts/resource-location-context";
import { formatCellValue } from "@/lib/table-cell-value";
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

const rowId = "6ab163e8-6ea4-4d20-8765-bf912716384c";
const secondRowId = "7bc274f9-7fb5-4e31-9876-c0a23827495d";
// Longer than the grid's 120-character display limit.
const notes =
  "Root cause: the gateway retried every failed request until the connection pool was exhausted, " +
  "so new sessions queued behind stale ones and the health check timed out before failover began.";
// Longer than the 600-character display limit of wrapped cells.
const longNotes = [notes, notes, notes, notes].join(" ");
const steps = "Drain the gateway\nRotate the pool\n\nConfirm recovery";
// Its one-line JSON is longer than the 120-character display limit.
const metadata = {
  service: "gateway",
  regions: ["ap-northeast-2", "us-east-1"],
  runbook: "https://example.com/runbooks/gateway-certificate-rotation",
};
const reader = { role: "reader", is_archived: false, is_external_git: false };
const writer = { role: "writer", is_archived: false, is_external_git: false };
const WRAP_TEXT_KEY = "akb.tableWrapText";
const CAPPED_NOTES = /^Root cause:.*\(show full value\)$/;

function firstRow(overrides: Record<string, unknown> = {}) {
  return {
    id: rowId,
    title: "API outage",
    notes,
    steps,
    metadata,
    owner: null,
    created_at: "2026-08-31T04:45:00Z",
    updated_at: "2026-09-01T04:45:00Z",
    ...overrides,
  };
}

function rowsResponse(overrides: Record<string, unknown> = {}, extraRows: Record<string, unknown>[] = []) {
  return {
    kind: "table_query",
    columns: ["id", "title", "notes", "steps", "metadata", "owner", "created_at"],
    items: [firstRow(overrides), ...extraRows],
    total: 1 + extraRows.length,
  };
}

function renderTable() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/vault/ops/table/incidents"]}>
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

/** The grid has rendered its first row. */
async function gridReady() {
  await screen.findByText("API outage");
}

/** The expanded value region of a cell. */
function valueRegion(columnName: string, rowNumber = 1) {
  return screen.getByRole("region", { name: `${columnName}, row ${rowNumber}` });
}

function queryValueRegion(columnName: string, rowNumber = 1) {
  return screen.queryByRole("region", { name: `${columnName}, row ${rowNumber}` });
}

/** The grid's data cells; the row number and row actions cells have no data-wrap. */
function dataCells(row?: HTMLElement) {
  return (row ? within(row) : screen)
    .getAllByRole("cell")
    .filter((cell) => cell.hasAttribute("data-wrap"));
}

/** A collapsed cell's content box: a button while part of the value is hidden, else a plain div. */
function cellBox(cell: HTMLElement) {
  return cell.firstElementChild as HTMLElement;
}

/** The data cell whose text starts with `text`. */
function cellWithText(text: string) {
  const cell = dataCells().find((candidate) => candidate.textContent?.startsWith(text));
  if (!cell) throw new Error(`No data cell starts with ${text}`);
  return cell;
}

/** Row `n` of the grid body (1-based). */
function bodyRow(n: number) {
  return screen.getAllByRole("row")[n];
}

/**
 * jsdom has no layout, so every element measures 0×0. Report the element
 * whose text is `text` with the given sizes; everything else keeps 0.
 */
function stubMeasuredText(text: string, sizes: Record<string, number>) {
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

/** Text wider than its box, as a single truncated line leaves it. */
function stubOverflowingText(text: string) {
  return stubMeasuredText(text, { scrollWidth: 320, clientWidth: 120 });
}

/** Text taller than its box, as a line clamp leaves it. */
function stubClampedText(text: string) {
  return stubMeasuredText(text, { scrollHeight: 120, clientHeight: 40 });
}

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  return writeText;
}

/** Every element that takes the focus while `action` runs, in order. */
async function focusedDuring(action: () => Promise<void>) {
  const focused: Element[] = [];
  const record = (event: FocusEvent) => focused.push(event.target as Element);
  document.addEventListener("focusin", record);
  try {
    await action();
  } finally {
    document.removeEventListener("focusin", record);
  }
  return focused;
}

/** A collapsed cell's button, named "… (show full value)". */
function isCellButton(element: Element) {
  return element.tagName === "BUTTON" && /\(show full value\)$/.test(element.textContent ?? "");
}

/** The two ways a person collapses an expanded cell, with the value region focused. */
const COLLAPSE_WITH: [string, (user: ReturnType<typeof userEvent.setup>) => Promise<void>][] = [
  ["Escape", (user) => user.keyboard("{Escape}")],
  ["the Collapse button", (user) => user.click(screen.getByRole("button", { name: "Collapse" }))],
];

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");

beforeEach(() => {
  localStorage.removeItem(WRAP_TEXT_KEY);
  vaultInfoMock.mockReset();
  listTablesMock.mockReset();
  listRowsMock.mockReset();
  vaultInfoMock.mockResolvedValue(reader);
  listTablesMock.mockResolvedValue({
    items: [{
      name: "incidents",
      collection: "operations",
      description: "Operational incidents",
      row_count: 1,
      columns: [
        { name: "title", type: "text", required: true },
        { name: "notes", type: "text" },
        { name: "steps", type: "text" },
        { name: "metadata", type: "jsonb" },
        { name: "owner", type: "text" },
      ],
    }],
  });
  listRowsMock.mockResolvedValue(rowsResponse());
});

afterEach(() => {
  if (clipboardDescriptor) Object.defineProperty(navigator, "clipboard", clipboardDescriptor);
  else Reflect.deleteProperty(navigator, "clipboard");
});

describe("table cell expansion", () => {
  it("expands a cell cut by the display limit in place, without a dialog", async () => {
    const user = userEvent.setup();
    renderTable();
    const cell = await screen.findByRole("button", { name: CAPPED_NOTES });
    expect(cell).toHaveTextContent(`${notes.slice(0, 120)}…`);
    expect(cell).toHaveAttribute("aria-expanded", "false");
    expect(cell).not.toHaveAttribute("aria-haspopup");
    const td = cell.closest("td")!;
    const row = cell.closest("tr")!;

    await user.click(cell);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(within(td).getByRole("button", { name: "Collapse" })).toHaveAttribute("aria-expanded", "true");
    expect(within(td).queryByRole("button", { name: CAPPED_NOTES })).not.toBeInTheDocument();
    const region = within(row).getByRole("region", { name: "notes, row 1" });
    expect(td).toContainElement(region);
    expect(region.textContent).toBe(notes);
    expect(td).toHaveClass("align-top");
  });

  it("shows an object as indented JSON and a string with its line breaks", async () => {
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByRole("button", { name: /^\{"service":"gateway"/ }));

    const json = valueRegion("metadata");
    expect(json.querySelector("pre")?.textContent).toBe(
      [
        "{",
        '  "service": "gateway",',
        '  "regions": [',
        '    "ap-northeast-2",',
        '    "us-east-1"',
        "  ],",
        '  "runbook": "https://example.com/runbooks/gateway-certificate-rotation"',
        "}",
      ].join("\n"),
    );
    expect(json.textContent).toBe(formatCellValue(metadata).text);

    await user.click(screen.getByRole("button", { name: /^Drain the gateway/ }));
    const text = valueRegion("steps").firstElementChild;
    expect(text?.textContent).toBe(steps);
    expect(text).toHaveClass("whitespace-pre-wrap");
  });

  it("keeps an expanded cell open across a refresh and shows its current value", async () => {
    const user = userEvent.setup();
    const writeText = stubClipboard();
    renderTable();
    await user.click(await screen.findByRole("button", { name: CAPPED_NOTES }));
    const region = valueRegion("notes");

    listRowsMock.mockResolvedValue(rowsResponse({ notes: null }));
    await user.click(screen.getByRole("button", { name: "Refresh records" }));
    const marker = await within(region).findByText("null");
    expect(marker).toHaveClass("italic", "text-foreground-muted");
    expect(valueRegion("notes")).toBe(region);

    listRowsMock.mockResolvedValue(rowsResponse({ notes: "" }));
    await user.click(screen.getByRole("button", { name: "Refresh records" }));
    expect(await within(region).findByText("Empty string")).toHaveClass("italic", "text-foreground-muted");

    // The label is not the value: an empty string copies as "".
    await user.click(within(region.closest("td")!).getByRole("button", { name: "Copy value" }));
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith("");
  });

  it("copies the full value, not the truncated text", async () => {
    const user = userEvent.setup();
    // After setup(): user-event installs its own clipboard stub there.
    const writeText = stubClipboard();
    renderTable();

    await user.click(await screen.findByRole("button", { name: CAPPED_NOTES }));
    const notesCell = valueRegion("notes").closest("td")!;
    await user.click(within(notesCell).getByRole("button", { name: "Copy value" }));
    expect(writeText).toHaveBeenLastCalledWith(notes);
    expect(await within(notesCell).findByRole("button", { name: "Copy value: copied" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^\{"service":"gateway"/ }));
    const metadataCell = valueRegion("metadata").closest("td")!;
    await user.click(within(metadataCell).getByRole("button", { name: "Copy value" }));
    expect(writeText).toHaveBeenLastCalledWith(JSON.stringify(metadata, null, 2));
  });

  it("lets a reader without row permissions expand a cell", async () => {
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByRole("button", { name: CAPPED_NOTES }));

    expect(screen.queryByRole("button", { name: "Actions for row 1" })).not.toBeInTheDocument();
    expect(valueRegion("notes")).toHaveTextContent(notes);
  });

  it("makes only partly hidden cells tab stops, and expands and collapses from the keyboard", async () => {
    const user = userEvent.setup();
    renderTable();
    const capped = await screen.findByRole("button", { name: CAPPED_NOTES });
    expect(capped).toHaveAttribute("tabindex", "0");

    // From the last column header's last control, its resize separator, Tab
    // skips the fully visible id and title cells.
    screen.getByRole("separator", { name: "Resize created_at column" }).focus();
    await user.tab();
    expect(capped).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(valueRegion("notes")).toHaveFocus();
    expect(valueRegion("notes")).toHaveAttribute("tabindex", "0");

    await user.keyboard("{Escape}");
    expect(queryValueRegion("notes")).not.toBeInTheDocument();
    const collapsed = screen.getByRole("button", { name: CAPPED_NOTES });
    expect(collapsed).toHaveAttribute("aria-expanded", "false");
    expect(collapsed).toHaveFocus();

    await user.keyboard(" ");
    expect(valueRegion("notes")).toHaveFocus();
  });

  it("collapses from the Collapse button and returns focus to the cell", async () => {
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByRole("button", { name: CAPPED_NOTES }));
    const td = valueRegion("notes").closest("td")!;

    await user.click(within(td).getByRole("button", { name: "Collapse" }));

    expect(queryValueRegion("notes")).not.toBeInTheDocument();
    expect(within(td).getByRole("button", { name: CAPPED_NOTES })).toHaveFocus();
  });

  it("moves focus to the records when the collapsed value no longer hides anything", async () => {
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByRole("button", { name: CAPPED_NOTES }));
    const region = valueRegion("notes");
    listRowsMock.mockResolvedValue(rowsResponse({ notes: "Pool exhausted" }));
    await user.click(screen.getByRole("button", { name: "Refresh records" }));
    await waitFor(() => expect(region).toHaveTextContent("Pool exhausted"));

    await user.click(within(region.closest("td")!).getByRole("button", { name: "Collapse" }));

    expect(within(cellWithText("Pool exhausted")).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Records for incidents" })).toHaveFocus();
  });

  it("keeps several cells open and top-aligns only the rows that hold one", async () => {
    vaultInfoMock.mockResolvedValue(writer);
    listRowsMock.mockResolvedValue(rowsResponse({}, [
      firstRow({ id: secondRowId, title: "Disk pressure", steps: "Resize the volume", metadata: { service: "db" } }),
    ]));
    const user = userEvent.setup();
    renderTable();
    await screen.findByRole("button", { name: "Actions for row 2" });

    await user.click(within(bodyRow(1)).getByRole("button", { name: CAPPED_NOTES }));
    await user.click(within(bodyRow(1)).getByRole("button", { name: /^Drain the gateway/ }));
    expect(valueRegion("notes")).toHaveTextContent(notes);
    expect(valueRegion("steps").textContent).toBe(steps);

    // Row 1 holds expanded cells: its other cells align to the top, as wrapped rows do.
    const [rowNumber] = within(bodyRow(1)).getAllByRole("cell");
    expect(rowNumber).toHaveClass("align-top", "leading-5");
    expect(screen.getByRole("button", { name: "Actions for row 1" }).closest("td"))
      .toHaveClass("align-top", "pt-0.5", "pb-1.5");
    const collapsed = dataCells(bodyRow(1)).filter((cell) => !within(cell).queryByRole("region"));
    expect(collapsed).toHaveLength(5);
    for (const cell of collapsed) {
      // The cell too: Firefox does not stretch the box to a taller row.
      expect(cell).toHaveClass("align-top");
      expect(cellBox(cell)).toHaveClass("items-start", "py-2");
      expect(cellBox(cell)).not.toHaveClass("items-center");
    }
    for (const columnName of ["notes", "steps"]) {
      expect(valueRegion(columnName).closest("td")).toHaveClass("align-top");
    }

    // Row 2 holds none and keeps its single-line, centered layout.
    const [secondRowNumber] = within(bodyRow(2)).getAllByRole("cell");
    expect(secondRowNumber).not.toHaveClass("align-top");
    expect(screen.getByRole("button", { name: "Actions for row 2" }).closest("td"))
      .toHaveClass("py-1");
    for (const cell of dataCells(bodyRow(2))) {
      expect(cell).not.toHaveClass("align-top");
      expect(cellBox(cell)).toHaveClass("items-center");
      expect(cellBox(cell)).not.toHaveClass("items-start");
    }

    // A third cell, in another row.
    await user.click(within(bodyRow(2)).getByRole("button", { name: CAPPED_NOTES }));
    expect(valueRegion("notes", 2)).toHaveTextContent(notes);
    expect(valueRegion("notes", 1)).toBeInTheDocument();
    expect(valueRegion("steps", 1)).toBeInTheDocument();
    expect(secondRowNumber).toHaveClass("align-top");
  });

  it("does not collapse when the expanded value is clicked or double-clicked", async () => {
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByRole("button", { name: CAPPED_NOTES }));
    const region = valueRegion("notes");

    await user.click(region);
    await user.dblClick(region.firstElementChild!);

    expect(valueRegion("notes")).toBe(region);
    expect(screen.queryByRole("button", { name: CAPPED_NOTES })).not.toBeInTheDocument();
  });

  it("collapses every expanded cell when the sort changes", async () => {
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByRole("button", { name: CAPPED_NOTES }));
    await user.click(screen.getByRole("button", { name: /^Drain the gateway/ }));
    expect(valueRegion("notes")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Sort by title/ }));

    expect(queryValueRegion("notes")).not.toBeInTheDocument();
    expect(queryValueRegion("steps")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: CAPPED_NOTES })).toHaveAttribute("aria-expanded", "false");
    await waitFor(() => expect(listRowsMock).toHaveBeenLastCalledWith(
      "ops",
      "incidents",
      expect.objectContaining({ order: "title.asc,id.asc" }),
    ));
  });

  it("collapses every expanded cell when the page changes", async () => {
    // More records than one page holds, so the next page exists.
    listRowsMock.mockResolvedValue({ ...rowsResponse(), total: 120 });
    const user = userEvent.setup();
    renderTable();
    await user.click(await screen.findByRole("button", { name: CAPPED_NOTES }));
    expect(valueRegion("notes")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next page" }));

    expect(queryValueRegion("notes")).not.toBeInTheDocument();
    await waitFor(() => expect(listRowsMock.mock.lastCall?.[2]?.offset).toBeGreaterThan(0));
    expect(await screen.findByRole("button", { name: CAPPED_NOTES })).toHaveAttribute("aria-expanded", "false");
  });

  it("hides the expanded cell's width-holding copy from assistive technology", async () => {
    const user = userEvent.setup();
    renderTable();
    const cell = await screen.findByRole("button", { name: CAPPED_NOTES });
    const td = cell.closest("td")!;
    await user.click(cell);

    // The collapsed text stays in the cell only to hold the column's width.
    const holder = within(td).getByText(`${notes.slice(0, 120)}…`);
    expect(holder).toHaveAttribute("aria-hidden", "true");
    expect(holder).toHaveClass("invisible");
    expect(valueRegion("notes")).not.toContainElement(holder);
    expect(valueRegion("notes").textContent).toBe(notes);
  });

  it("keeps the Escape that collapses a cell from closing the schema panel", async () => {
    const user = userEvent.setup();
    renderTable();
    await gridReady();
    await user.click(screen.getByRole("button", { name: "Schema" }));
    expect(screen.getByRole("complementary", { name: "Table schema" })).toBeInTheDocument();
    // Opening the panel moves focus to its close button on the next frame.
    await waitFor(() => expect(document.activeElement).toHaveAccessibleName("Close table schema"));

    await user.click(screen.getByRole("button", { name: CAPPED_NOTES }));
    expect(valueRegion("notes")).toHaveFocus();
    await user.keyboard("{Escape}");

    expect(queryValueRegion("notes")).not.toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Table schema" })).toBeInTheDocument();
  });

  it("leaves a fully visible value as plain, unfocusable text", async () => {
    const user = userEvent.setup();
    renderTable();
    await gridReady();
    const title = cellWithText("API outage");
    expect(within(title).queryByRole("button")).not.toBeInTheDocument();
    expect(title.querySelector("[tabindex]")).toBeNull();
    expect(cellBox(title).tagName).toBe("DIV");

    await user.click(within(title).getByText("API outage"));

    expect(screen.queryByRole("region", { name: /, row 1$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Collapse" })).not.toBeInTheDocument();
  });

  it("names an empty cell without making it a button", async () => {
    listRowsMock.mockResolvedValue({
      kind: "table_query",
      columns: ["id", "title"],
      items: [{ id: rowId, title: "", updated_at: "2026-09-01T04:45:00Z" }],
      total: 1,
    });
    renderTable();
    await screen.findByText(rowId);
    const [, empty] = dataCells();
    expect(empty).toHaveTextContent("Empty");
    expect(within(empty).getByText("Empty")).toHaveClass("sr-only");
    expect(within(empty).queryByRole("button")).not.toBeInTheDocument();
  });

  it("does not expand anything from the row number column", async () => {
    vaultInfoMock.mockResolvedValue(writer);
    const user = userEvent.setup();
    renderTable();
    await screen.findByRole("button", { name: "Actions for row 1" });
    const [rowNumber] = within(bodyRow(1)).getAllByRole("cell");
    expect(rowNumber).toHaveTextContent("1");
    expect(within(rowNumber).queryByRole("button")).not.toBeInTheDocument();

    await user.click(rowNumber);
    expect(screen.queryByRole("region", { name: /, row 1$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("makes a cell a button and a tab stop when its text overflows the column", async () => {
    const restore = stubOverflowingText("API outage");
    try {
      const user = userEvent.setup();
      renderTable();
      const title = await screen.findByRole("button", { name: "API outage (show full value)" });
      expect(title).toHaveAttribute("tabindex", "0");
      expect(within(cellWithText("6ab163e8")).queryByRole("button")).not.toBeInTheDocument();

      // The hover tooltip still appears, outside the button, so the button's
      // content and accessible name stay the cell's own text.
      await user.hover(screen.getByText("API outage"));
      const tooltip = await screen.findByRole("tooltip");
      expect(tooltip).toHaveTextContent("API outage");
      expect(title).not.toContainElement(tooltip);
      expect(title).toHaveAccessibleName("API outage (show full value)");

      // Expanding and collapsing remeasures the text; the cell settles as a
      // button again and takes the focus back.
      await user.click(title);
      expect(valueRegion("title")).toHaveFocus();
      await user.keyboard("{Escape}");
      const collapsed = await screen.findByRole("button", { name: "API outage (show full value)" });
      expect(collapsed).toHaveFocus();
    } finally {
      restore();
    }
  });

  // Nothing measures an expanded cell's text, so by the time it collapses the
  // last measurement can be out of date.
  it.each(COLLAPSE_WITH)(
    "after %s, measures the collapsed text before returning focus to the cell",
    async (_how, collapse) => {
      let restore = stubOverflowingText("API outage");
      try {
        const user = userEvent.setup();
        renderTable();
        const records = await screen.findByRole("region", { name: "Records for incidents" });
        const title = () => screen.getByRole("button", { name: "API outage (show full value)" });

        // Still cut by its column: focus returns to the cell's button.
        await user.click(await screen.findByRole("button", { name: "API outage (show full value)" }));
        expect(valueRegion("title")).toHaveFocus();
        await collapse(user);
        await waitFor(() => expect(title()).toHaveFocus());

        // The column was widened while the cell was open, so the title now
        // fits: focus goes to the records, never to a button the measurement
        // is about to replace, and is not left on <body>.
        await user.click(title());
        expect(valueRegion("title")).toHaveFocus();
        restore();
        restore = () => {};
        const focused = await focusedDuring(() => collapse(user));
        await waitFor(() => expect(records).toHaveFocus());
        expect(document.activeElement).not.toBe(document.body);
        expect(within(cellWithText("API outage")).queryByRole("button")).not.toBeInTheDocument();
        expect(focused.filter(isCellButton)).toEqual([]);
        expect(focused.at(-1)).toBe(records);
      } finally {
        restore();
      }
    },
  );

  it.each(COLLAPSE_WITH)(
    "after %s, focuses the button a refreshed value turns out to need",
    async (_how, collapse) => {
      // Short enough for the display limit, but wider than its column.
      const restore = stubOverflowingText("Pool exhausted");
      try {
        const user = userEvent.setup();
        renderTable();
        await user.click(await screen.findByRole("button", { name: CAPPED_NOTES }));
        const region = valueRegion("notes");
        listRowsMock.mockResolvedValue(rowsResponse({ notes: "Pool exhausted" }));
        await user.click(screen.getByRole("button", { name: "Refresh records" }));
        await waitFor(() => expect(region).toHaveTextContent("Pool exhausted"));
        // The refresh moved the focus to its button; the person returns to the value.
        region.focus();

        const focused = await focusedDuring(() => collapse(user));

        const cell = await screen.findByRole("button", { name: "Pool exhausted (show full value)" });
        await waitFor(() => expect(cell).toHaveFocus());
        expect(focused.at(-1)).toBe(cell);
        expect(focused).not.toContain(screen.getByRole("region", { name: "Records for incidents" }));
      } finally {
        restore();
      }
    },
  );

  it("expands a cut cell from a press anywhere in it, also where its box does not reach", async () => {
    const user = userEvent.setup();
    renderTable();
    const notesButton = await screen.findByRole("button", { name: CAPPED_NOTES });
    // The box fills the cell through min-height; Firefox resolves a height of
    // 100% against the cell's 1px instead, and would shrink it below its text.
    for (const cell of dataCells()) {
      expect(cell).toHaveClass("h-px");
      expect(cellBox(cell)).toHaveClass("min-h-full");
      expect(cellBox(cell)).not.toHaveClass("h-full");
    }

    // Where the box ends short of the cell, the press lands on the <td> itself.
    const td = notesButton.closest("td")!;
    expect(td).toHaveClass("cursor-pointer");
    await user.click(td);
    expect(valueRegion("notes")).toHaveFocus();
    expect(td).toContainElement(valueRegion("notes"));
    // An expanded cell's own <td> does not collapse it.
    await user.click(td);
    expect(valueRegion("notes")).toBeInTheDocument();

    // A press on a cut cell's resize handle never expands it.
    const stepsTd = screen.getByRole("button", { name: /^Drain the gateway/ }).closest("td")!;
    await user.click(stepsTd.querySelector<HTMLElement>('[data-slot="table-column-resize-handle"]')!);
    expect(queryValueRegion("steps")).not.toBeInTheDocument();

    // A fully visible cell's own <td> stays plain.
    const title = cellWithText("API outage");
    expect(title).not.toHaveClass("cursor-pointer");
    await user.click(title);
    expect(queryValueRegion("title")).not.toBeInTheDocument();
  });

  it("makes a short multi-line string a button, since one line hides its breaks", async () => {
    renderTable();
    const multiLine = await screen.findByRole("button", {
      name: /^Drain the gateway\s+Rotate the pool\s+Confirm recovery \(show full value\)$/,
    });
    expect(steps.length).toBeLessThan(120);
    expect(multiLine).toHaveAttribute("tabindex", "0");
  });
});

describe("table wrap text", () => {
  function wrapToggle() {
    return screen.getByRole("button", { name: "Wrap text" });
  }

  // The classes that produce each mode, beyond the data-wrap marker: wrapped
  // values keep their line breaks and stop at six top-aligned lines; single
  // lines are truncated and vertically centered.
  const WRAPPED = { value: ["whitespace-pre-wrap", "line-clamp-6"], box: ["items-start"] };
  const SINGLE_LINE = { value: ["truncate", "whitespace-nowrap"], box: ["items-center"] };

  function expectCellWrapped(cell: HTMLElement, wrapped: boolean) {
    expect(cell).toHaveAttribute("data-wrap", String(wrapped));
    const box = cellBox(cell);
    // The value span comes first; the sr-only labels follow it.
    const value = box.firstElementChild;
    const [mode, other] = wrapped ? [WRAPPED, SINGLE_LINE] : [SINGLE_LINE, WRAPPED];
    expect(value).toHaveClass(...mode.value);
    expect(box).toHaveClass(...mode.box);
    for (const name of other.value) expect(value).not.toHaveClass(name);
    for (const name of other.box) expect(box).not.toHaveClass(name);
  }

  function expectCellsWrapped(wrapped: boolean) {
    const cells = dataCells();
    expect(cells).toHaveLength(7);
    for (const cell of cells) expectCellWrapped(cell, wrapped);
  }

  /**
   * The header row, the row number and the row actions stay in place while
   * the grid scrolls, in either mode. Wrapped rows grow, so the row number
   * and the actions move to the top of the row.
   */
  function expectStickyFrame(wrapped: boolean) {
    const headers = screen.getAllByRole("columnheader");
    // #, the seven columns, and the row actions.
    expect(headers).toHaveLength(9);
    for (const header of headers) expect(header).toHaveClass("sticky", "top-0");
    const [rowNumber] = within(bodyRow(1)).getAllByRole("cell");
    const rowActions = screen.getByRole("button", { name: "Actions for row 1" }).closest("td");
    expect(rowNumber).toHaveClass("sticky", "left-0");
    expect(rowActions).toHaveClass("sticky", "right-0");
    for (const cell of [rowNumber, rowActions]) {
      if (wrapped) expect(cell).toHaveClass("align-top");
      else expect(cell).not.toHaveClass("align-top");
    }
  }

  it("is off by default, so cells keep a single line", async () => {
    renderTable();
    await gridReady();
    expect(wrapToggle()).toHaveAttribute("aria-pressed", "false");
    expectCellsWrapped(false);
  });

  it("wraps the cells when turned on, saves the choice, and restores single lines when off", async () => {
    const user = userEvent.setup();
    renderTable();
    await gridReady();

    await user.click(wrapToggle());
    expect(wrapToggle()).toHaveAttribute("aria-pressed", "true");
    expectCellsWrapped(true);
    expect(localStorage.getItem(WRAP_TEXT_KEY)).toBe("true");
    // The 600-character limit applies now, so the whole note shows, as plain text.
    const notesCell = cellWithText("Root cause:");
    expect(notesCell).toHaveTextContent(notes);
    expect(within(notesCell).queryByRole("button")).not.toBeInTheDocument();

    await user.click(wrapToggle());
    expect(wrapToggle()).toHaveAttribute("aria-pressed", "false");
    expectCellsWrapped(false);
    expect(localStorage.getItem(WRAP_TEXT_KEY)).toBe("false");
    expect(screen.getByRole("button", { name: CAPPED_NOTES }))
      .toHaveTextContent(`${notes.slice(0, 120)}…`);
  });

  it("keeps the header, row number and row actions sticky in both modes", async () => {
    vaultInfoMock.mockResolvedValue(writer);
    const user = userEvent.setup();
    renderTable();
    await screen.findByRole("button", { name: "Actions for row 1" });
    expectStickyFrame(false);

    await user.click(wrapToggle());
    expectStickyFrame(true);

    await user.click(wrapToggle());
    expectStickyFrame(false);
  });

  it("restores the saved choice when the page mounts again", async () => {
    const user = userEvent.setup();
    const first = renderTable();
    await user.click(await screen.findByRole("button", { name: "Wrap text" }));
    first.unmount();

    renderTable();
    expect(await screen.findByRole("button", { name: "Wrap text" })).toHaveAttribute("aria-pressed", "true");
    expectCellsWrapped(true);
  });

  it("falls back to single lines without crashing when storage is unavailable", async () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("Blocked");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("Blocked");
    });
    try {
      const user = userEvent.setup();
      renderTable();
      const toggle = await screen.findByRole("button", { name: "Wrap text" });
      expect(getItem).toHaveBeenCalledWith(WRAP_TEXT_KEY);
      expect(toggle).toHaveAttribute("aria-pressed", "false");
      expectCellsWrapped(false);

      // Saving fails quietly; the choice still applies to the open page.
      await user.click(toggle);
      expect(setItem).toHaveBeenCalledWith(WRAP_TEXT_KEY, "true");
      expect(toggle).toHaveAttribute("aria-pressed", "true");
      expectCellsWrapped(true);
    } finally {
      getItem.mockRestore();
      setItem.mockRestore();
    }
  });

  it("expands a wrapped cell with the full value, not clamped", async () => {
    listRowsMock.mockResolvedValue(rowsResponse({ notes: longNotes }));
    const user = userEvent.setup();
    renderTable();
    await gridReady();
    await user.click(wrapToggle());

    const cell = screen.getByRole("button", { name: CAPPED_NOTES });
    expectCellWrapped(cell.closest("td")!, true);
    await user.click(cell);

    const region = valueRegion("notes");
    expect(region.textContent).toBe(longNotes);
    expect(region.closest("td")).toHaveAttribute("data-wrap", "true");
    expect(region.querySelector(".line-clamp-6")).toBeNull();
    expect(region.firstElementChild).toHaveClass("whitespace-pre-wrap");
  });

  it("makes a wrapped cell a button only while part of its value is hidden", async () => {
    listRowsMock.mockResolvedValue(rowsResponse({ notes: longNotes }));
    const restore = stubClampedText("API outage");
    try {
      const user = userEvent.setup();
      renderTable();
      await gridReady();
      // A single line measures only the width, so the title still fits there.
      expect(within(cellWithText("API outage")).queryByRole("button")).not.toBeInTheDocument();

      await user.click(wrapToggle());
      // Cut by the 600-character limit.
      const capped = screen.getByRole("button", { name: CAPPED_NOTES });
      expect(capped).toHaveAttribute("tabindex", "0");
      expect(capped).toHaveTextContent(`${longNotes.slice(0, 600)}…`);
      // Taller than its six-line clamp.
      expect(await screen.findByRole("button", { name: "API outage (show full value)" }))
        .toHaveAttribute("tabindex", "0");
      // Short values show in full, line breaks included, as plain text.
      for (const text of ["Drain the gateway", "null"]) {
        expect(within(cellWithText(text)).queryByRole("button")).not.toBeInTheDocument();
      }
    } finally {
      restore();
    }
  });
});
