import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import { GlobalSearchDialog } from "@/components/global-search-dialog";
import { listVaults, searchDocs } from "@/lib/api";

vi.mock("@/lib/api", () => ({ listVaults: vi.fn(), searchDocs: vi.fn() }));
const search = vi.mocked(searchDocs);
const empty = { query: "guide", results: [], total: 0, returned: 0, total_matches: 0 };
function Location() { const location = useLocation(); return <output data-testid="url">{location.pathname + location.search}</output>; }
function mount() { render(<MemoryRouter initialEntries={["/vault/test4/members"]}><GlobalSearchDialog /><Location /></MemoryRouter>); }
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listVaults).mockResolvedValue({ vaults: [{ name: "test4" }, { name: "test3" }, { name: "team" }] });
  search.mockResolvedValue(empty);
});
afterEach(cleanup);

it("adds a second Vault without losing query/kind, and removes chips without leaving the workspace", async () => {
  const user = userEvent.setup(); mount();
  await user.click(screen.getByRole("button", { name: "Search knowledge" }));
  await user.click(screen.getByRole("button", { name: "Files" }));
  await user.type(screen.getByRole("combobox"), "guide");
  await user.click(screen.getByRole("button", { name: "Search scope: test4" }));
  await user.type(screen.getByRole("searchbox", { name: "Filter vaults" }), "test3");
  await user.click(await screen.findByRole("menuitemcheckbox", { name: "test3" }));
  await user.keyboard("{Escape}");
  expect(screen.getByRole("combobox")).toHaveValue("guide");
  await waitFor(() => expect(search).toHaveBeenLastCalledWith("guide", ["test4", "test3"], 12, { source_type: "file" }));
  expect(screen.getByTestId("url")).toHaveTextContent("/vault/test4/members");
  await user.click(screen.getByRole("button", { name: "Remove test4 from search scope" }));
  await waitFor(() => expect(search).toHaveBeenLastCalledWith("guide", ["test3"], 12, { source_type: "file" }));
  await user.click(screen.getByRole("button", { name: "Remove test3 from search scope" }));
  await waitFor(() => expect(search).toHaveBeenLastCalledWith("guide", [], 12, { source_type: "file" }));
  expect(screen.getByRole("button", { name: "Search scope: All vaults" })).toHaveFocus();
});

it("offers accessible Vault names while typing and consumes the selected name into a chip", async () => {
  const user = userEvent.setup(); mount();
  await user.click(screen.getByRole("button", { name: "Search knowledge" }));
  const input = screen.getByRole("combobox");
  await user.type(input, "test3");
  await user.click(await screen.findByRole("button", { name: "Add test3 to search scope" }));
  expect(input).toHaveValue("");
  expect(input).toHaveFocus();
  expect(screen.getByRole("button", { name: "Remove test3 from search scope" })).toBeVisible();
  await user.type(input, "guide");
  await waitFor(() => expect(search).toHaveBeenLastCalledWith("guide", ["test4", "test3"], 12, { source_type: undefined }));
  await user.click(screen.getByRole("button", { name: "Continue in search page" }));
  const url = new URL(screen.getByTestId("url").textContent!, "https://example.invalid");
  expect(url.pathname).toBe("/search");
  expect(url.searchParams.get("v")).toBe("test4,test3");
  expect(url.searchParams.get("q")).toBe("guide");
});

it("discards late single-Vault results after adding another Vault", async () => {
  let finish!: (value: typeof empty) => void;
  search.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const user = userEvent.setup(); mount();
  await user.click(screen.getByRole("button", { name: "Search knowledge" }));
  await user.type(screen.getByRole("combobox"), "guide");
  await waitFor(() => expect(search).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole("button", { name: "Search scope: test4" }));
  await user.click(await screen.findByRole("menuitemcheckbox", { name: "test3" }));
  await user.keyboard("{Escape}");
  await waitFor(() => expect(search).toHaveBeenCalledTimes(2));
  await act(async () => finish({ ...empty, results: [{ title: "Stale", uri: "akb://test4/coll/a/doc/b.md", vault: "test4", path: "a/b.md", score: 1 }] } as typeof empty));
  expect(screen.queryByRole("option")).not.toBeInTheDocument();
  expect(screen.getByText("No results for “guide”")).toBeVisible();
});
