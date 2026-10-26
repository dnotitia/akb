import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

function FileHarness({ identity = "alpha", checking = false, revision = 0 }: { identity?: string; checking?: boolean; revision?: number }) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  const user: CurrentUser = {
    user_id: identity, username: identity, email: "test@example.com", display_name: null,
    is_admin: false, auth_method: "session", key_class: null,
  };
  return (
    <QueryClientProvider client={client}><MemoryRouter initialEntries={["/vault/ops/file/f-first"]}>
      <CurrentUserProvider user={user} checking={checking} revision={revision}>
        <ResourceLocationProvider identity={identity} checking={checking} revision={revision}>
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
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("file reading workspace", () => {
  it("rechecks mutation permissions when a transient metadata failure is retried", async () => {
    const user = userEvent.setup();
    vaultInfo.mockResolvedValue({ role: "writer", is_archived: false, is_external_git: false } as Awaited<ReturnType<typeof getVaultInfo>>);
    let metadataReads = 0;
    fileFetch.mockImplementation((path) => String(path).endsWith("/download")
      ? response({ download_url: "https://files.example/first" })
      : ++metadataReads === 1 ? response({}, 503) : response(firstFile));
    render(<FileHarness />);
    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: /Actions for/ })).toBeInTheDocument();
    expect(vaultInfo).toHaveBeenCalledTimes(2);
  });

  it("retains the mounted preview during refresh and failure until an explicit reader reload", async () => {
    const user = userEvent.setup();
    render(<FileHarness />);
    const frame = await screen.findByTitle(firstFile.name);
    fireEvent.load(frame);
    const pending = deferred<Response>();
    fileFetch.mockImplementation(() => pending.promise);
    await user.click(screen.getByRole("button", { name: "Refresh link" }));
    expect(screen.getByTitle(firstFile.name)).toBe(frame);
    expect(screen.getByRole("region", { name: "File preview" })).toHaveAttribute("aria-busy", "true");
    await act(async () => pending.resolve(await response({}, 503)));
    expect(await screen.findByText(/Couldn't refresh the file link/)).toBeInTheDocument();
    expect(screen.getByTitle(firstFile.name)).toBe(frame);
    expect(screen.getByRole("region", { name: "File preview" })).toHaveAttribute("aria-busy", "false");
    fileFetch.mockImplementation(() => response({ download_url: "https://files.example/renewed" }));
    await user.click(screen.getByRole("button", { name: "Refresh link" }));
    await waitFor(() => expect(screen.queryByText(/Couldn't refresh the file link/)).toBeNull());
    expect(screen.getByTitle(firstFile.name)).toBe(frame);
    expect(screen.getByRole("link", { name: "Download file" })).toHaveAttribute("href", "https://files.example/renewed");
    await user.click(screen.getByRole("button", { name: "Reload preview" }));
    expect(screen.getByTitle(firstFile.name)).not.toBe(frame);
    expect(screen.getByTitle(firstFile.name)).toHaveAttribute("src", "https://files.example/renewed");
  });

  it("publishes resolved location and keeps metadata in the labelled Info disclosure", async () => {
    const user = userEvent.setup();
    const { container } = render(<FileHarness />);
    const download = await screen.findByRole("link", { name: "Download file" });
    expect(download).toHaveAttribute("href", "https://files.example/first");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveClass("sr-only");
    expect(container.querySelectorAll('[data-slot="resource-command-row"]')).toHaveLength(1);
    expect(screen.getByTestId("location")).toHaveTextContent(JSON.stringify({
      vault: "ops", title: firstFile.name, kind: "File", collectionPath: "guides", resourceRef: "f-first",
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

  it("renders an application/yaml file through the existing text preview without parsing it", async () => {
    const file = {
      ...firstFile,
      name: "deployment.yaml",
      mime_type: "application/yaml",
      size_bytes: 94,
    };
    const raw = "# Synthetic settings\nservice:\n  message: 'keep: spaces'\n  labels: [unfinished\n";
    fileFetch.mockImplementation((path) => String(path).endsWith("/download")
      ? response({
          name: file.name,
          download_url: "https://files.example/deployment.yaml",
          mime_type: "application/octet-stream",
        })
      : response(file));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(raw)));

    const { container } = render(<FileHarness />);
    expect(await screen.findByRole("link", { name: "Download file" })).toHaveAttribute(
      "href",
      "https://files.example/deployment.yaml",
    );
    await waitFor(() => expect(container.querySelector("pre")?.textContent).toBe(raw));
    expect(screen.queryByText("Preview unavailable")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "File preview" })).toContainElement(
      container.querySelector("pre"),
    );
  });

  it.each([
    { name: "missing-type.yaml", mime_type: undefined },
    { name: "generic-type.yml", mime_type: "application/octet-stream" },
  ])("infers a text preview for $name from its YAML extension", async ({ name, mime_type }) => {
    const file = { ...firstFile, name, mime_type };
    const raw = "# Inferred synthetic settings\nservice:\n  mode: plain\n";
    fileFetch.mockImplementation((path) => String(path).endsWith("/download")
      ? response({ name, download_url: "https://files.example/inferred-yaml" })
      : response(file));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(raw)));

    const { container } = render(<FileHarness />);
    await screen.findByRole("link", { name: "Download file" });
    await waitFor(() => expect(container.querySelector("pre")?.textContent).toBe(raw));
    expect(screen.getByText("Text")).toBeInTheDocument();
    expect(screen.queryByText("Preview unavailable")).not.toBeInTheDocument();
  });

  it.each([403, 404])("distinguishes renewed-link HTTP %s and clears private metadata", async (status) => {
    const user = userEvent.setup();
    render(<FileHarness />);
    await screen.findByRole("link", { name: "Download file" });
    fileFetch.mockImplementation((path) => String(path).endsWith("/download")
      ? response({ detail: "Unavailable" }, status) : response(firstFile));
    await user.click(screen.getByRole("button", { name: "Refresh link" }));
    expect(await screen.findByText(status === 403 ? "You no longer have permission to read this file." : "File not found in vault.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Download file" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Info" })).toBeNull();
    expect(screen.getByTestId("location")).toHaveTextContent("No resource");
    expect(screen.queryByRole("heading", { name: firstFile.name })).toBeNull();
  });

  it("refreshes an expired preview and download grant without inventing file deletion", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-14T00:00:00Z"));
    let grant = 0;
    fileFetch.mockImplementation((path) => String(path).endsWith("/download")
      ? response({ download_url: `https://files.example/grant-${++grant}`, expires_in: 1 }) : response(firstFile));
    await act(async () => { render(<FileHarness />); });
    expect(screen.getByRole("link", { name: "Download file" })).toHaveAttribute("href", "https://files.example/grant-1");
    await act(async () => { vi.advanceTimersByTime(1000); });
    expect(screen.getByText(/file link has expired/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download file" })).toBeDisabled();
    await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: "Refresh link" })[0]); });
    expect(screen.getByRole("link", { name: "Download file" })).toHaveAttribute("href", "https://files.example/grant-2");
    expect(screen.queryByText(/file link has expired/)).toBeNull();
    expect(screen.getByTestId("location")).toHaveTextContent(firstFile.name);
  });

  it("keeps metadata and offers recovery for a temporary link-refresh failure", async () => {
    const user = userEvent.setup();
    render(<FileHarness />);
    await screen.findByRole("link", { name: "Download file" });
    fileFetch.mockImplementation((path) => String(path).endsWith("/download")
      ? response({}, 503) : response(firstFile));
    await user.click(screen.getByRole("button", { name: "Refresh link" }));
    expect(await screen.findByText("Couldn't refresh the file link (503). Try again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Info" })).toBeEnabled();
    fileFetch.mockImplementation((path) => String(path).endsWith("/download")
      ? response({ download_url: "https://files.example/refreshed" }) : response(firstFile));
    await user.click(screen.getAllByRole("button", { name: "Refresh link" })[0]);
    expect(await screen.findByRole("link", { name: "Download file" })).toHaveAttribute("href", "https://files.example/refreshed");
  });

  it("isolates loaded files and Info during verification and ignores late refreshes", async () => {
    const user = userEvent.setup();
    const oldRefresh = deferred<Response>();
    const view = render(<FileHarness />);
    await user.click(await screen.findByRole("button", { name: "Info" }));
    view.rerender(<FileHarness checking />);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("Mina")).toBeNull();
    expect(screen.queryByRole("link", { name: "Download file" })).toBeNull();
    view.rerender(<FileHarness revision={1} />);
    await screen.findByRole("link", { name: "Download file" });
    fileFetch.mockImplementation((path) => String(path).includes("f-first/download")
      ? oldRefresh.promise : String(path).endsWith("/download")
        ? response({ download_url: "https://files.example/second" }) : response({ ...firstFile, name: "Second.pdf" }));
    await user.click(screen.getByRole("button", { name: "Refresh link" }));
    await user.click(screen.getByRole("link", { name: "Next file" }));
    await screen.findByRole("link", { name: "Download file" });
    await act(async () => { oldRefresh.resolve(await response({ download_url: "https://files.example/old-private-link" })); });
    expect(screen.getByRole("link", { name: "Download file" })).toHaveAttribute("href", "https://files.example/second");
    expect(screen.getByTestId("location")).toHaveTextContent("Second.pdf");
  });
});
