import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TablePublishDialog } from "@/components/table-publish-dialog";
import { buildTablePublicationQuery } from "@/lib/table-publication";

vi.mock("@/lib/api", () => ({
  createPublication: vi.fn(),
  createPublicationSnapshot: vi.fn(),
  deletePublication: vi.fn(),
  getDocument: vi.fn(),
  previewTablePublicationQuery: vi.fn(),
}));

import {
  createPublication,
  createPublicationSnapshot,
  deletePublication,
  previewTablePublicationQuery,
} from "@/lib/api";

const createPublicationMock = createPublication as unknown as ReturnType<typeof vi.fn>;
const snapshotMock = createPublicationSnapshot as unknown as ReturnType<typeof vi.fn>;
const deletePublicationMock = deletePublication as unknown as ReturnType<typeof vi.fn>;
const previewMock = previewTablePublicationQuery as unknown as ReturnType<typeof vi.fn>;

const COLUMNS = [
  { name: "ticket_id", type: "uuid", primary_key: true },
  { name: "title", type: "text" },
  { name: "internal_note", type: "text" },
];

const LIVE_PUBLICATION = {
  slug: "tickets-live",
  share_url: "https://example.test/p/tickets-live",
  resource_type: "table_query",
  resource_uri: null,
  vault: "demo",
  title: "tickets",
  mode: "live",
  expires_at: null,
  max_views: null,
  view_count: 0,
  allow_embed: true,
  section_filter: null,
  password_protected: false,
  created_at: "2026-08-31T00:00:00Z",
  snapshot_at: null,
  query_sql: "SELECT ticket_id, title FROM tickets ORDER BY ticket_id LIMIT 100",
  query_vault_names: ["demo"],
  query_params: {},
} as const;

beforeEach(() => {
  vi.clearAllMocks();
  previewMock.mockResolvedValue({
    kind: "table_query",
    columns: ["ticket_id", "title"],
    items: [{ ticket_id: "t-1", title: "First" }],
    total: 1,
  });
  createPublicationMock.mockResolvedValue(LIVE_PUBLICATION);
  snapshotMock.mockResolvedValue({
    ...LIVE_PUBLICATION,
    mode: "snapshot",
    snapshot_at: "2026-08-31T01:00:00Z",
  });
  deletePublicationMock.mockResolvedValue({ deleted: 1 });
});

afterEach(cleanup);

describe("table publication query builder", () => {
  it("keeps catalog order, adds deterministic primary-key ordering, and bounds rows", () => {
    expect(
      buildTablePublicationQuery({
        table: "tickets",
        columns: COLUMNS,
        selectedColumns: ["title", "ticket_id"],
        rowLimit: 250,
      }),
    ).toBe("SELECT ticket_id, title FROM tickets ORDER BY ticket_id LIMIT 250");
  });

  it("rejects malformed identifiers and unbounded selections", () => {
    expect(() =>
      buildTablePublicationQuery({
        table: "tickets;drop",
        columns: COLUMNS,
        selectedColumns: ["ticket_id"],
        rowLimit: 100,
      }),
    ).toThrow(/cannot be published safely/i);
    expect(() =>
      buildTablePublicationQuery({
        table: "tickets",
        columns: COLUMNS,
        selectedColumns: [],
        rowLimit: 100,
      }),
    ).toThrow(/select at least one column/i);
  });
});

describe("TablePublishDialog", () => {
  it("requires a current server preview before publishing selected columns", async () => {
    const user = userEvent.setup();
    const onPublished = vi.fn();
    render(
      <TablePublishDialog
        open
        onOpenChange={vi.fn()}
        vault="demo"
        table="tickets"
        columns={COLUMNS}
        onPublished={onPublished}
      />,
    );

    const publish = screen.getByRole("button", { name: "Publish live table" });
    expect(publish).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /internal_note/i }));
    await user.click(screen.getByRole("button", { name: "Preview query" }));

    const sql = "SELECT ticket_id, title FROM tickets ORDER BY ticket_id LIMIT 100";
    await waitFor(() => expect(previewMock).toHaveBeenCalledWith("demo", sql));
    expect(await screen.findByText("Verified")).toBeInTheDocument();
    expect(publish).toBeEnabled();

    await user.click(publish);
    await waitFor(() => {
      expect(createPublicationMock).toHaveBeenCalledWith("demo", {
        resource_type: "table_query",
        query_sql: sql,
        query_vault_names: ["demo"],
        title: "tickets",
        password: undefined,
        expires_in: undefined,
        max_views: undefined,
      });
    });
    expect(onPublished).toHaveBeenCalledWith(LIVE_PUBLICATION);
  });

  it("turns a newly-created live link into a snapshot", async () => {
    const user = userEvent.setup();
    const onPublished = vi.fn();
    render(
      <TablePublishDialog
        open
        onOpenChange={vi.fn()}
        vault="demo"
        table="tickets"
        columns={COLUMNS.slice(0, 2)}
        onPublished={onPublished}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^Snapshot/i }));
    await user.click(screen.getByRole("button", { name: "Preview query" }));
    await screen.findByText("Verified");
    await user.click(screen.getByRole("button", { name: "Publish snapshot" }));

    await waitFor(() => expect(snapshotMock).toHaveBeenCalledWith("demo", "tickets-live"));
    expect(onPublished).toHaveBeenCalledWith(
      expect.objectContaining({ slug: "tickets-live", mode: "snapshot" }),
    );
  });

  it("revokes the temporary live link when snapshot creation fails", async () => {
    snapshotMock.mockRejectedValue(new Error("Snapshot storage unavailable"));
    const user = userEvent.setup();
    render(
      <TablePublishDialog
        open
        onOpenChange={vi.fn()}
        vault="demo"
        table="tickets"
        columns={COLUMNS.slice(0, 2)}
        onPublished={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^Snapshot/i }));
    await user.click(screen.getByRole("button", { name: "Preview query" }));
    await screen.findByText("Verified");
    await user.click(screen.getByRole("button", { name: "Publish snapshot" }));

    expect(
      await screen.findByText(/Snapshot storage unavailable.*temporary live link was removed/i),
    ).toBeInTheDocument();
    expect(deletePublicationMock).toHaveBeenCalledWith("demo", "tickets-live");
  });

  it("surfaces a direct recovery path when snapshot cleanup also fails", async () => {
    snapshotMock.mockRejectedValue(new Error("Snapshot storage unavailable"));
    deletePublicationMock.mockRejectedValue(new Error("Revocation unavailable"));
    const user = userEvent.setup();
    render(
      <TablePublishDialog
        open
        onOpenChange={vi.fn()}
        vault="demo"
        table="tickets"
        columns={COLUMNS.slice(0, 2)}
        onPublished={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /^Snapshot/i }));
    await user.click(screen.getByRole("button", { name: "Preview query" }));
    await screen.findByText("Verified");
    await user.click(screen.getByRole("button", { name: "Publish snapshot" }));

    expect(
      await screen.findByText(/could not be removed; revoke it from Publish immediately/i),
    ).toBeInTheDocument();
    expect(deletePublicationMock).toHaveBeenCalledWith("demo", "tickets-live");
  });
});
