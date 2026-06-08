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
      uri: "akb://akb/doc/x",
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

  it("calls onSelectUri with the target when a related document is clicked", async () => {
    getDocument.mockResolvedValue({ doc_id: "d-1", title: "Hello", content: "" });
    getRelations.mockResolvedValue({
      doc_id: "d-1",
      uri: "akb://akb/doc/x",
      relations: [
        { direction: "outgoing", relation: "depends_on", uri: "akb://akb/doc/y", name: "Y", resource_type: "document" },
      ],
    });
    const onSelectUri = vi.fn();
    const u = userEvent.setup();
    render(
      wrap(
        <GraphDetailPanel
          vault="akb"
          docId="d-1"
          kind="document"
          uri="akb://akb/doc/x"
          onSelectUri={onSelectUri}
          onFitToNode={() => {}}
          onClose={() => {}}
        />,
      ),
    );
    await u.click(await screen.findByRole("button", { name: "Y" }));
    expect(onSelectUri).toHaveBeenCalledWith("akb://akb/doc/y");
  });

  it("defers META fetches until the section expands", async () => {
    getDocument.mockResolvedValue({ doc_id: "d-1", title: "x", content: "" });
    getRelations.mockResolvedValue({
      doc_id: "d-1",
      uri: "u",
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
    await waitFor(() => expect(getProvenance).toHaveBeenCalledWith("akb", "d-1"));
  });
});

describe("GraphDetailPanel · fetch states", () => {
  it("shows an error state with a retry control when the document fetch fails", async () => {
    getDocument.mockRejectedValue(new Error("404 not found"));
    getRelations.mockResolvedValue({ doc_id: "d-x", uri: "u", relations: [] });

    render(
      wrap(
        <GraphDetailPanel
          vault="akb"
          docId="d-x"
          kind="document"
          uri="akb://akb/doc/x"
          onSelectUri={() => {}}
          onFitToNode={() => {}}
          onClose={() => {}}
        />,
      ),
    );

    expect(await screen.findByText(/couldn't load this resource/i)).toBeTruthy();
    expect(screen.getByText(/404 not found/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /retry/i })).toBeTruthy();
  });

  it("re-fetches when Retry is clicked", async () => {
    getDocument
      .mockRejectedValueOnce(new Error("transient"))
      .mockResolvedValueOnce({ doc_id: "d-x", title: "Recovered", content: "" });
    getRelations.mockResolvedValue({ doc_id: "d-x", uri: "u", relations: [] });

    const u = userEvent.setup();
    render(
      wrap(
        <GraphDetailPanel
          vault="akb"
          docId="d-x"
          kind="document"
          uri="akb://akb/doc/x"
          onSelectUri={() => {}}
          onFitToNode={() => {}}
          onClose={() => {}}
        />,
      ),
    );

    await u.click(await screen.findByRole("button", { name: /retry/i }));
    expect(await screen.findByText("Recovered")).toBeTruthy();
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
    getRelations.mockResolvedValue({ doc_id: "t-1", uri: "u", relations: [] });
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
