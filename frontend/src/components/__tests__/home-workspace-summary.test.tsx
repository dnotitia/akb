import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HomeWorkspaceSummary } from "../home-workspace-summary";
import { getWorkspaceSummary, WorkspaceSummaryAccessDenied, WorkspaceSummaryUnavailable, type WorkspaceSummary } from "@/lib/workspace-summary";

vi.mock("@/lib/workspace-summary", async () => ({
  ...await vi.importActual<typeof import("@/lib/workspace-summary")>("@/lib/workspace-summary"),
  getWorkspaceSummary: vi.fn(),
}));
const sample: WorkspaceSummary = { version: 1, scope: "accessible", observed_at: "2026-09-14T01:00:00Z", vault_count: 20, document_count: 8_001, table_count: 12, file_count: 86 };
const clients: QueryClient[] = [];
function setup() {
  const client = new QueryClient();
  clients.push(client);
  const tree = (userId = "user-a", vaultCount: number | undefined = 20, directoryLoading = false, accessChecking = false, accessKey = "0") => <QueryClientProvider client={client}>
    <HomeWorkspaceSummary userId={userId} accessKey={accessKey} accessChecking={accessChecking} vaultCount={vaultCount} directoryLoading={directoryLoading} />
  </QueryClientProvider>;
  return { client, tree };
}
beforeEach(() => { vi.mocked(getWorkspaceSummary).mockReset().mockResolvedValue(sample); });
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.clearAllMocks(); });

