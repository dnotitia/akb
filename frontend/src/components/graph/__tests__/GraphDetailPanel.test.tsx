import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { GraphDetailPanel } from "../GraphDetailPanel";

const getDocument = vi.fn();
const getRelations = vi.fn();
const getProvenance = vi.fn();
const drillDown = vi.fn();

vi.mock("@/lib/api", () => ({
  getDocument: (...args: unknown[]) => getDocument(...args),
  getRelations: (...args: unknown[]) => getRelations(...args),
  getProvenance: (...args: unknown[]) => getProvenance(...args),
  drillDown: (...args: unknown[]) => drillDown(...args),
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

afterEach(cleanup);
beforeEach(() => {
  getDocument.mockReset();
  getRelations.mockReset();
  getProvenance.mockReset();
  drillDown.mockReset();
});

describe("GraphDetailPanel · document node", () => {
  it("renders title, summary, preview, and relations after fetch", async () => {
    getDocument.mockResolvedValue({
      doc_id: "d-1",
      title: "Hello",
      summary: "A nice doc",
      tags: ["alpha", "beta"],
      content: "line1\nline2\nline3",
      type: "document",
    });
    getRelations.mockResolvedValue({
      doc_id: "d-1",
      resource_uri: "akb://akb/doc/x",
      relations: [
        {
          direction: "outgoing",
          relation: "depends_on",
          uri: "akb://akb/doc/y",
          name: "Y",
          resource_type: "document",
        },
      ],
    });

    render(
      wrap(
        <GraphDetailPanel
          vault="akb"
          docId="d-1"
          kind="document"
          uri="akb://akb/doc/x"
          onSelectUri={() => {}}
          onFitToNode={() => {}}
          onClose={() => {}}
        />,
      ),
    );

    expect(await screen.findByText("Hello")).toBeTruthy();
    expect(screen.getByText("A nice doc")).toBeTruthy();
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText(/depends_on/i)).toBeTruthy();
    expect(screen.getByText(/line1/)).toBeTruthy();
  });

  it("defers META fetches until the section expands", async () => {
    getDocument.mockResolvedValue({ doc_id: "d-1", title: "x", content: "" });
    getRelations.mockResolvedValue({
      doc_id: "d-1",
      resource_uri: "u",
      relations: [],
    });
    getProvenance.mockResolvedValue({ provenance: { source: "manual" } });

    const u = userEvent.setup();
    render(
      wrap(
        <GraphDetailPanel
          vault="akb"
          docId="d-1"
          kind="document"
          uri="u"
          onSelectUri={() => {}}
          onFitToNode={() => {}}
          onClose={() => {}}
        />,
      ),
    );
    await screen.findByText("x");
    expect(getProvenance).not.toHaveBeenCalled();
    await u.click(screen.getByRole("button", { name: /toggle meta/i }));
    await waitFor(() => expect(getProvenance).toHaveBeenCalledWith("d-1"));
  });
});

describe("GraphDetailPanel · table node", () => {
  it("does not show preview for tables", async () => {
    getDocument.mockResolvedValue({
      doc_id: "t-1",
      title: "Things",
      columns: ["a", "b"],
      type: "table",
    });
    getRelations.mockResolvedValue({ doc_id: "t-1", resource_uri: "u", relations: [] });
    render(
      wrap(
        <GraphDetailPanel
          vault="akb"
          docId="t-1"
          kind="table"
          uri="u"
          onSelectUri={() => {}}
          onFitToNode={() => {}}
          onClose={() => {}}
        />,
      ),
    );
    await screen.findByText("Things");
    expect(screen.queryByText(/§ PREVIEW/i)).toBeNull();
  });
});
