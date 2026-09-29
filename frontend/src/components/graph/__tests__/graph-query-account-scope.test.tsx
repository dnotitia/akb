import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { getDocument, getGraph, getGraphOverview, getRelations, type CurrentUser } from "@/lib/api";
import { GraphDetailPanel } from "../GraphDetailPanel";
import { useFullGraph, useNeighborhood } from "../use-graph-data";

vi.mock("@/lib/api", () => ({ getDocument: vi.fn(), getGraph: vi.fn(), getGraphOverview: vi.fn(), getRelations: vi.fn() }));
const overview = vi.mocked(getGraphOverview);
const neighborhood = vi.mocked(getGraph);
const resource = vi.mocked(getDocument);
const relations = vi.mocked(getRelations);
const uri = "akb://team/doc/selected.md";
const account: CurrentUser = { user_id: "alice", username: "alice", email: "alice@example.com", display_name: null, is_admin: false, auth_method: "local", key_class: null };

function graph(name: string) {
  return { nodes: [{ uri, name, resource_type: "document", degree: 0, depth: 0 }], edges: [] };
}

function scopeHarness() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const scope = { user: account as CurrentUser | null, revision: 0, checking: false };
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}><CurrentUserProvider {...scope}>{children}</CurrentUserProvider></QueryClientProvider>;
  }
  return { client, scope, Wrapper };
}

function inspectorResponse(title: string) {
  resource.mockResolvedValue({ doc_id: "selected.md", title, summary: title + " description", content: "Body" });
  relations.mockResolvedValue({ uri, relations: [{ direction: "outgoing", relation: "references", uri: "akb://team/doc/neighbor.md", resource_type: "document", kind: "explicit", name: title + " neighbor" }] });
}

beforeEach(() => {
  vi.resetAllMocks();
  overview.mockResolvedValue(graph("Initial overview"));
  neighborhood.mockResolvedValue(graph("Initial neighborhood"));
  inspectorResponse("Initial resource");
});

describe("graph query account and access scope", () => {
  it.each(["account", "revision"] as const)("does not reuse overview or neighborhood data after the %s changes", async (change) => {
    const { scope, Wrapper } = scopeHarness();
    const { result, rerender } = renderHook(() => ({
      overview: useFullGraph("team", true), neighborhood: useNeighborhood("team", uri, 1),
    }), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.overview.data?.nodes[0].name).toBe("Initial overview"));
    await waitFor(() => expect(result.current.neighborhood.data?.nodes[0].name).toBe("Initial neighborhood"));

    overview.mockResolvedValue(graph("New overview"));
    neighborhood.mockResolvedValue(graph("New neighborhood"));
    if (change === "account") scope.user = { ...account, user_id: "bob" };
    else scope.revision = 1;
    rerender();
    await waitFor(() => expect(result.current.overview.data?.nodes[0].name).toBe("New overview"));
    expect(result.current.neighborhood.data?.nodes[0].name).toBe("New neighborhood");
  });

  it("waits for access verification before fetching either graph query", async () => {
    const { scope, Wrapper } = scopeHarness();
    scope.checking = true;
    const { result, rerender } = renderHook(() => ({
      overview: useFullGraph("team", true), neighborhood: useNeighborhood("team", uri, 1),
    }), { wrapper: Wrapper });
    expect(overview).not.toHaveBeenCalled();
    expect(neighborhood).not.toHaveBeenCalled();
    expect(result.current.overview.fetchStatus).toBe("idle");
    expect(result.current.neighborhood.fetchStatus).toBe("idle");
    scope.checking = false;
    rerender();
    await waitFor(() => expect(result.current.overview.isSuccess && result.current.neighborhood.isSuccess).toBe(true));
  });

  it("does not fetch graph data without an authenticated account", () => {
    const { scope, Wrapper } = scopeHarness();
    scope.user = null;
    renderHook(() => ({ overview: useFullGraph("team", true), neighborhood: useNeighborhood("team", uri, 1) }), { wrapper: Wrapper });
    expect(overview).not.toHaveBeenCalled();
    expect(neighborhood).not.toHaveBeenCalled();
  });
});

describe("inspector query account and access scope", () => {
  const props = { vault: "team", docId: "selected.md", uri, name: "Selection", kind: "document" as const, onSelectRelated: vi.fn(), onFitToNode: vi.fn(), onClose: vi.fn() };

  it.each(["account", "revision"] as const)("does not reuse descriptions or connections after the %s changes", async (change) => {
    const { scope, Wrapper } = scopeHarness();
    const tree = () => <Wrapper><GraphDetailPanel {...props} /></Wrapper>;
    const panel = render(tree());
    expect(await screen.findByText("Initial resource description")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Initial resource neighbor" })).toBeInTheDocument();
    inspectorResponse("New resource");
    if (change === "account") scope.user = { ...account, user_id: "bob" };
    else scope.revision = 1;
    panel.rerender(tree());
    expect(screen.queryByText("Initial resource description")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Initial resource neighbor" })).not.toBeInTheDocument();
    expect(await screen.findByText("New resource description")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "New resource neighbor" })).toBeInTheDocument();
  });

  it("waits for access verification before fetching descriptions or connections", async () => {
    const { scope, Wrapper } = scopeHarness();
    scope.checking = true;
    const tree = () => <Wrapper><GraphDetailPanel {...props} /></Wrapper>;
    const panel = render(tree());
    expect(resource).not.toHaveBeenCalled();
    expect(relations).not.toHaveBeenCalled();
    scope.checking = false;
    panel.rerender(tree());
    expect(await screen.findByText("Initial resource description")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Initial resource neighbor" })).toBeInTheDocument();
  });

  it("does not fetch resource details without an authenticated account", () => {
    const { scope, Wrapper } = scopeHarness();
    scope.user = null;
    render(<Wrapper><GraphDetailPanel {...props} /></Wrapper>);
    expect(resource).not.toHaveBeenCalled();
    expect(relations).not.toHaveBeenCalled();
  });
});