describe("Home cardless workspace summary", () => {
  it("shows complete workspace totals, not four preview cards", async () => {
    const { tree } = setup();
    render(tree());
    const region = screen.getByRole("region", { name: "Workspace summary" });
    expect(await within(region).findByText("8,001")).toBeInTheDocument();
    expect(within(region).getByText("20")).toBeInTheDocument();
    expect(within(region).getAllByRole("definition")).toHaveLength(4);
    expect(within(region).queryByText("Available to you")).not.toBeInTheDocument();
    expect(within(region).getByText("Resources in vaults you can access.")).toHaveClass("sr-only");
    expect(within(region).queryByRole("button")).not.toBeInTheDocument();
    expect(getWorkspaceSummary).toHaveBeenCalledTimes(1);
  });
  it("keeps old-server exact Vault totals and makes absent resource counts explicit", async () => {
    vi.mocked(getWorkspaceSummary).mockRejectedValue(new WorkspaceSummaryUnavailable());
    const { tree } = setup();
    render(tree());
    expect(await screen.findByText("Totals unavailable")).toBeInTheDocument();
    expect(screen.getByText("20")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Unavailable")).toHaveLength(3);
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });
  it("keeps supported fields when other fields are omitted", async () => {
    vi.mocked(getWorkspaceSummary).mockResolvedValue({ ...sample, table_count: undefined });
    const { tree } = setup();
    render(tree());
    expect(await screen.findByText("8,001")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Unavailable")).toHaveLength(1);
  });
  it("shows only zero Vaults for a verified empty workspace", async () => {
    vi.mocked(getWorkspaceSummary).mockResolvedValue({ ...sample, vault_count: 0, document_count: 0, table_count: 0, file_count: 0 });
    const { tree } = setup();
    render(tree("user-a", 0));
    expect(await screen.findByText("0")).toBeInTheDocument();
    expect(screen.getAllByRole("definition")).toHaveLength(1);
    expect(screen.queryByText("Totals unavailable")).not.toBeInTheDocument();
  });
  it("does not read summary or claim zero before identity verification", async () => {
    const { tree } = setup();
    const view = render(tree("user-a", undefined, true, true));
    expect(screen.getByRole("status", { name: "Loading workspace totals" })).toBeInTheDocument();
    expect(getWorkspaceSummary).not.toHaveBeenCalled();
    // Explicit undefined through props, bypassing the helper's default argument.
    view.rerender(<QueryClientProvider client={clients[0]}><HomeWorkspaceSummary userId="" accessKey="none" accessChecking={false} vaultCount={undefined} directoryLoading={false} /></QueryClientProvider>);
    expect(screen.getByText("Totals unavailable")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Unavailable")).toHaveLength(4);
    expect(getWorkspaceSummary).not.toHaveBeenCalled();
  });
  it("reads the independently scoped summary before the directory and never restarts on its completion", async () => {
    const { tree } = setup();
    const view = render(tree("user-a", undefined, true));
    expect(await screen.findByText("8,001")).toBeInTheDocument();
    expect(screen.getAllByRole("definition")).toHaveLength(4);
    expect(screen.queryByRole("status", { name: "Loading workspace totals" })).not.toBeInTheDocument();
    view.rerender(tree("user-a", 20, false));
    expect(getWorkspaceSummary).toHaveBeenCalledTimes(1);
  });
  it("exposes the verified Vault count to assistive technology while the resource counts are pending", async () => {
    let finish!: (value: WorkspaceSummary) => void;
    vi.mocked(getWorkspaceSummary).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const { tree } = setup();
    render(tree());
    expect(screen.getByText("20")).toBeInTheDocument();
    expect(screen.getAllByRole("definition")).toHaveLength(4);
    expect(screen.getAllByLabelText("Loading")).toHaveLength(3);
    expect(screen.getByRole("status", { name: "Loading workspace totals" })).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    expect(screen.queryByText("Totals unavailable")).not.toBeInTheDocument();
    await waitFor(() => expect(getWorkspaceSummary).toHaveBeenCalledTimes(1));
    await act(async () => finish(sample));
    expect(await screen.findByText("8,001")).toBeInTheDocument();
  });
  it("clears previous totals during foreground proof and uses a new access epoch for an unchanged account", async () => {
    let finish!: (value: WorkspaceSummary) => void;
    const { tree } = setup();
    const view = render(tree());
    await screen.findByText("8,001");
    view.rerender(tree("user-a", 20, false, true));
    expect(screen.queryByText("8,001")).not.toBeInTheDocument();
    expect(screen.queryByText("20")).not.toBeInTheDocument();
    expect(getWorkspaceSummary).toHaveBeenCalledTimes(1);
    vi.mocked(getWorkspaceSummary).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    view.rerender(tree("user-a", 20, true, false, "1"));
    await waitFor(() => expect(getWorkspaceSummary).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("8,001")).not.toBeInTheDocument();
    await act(async () => finish({ ...sample, vault_count: 2, document_count: 7 }));
    expect(await screen.findByText("7")).toBeInTheDocument();
    expect(screen.queryByText("8,001")).not.toBeInTheDocument();
  });
  it("does not use directory fallback after an explicit access denial", async () => {
    vi.mocked(getWorkspaceSummary).mockRejectedValue(new WorkspaceSummaryAccessDenied());
    const { tree } = setup();
    render(tree());
    expect(await screen.findByText("Totals unavailable")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Unavailable")).toHaveLength(4);
    expect(screen.queryByText("20")).not.toBeInTheDocument();
  });
  it("hides prior account totals and aborts an obsolete read on identity changes", async () => {
    let resolve!: (value: WorkspaceSummary) => void;
    let oldSignal: AbortSignal | undefined;
    vi.mocked(getWorkspaceSummary).mockImplementationOnce(signal => {
      oldSignal = signal;
      return new Promise(done => { resolve = done; });
    }).mockResolvedValueOnce({ ...sample, document_count: 7 });
    const { tree } = setup();
    const view = render(tree());
    await waitFor(() => expect(getWorkspaceSummary).toHaveBeenCalledTimes(1));
    view.rerender(tree("user-b"));
    expect(await screen.findByText("7")).toBeInTheDocument();
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => resolve(sample));
    expect(screen.queryByText("8,001")).not.toBeInTheDocument();
  });
  it("never retains previous private totals after refresh fails", async () => {
    const { tree, client } = setup();
    render(tree());
    await screen.findByText("8,001");
    vi.mocked(getWorkspaceSummary).mockRejectedValue(new Error("Offline"));
    await act(async () => { await client.invalidateQueries({ queryKey: ["workspace-summary"] }); });
    expect(await screen.findByText("Totals unavailable")).toBeInTheDocument();
    expect(screen.queryByText("8,001")).not.toBeInTheDocument();
  });
});
