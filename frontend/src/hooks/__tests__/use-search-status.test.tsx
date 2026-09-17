import { StrictMode } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SearchStatusProvider, useSearchStatus } from "../use-search-status";

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ authenticatedFetch: mocks.fetch }));
afterEach(() => { cleanup(); mocks.fetch.mockReset(); });
const body = { vector_store: { backfill: { upsert: { pending: 3, retrying: 0, abandoned: 0 } } } };
function Consumer() {
  const { observations, summary } = useSearchStatus();
  return <><output aria-label="Vault observations">{observations.map(row => row.vaultName).join(",")}</output><output aria-label="Search summary">{summary.description}</output></>;
}
describe("Shared search status provider", () => {
  it("retains authorized Vault observations while a revoked Vault makes coverage partial", async () => {
    mocks.fetch.mockImplementation(async (url: string) => {
      if (url.endsWith("/my/vaults")) return new Response(JSON.stringify({ vaults: [{ id: "a", name: "Allowed" }, { id: "b", name: "Revoked" }] }));
      if (url.endsWith("/Revoked")) return new Response("", { status: 403 });
      return new Response(JSON.stringify(body));
    });
    render(<QueryClientProvider client={new QueryClient()}><SearchStatusProvider identity="user" enabled><Consumer /></SearchStatusProvider></QueryClientProvider>);
    await waitFor(() => expect(screen.getByLabelText("Vault observations")).toHaveTextContent("Allowed"));
    await waitFor(() => expect(screen.getByLabelText("Search summary")).toHaveTextContent("Accessible vault scope could not be verified"));
    expect(screen.getByLabelText("Vault observations")).not.toHaveTextContent("Revoked");
    expect(screen.getByLabelText("Search summary")).toHaveTextContent("1 vault");
  });
  it("gates old observations immediately during foreground revalidation under StrictMode", async () => {
    mocks.fetch.mockImplementation(async (url: string) => new Response(JSON.stringify(url.endsWith("/my/vaults") ? { vaults: [{ id: "a", name: "Private vault" }] } : body)));
    const client = new QueryClient();
    const tree = (enabled: boolean) => <StrictMode><QueryClientProvider client={client}><SearchStatusProvider identity="user" enabled={enabled}><Consumer /></SearchStatusProvider></QueryClientProvider></StrictMode>;
    const view = render(tree(true));
    await waitFor(() => expect(screen.getByLabelText("Vault observations")).toHaveTextContent("Private vault"));
    view.rerender(tree(false));
    expect(screen.getByLabelText("Vault observations")).toHaveTextContent("");
    expect(screen.getByLabelText("Vault observations")).not.toHaveTextContent("Private vault");
    view.rerender(tree(true));
    await waitFor(() => expect(screen.getByLabelText("Vault observations")).toHaveTextContent("Private vault"));
  });
});
