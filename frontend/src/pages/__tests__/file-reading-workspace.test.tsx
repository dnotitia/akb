import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceLocationProvider, useResourceLocation } from "@/contexts/resource-location-context";
import { authenticatedFetch, getVaultInfo, type CurrentUser } from "@/lib/api";
import FilePage from "@/pages/file";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  authenticatedFetch: vi.fn(),
  getVaultInfo: vi.fn(),
  browseVault: vi.fn(async () => ({ items: [] })),
}));
vi.mock("@/components/file-viewer", () => ({
  FilePreviewBody: ({ name }: { name: string }) => <p>Preview of {name}</p>,
}));

const fileFetch = vi.mocked(authenticatedFetch);
const vaultInfo = vi.mocked(getVaultInfo);
const firstFile = {
  uri: "akb://ops/coll/guides/file/f-first",
  name: "Architecture.pdf",
  collection: "guides",
  mime_type: "application/pdf",
  size_bytes: 2048,
  created_by: "Mina",
  created_at: "2026-09-01T04:45:00Z",
};

function response(data: unknown, status = 200) {
  return Promise.resolve({ ok: status < 400, status, json: async () => data } as Response);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function LocationProbe() {
  const location = useResourceLocation();
  return <output data-testid="location">{location ? JSON.stringify(location) : "No resource"}</output>;
}

function FileHarness({ identity = "alpha" }: { identity?: string }) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  const user: CurrentUser = {
    user_id: identity, username: identity, email: "test@example.com", display_name: null,
    is_admin: false, auth_method: "session", key_class: null,
  };
  return (
    <QueryClientProvider client={client}><MemoryRouter initialEntries={["/vault/ops/file/f-first"]}>
      <CurrentUserProvider user={user}>
        <ResourceLocationProvider identity={identity}>
          <LocationProbe />
          <Link to="/vault/ops/file/f-second">Next file</Link>
          <Routes><Route path="/vault/:name/file/:id" element={<FilePage />} /></Routes>
        </ResourceLocationProvider>
      </CurrentUserProvider>
    </MemoryRouter></QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vaultInfo.mockResolvedValue({ role: "reader", is_archived: false, is_external_git: false } as Awaited<ReturnType<typeof getVaultInfo>>);
  fileFetch.mockImplementation((path) => String(path).endsWith("/download")
    ? response({ name: firstFile.name, download_url: "https://files.example/first" })
    : response(firstFile));
});
afterEach(cleanup);

describe("file reading workspace", () => {
  it("publishes resolved location and keeps metadata in the labelled Info disclosure", async () => {
    const user = userEvent.setup();
    const { container } = render(<FileHarness />);
    const download = await screen.findByRole("link", { name: "Download file" });
    expect(download).toHaveAttribute("href", "https://files.example/first");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveClass("sr-only");
    expect(container.querySelectorAll('[data-slot="resource-command-row"]')).toHaveLength(1);
    expect(screen.getByTestId("location")).toHaveTextContent(JSON.stringify({
      vault: "ops", title: firstFile.name, kind: "File", collectionPath: "guides",
    }));
    expect(screen.queryByText("Mina")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Actions for/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Info" }));
    const dialog = screen.getByRole("dialog", { name: "File info" });
    expect(within(dialog).getByText("Mina")).toBeInTheDocument();
    expect(within(dialog).getByText("guides")).toBeInTheDocument();
    expect(within(dialog).getByText("2.0 KB")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Info" })).toHaveFocus();
  });

  it("clears the old route title and ignores a late preview response after navigation", async () => {
    const user = userEvent.setup();
    const oldPreview = deferred<Response>();
    const nextList = deferred<Response>();
    let listCount = 0;
    fileFetch.mockImplementation((path) => {
      if (String(path).includes("f-first/download")) return oldPreview.promise;
      if (String(path).includes("f-second/download")) return response({ download_url: "https://files.example/second" });
      return ++listCount === 1 ? response(firstFile) : nextList.promise;
    });
    render(<FileHarness />);
    await screen.findByRole("button", { name: "Info" });
    await user.click(screen.getByRole("link", { name: "Next file" }));
    expect(screen.getByTestId("location")).toHaveTextContent("No resource");
    expect(screen.queryByRole("heading", { name: firstFile.name })).not.toBeInTheDocument();
    await act(async () => oldPreview.resolve(await response({ download_url: "https://files.example/first" })));
    expect(screen.queryByRole("link", { name: "Download file" })).not.toBeInTheDocument();
    await act(async () => nextList.resolve(await response({ ...firstFile, name: "Second.pdf", uri: "akb://ops/file/f-second", collection: undefined })));
    expect(await screen.findByRole("link", { name: "Download file" })).toHaveAttribute("href", "https://files.example/second");
    expect(screen.getByTestId("location")).toHaveTextContent('"title":"Second.pdf"');
    expect(screen.getByTestId("location")).not.toHaveTextContent("guides");
  });

  it("does not reuse a previous account's file title on the same route", async () => {
    const nextList = deferred<Response>();
    const view = render(<FileHarness />);
    await screen.findByRole("link", { name: "Download file" });
    fileFetch.mockImplementation((path) => String(path).endsWith("/download")
      ? response({ download_url: "https://files.example/second" }) : nextList.promise);
    view.rerender(<FileHarness identity="beta" />);
    expect(screen.getByTestId("location")).toHaveTextContent("No resource");
    expect(screen.queryByRole("heading", { name: firstFile.name })).not.toBeInTheDocument();
    await act(async () => nextList.resolve(await response({ detail: "File not found" }, 404)));
    expect(await screen.findByText("File not found in vault.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("No resource"));
    expect(screen.queryByRole("link", { name: "Download file" })).not.toBeInTheDocument();
  });
});
