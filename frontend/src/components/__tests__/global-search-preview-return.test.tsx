import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation, useNavigate, type Location } from "react-router-dom";
import { GlobalSearchDialog } from "@/components/global-search-dialog";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { listVaults, searchDocs, type CurrentUser } from "@/lib/api";
import * as previewNavigation from "@/lib/document-preview-navigation";

vi.mock("@/lib/api", () => ({ searchDocs: vi.fn(), listVaults: vi.fn() }));

const CURRENT_USER: CurrentUser = {
  user_id: "user-1", username: "mina", email: "mina@example.com", display_name: "Mina",
  is_admin: false, auth_method: "local", key_class: null,
};

let lastPreview: Location | null = null;

/** Exercise the Search side of the post-unmount notification contract. */
function PreviewHarness() {
  const location = useLocation();
  const navigate = useNavigate();
  const preview = previewNavigation.documentPreviewBackground(location);
  const previousPreview = useRef<Location | null>(null);
  useEffect(() => {
    const previous = previousPreview.current;
    previousPreview.current = preview ? location : null;
    if (previous && !preview && location.key === previewNavigation.documentPreviewBackground(previous)?.key) {
      lastPreview = previous;
      previewNavigation.notifyDocumentPreviewClosed(previous);
    }
  }, [location, preview]);
  function close() {
    navigate(-1);
  }
  return <>
    <GlobalSearchDialog />
    <output data-testid="location">{location.pathname}</output>
    <button onClick={() => navigate("/vault/alpha/doc/from-page", { state: previewNavigation.documentPreviewState(location) })}>Page result</button>
    {preview && <Dialog open onOpenChange={open => { if (!open) close(); }}>
      <DialogContent aria-describedby={undefined} onCloseAutoFocus={event => event.preventDefault()}>
        <DialogTitle>Preview</DialogTitle>
        <button onClick={close}>Dismiss preview</button>
        <button onClick={() => navigate(-1)}>Browser Back</button>
        <button onClick={() => {
          lastPreview = location;
          navigate(location.pathname, { replace: true, state: null });
        }}>Open in vault</button>
      </DialogContent>
    </Dialog>}
  </>;
}

function contents(user = CURRENT_USER) {
  return <MemoryRouter initialEntries={["/vault/alpha/members"]}>
    <CurrentUserProvider user={user}><PreviewHarness /></CurrentUserProvider>
  </MemoryRouter>;
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  lastPreview = null;
  vi.mocked(listVaults).mockResolvedValue({ vaults: [{ name: "alpha" }, { name: "beta" }] });
  vi.mocked(searchDocs).mockResolvedValue({
    query: "postgres", total: 2, returned: 2, total_matches: 2,
    results: ["alpha", "beta"].map(vault => ({
      source_type: "document", uri: `akb://${vault}/coll/notes/doc/postgres.md`, vault,
      path: "notes/postgres.md", title: `${vault} PostgreSQL`, score: 1,
    })),
  });
});

afterEach(cleanup);

async function searchAndPreview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Search knowledge" }));
  await user.click(screen.getByRole("button", { name: "Search scope: alpha" }));
  await user.click(await screen.findByRole("menuitemcheckbox", { name: "beta" }));
  await user.keyboard("{Escape}");
  await user.click(screen.getByRole("button", { name: "Documents" }));
  await user.type(screen.getByRole("combobox"), "postgres");
  await screen.findByRole("option", { name: /beta PostgreSQL/ });
  await user.keyboard("{ArrowDown}");
  const ledger = screen.getByRole("listbox").closest(".rail-scroll") as HTMLElement;
  ledger.scrollTop = 120;
  await user.keyboard("{Enter}");
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(screen.getByRole("dialog", { name: "Preview" })).toBeInTheDocument();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
}

describe("returning to the global Search session", () => {
  it.each(["Dismiss preview", "Browser Back"])("restores query, vaults, kind, selected result and scroll after %s", async (closeLabel) => {
    const user = userEvent.setup();
    render(contents());
    await searchAndPreview(user);
    const calls = vi.mocked(searchDocs).mock.calls.length;
    await user.click(screen.getByRole("button", { name: closeLabel }));

    const input = await screen.findByRole("combobox", { name: "Search in alpha, beta" });
    expect(input).toHaveValue("postgres");
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute("aria-activedescendant", "global-search-result-1");
    expect(screen.getByRole("option", { name: /beta PostgreSQL/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: "Documents, 2 results" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("listbox").closest(".rail-scroll")).toHaveProperty("scrollTop", 120);
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(vi.mocked(searchDocs).mock.calls).toHaveLength(calls);

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    let restored: boolean | undefined;
    act(() => { restored = previewNavigation.notifyDocumentPreviewClosed(lastPreview!); });
    expect(restored).toBe(false);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("does not restore the suspended session after full-page promotion", async () => {
    const user = userEvent.setup();
    render(contents());
    await searchAndPreview(user);
    await user.click(screen.getByRole("button", { name: "Open in vault" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    let restored: boolean | undefined;
    act(() => { restored = previewNavigation.notifyDocumentPreviewClosed(lastPreview!); });
    expect(restored).toBe(false);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("does not restore an earlier account's suspended search", async () => {
    const user = userEvent.setup();
    const { rerender } = render(contents());
    await searchAndPreview(user);
    rerender(contents({ ...CURRENT_USER, user_id: "user-2" }));
    await user.click(screen.getByRole("button", { name: "Dismiss preview" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Search knowledge" }));
    expect(screen.getByRole("combobox")).toHaveValue("");
  });

  it("keeps ordinary page-result previews independent of global Search", async () => {
    const user = userEvent.setup();
    render(contents());
    await user.click(screen.getByRole("button", { name: "Page result" }));
    await user.click(screen.getByRole("button", { name: "Dismiss preview" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByTestId("location")).toHaveTextContent("/vault/alpha/members");
  });
});
