import type { ComponentProps } from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { searchDocs, type SearchResponse } from "@/lib/api";
import { GraphToolbar } from "../GraphToolbar";
import { DEFAULT_VIEW, type NodeKind, type RelationKind } from "../graph-types";

vi.mock("@/lib/api", () => ({ searchDocs: vi.fn() }));
const search = vi.mocked(searchDocs);
const account = { user_id: "reader", username: "reader", email: "reader@example.com", display_name: null, is_admin: false, auth_method: "local", key_class: null };
const documentNode = { uri: "akb://team/coll/guides/doc/roadmap.md", name: "Roadmap", kind: "document" as const };
const response = (title: string, uri = "akb://team/coll/guides/doc/remote.md"): SearchResponse => ({
  query: title, total: 1, returned: 1, total_matches: 1,
  results: [{ uri, path: "guides/remote.md", title, source_type: "document" }],
});

function setup(overrides: Partial<ComponentProps<typeof GraphToolbar>> = {}) {
  const props = {
    vault: "team", view: DEFAULT_VIEW, onChange: vi.fn(), onNavigate: vi.fn(), hubs: [],
    nodeCount: 1, edgeCount: 0, displayMode: "graph" as const, onDisplayModeChange: vi.fn(),
    orphanCount: 0, hideOrphans: false, onToggleOrphans: vi.fn(), hiddenCount: 0,
    onUnhideAll: vi.fn(), onFit: vi.fn(), ...overrides,
  };
  const tree = (next = props) => <CurrentUserProvider user={account}><GraphToolbar {...next} /></CurrentUserProvider>;
  const rendered = render(tree());
  return { props, ...rendered, rerender: (next: Partial<typeof props>) => rendered.rerender(tree({ ...props, ...next })) };
}

beforeEach(() => { localStorage.clear(); search.mockReset(); search.mockResolvedValue({ query: "", total: 0, returned: 0, total_matches: 0, results: [] }); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe("GraphToolbar resource lookup", () => {
  it("keeps keyboard order from view choice through resource lookup to secondary actions", async () => {
    const user = userEvent.setup();
    setup();
    screen.getByRole("button", { name: "Graph" }).focus();
    await user.tab();
    expect(screen.getByRole("button", { name: "List" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("combobox", { name: "Find a resource" })).toHaveFocus();
    await user.keyboard("{Escape}");
    await user.tab();
    expect(screen.getByRole("button", { name: "Filters" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "More graph actions" })).toHaveFocus();
  });

  it("shows only currently loaded resources from history and uses their current titles", () => {
    localStorage.setItem("akb-graph-recent:v2:reader:team", JSON.stringify([
      { doc_id: "guides/roadmap.md", uri: documentNode.uri, title: "Outdated title" },
      { doc_id: "private.md", uri: "akb://team/doc/private.md", title: "Inaccessible title" },
    ]));
    setup({ nodes: [documentNode] });
    fireEvent.focus(screen.getByRole("combobox"));
    expect(screen.getByRole("option", { name: /Roadmap/ })).toBeInTheDocument();
    expect(screen.queryByText("Outdated title")).not.toBeInTheDocument();
    expect(screen.queryByText("Inaccessible title")).not.toBeInTheDocument();
  });

  it("selects a loaded resource without replacing the scene or waiting for Vault search", () => {
    const onSelect = vi.fn();
    const { props } = setup({ nodes: [documentNode], onSelect });
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "road" } });
    expect(screen.getByRole("group", { name: "Loaded resources" })).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith(documentNode.uri);
    expect(props.onChange).not.toHaveBeenCalled();
    expect(input).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps loaded matches before a separately labelled Vault search group", async () => {
    vi.useFakeTimers();
    search.mockResolvedValue(response("Roadmap outside this scene"));
    setup({ nodes: [documentNode], onSelect: vi.fn() });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "road" } });
    await act(() => vi.advanceTimersByTimeAsync(260));
    expect(screen.getAllByRole("option").map(option => option.textContent)).toEqual([
      expect.stringContaining("Roadmap"), expect.stringContaining("Roadmap outside this scene"),
    ]);
    expect(within(screen.getByRole("group", { name: "Vault search" })).getByRole("option")).toHaveTextContent("Roadmap outside this scene");
  });

  it.each([
    ["document", "akb://team/coll/guides/doc/remote.md"],
    ["table", "akb://team/coll/metrics/table/usage"],
    ["file", "akb://team/coll/assets/file/image.png"],
  ])("focuses %s results by canonical URI at one hop, ignoring unowned preferences", async (kind, uri) => {
    vi.useFakeTimers();
    localStorage.setItem("akb:graph:hops", "3");
    const data = response("Remote", uri);
    data.results[0].source_type = kind;
    search.mockResolvedValue(data);
    const { props } = setup();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "remote" } });
    await act(() => vi.advanceTimersByTimeAsync(260));
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
    expect(props.onChange).toHaveBeenCalledWith(expect.objectContaining({ entry: uri, hops: 1, selected: undefined }));
  });

  it("removes results immediately when the query changes and ignores a late response", async () => {
    vi.useFakeTimers();
    let finishOld!: (value: SearchResponse) => void;
    search.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }));
    search.mockResolvedValueOnce(response("New result"));
    setup();
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "old" } });
    await act(() => vi.advanceTimersByTimeAsync(260));
    fireEvent.change(input, { target: { value: "new" } });
    await act(async () => finishOld(response("Old result")));
    expect(screen.queryByRole("option", { name: /Old result/ })).not.toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(260));
    expect(screen.getByRole("option", { name: /New result/ })).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "next" } });
    expect(screen.queryByRole("option", { name: /New result/ })).not.toBeInTheDocument();
  });

  it("shows a retryable search failure while loaded matches stay selectable", async () => {
    vi.useFakeTimers();
    search.mockRejectedValueOnce(new Error("Service unavailable"));
    search.mockResolvedValueOnce(response("Recovered"));
    setup({ nodes: [documentNode], onSelect: vi.fn() });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "road" } });
    await act(() => vi.advanceTimersByTimeAsync(260));
    expect(screen.getByRole("alert")).toHaveTextContent(/search.*unavailable/i);
    expect(screen.queryByText(/no matching resources/i)).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Roadmap/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await act(async () => {});
    expect(screen.getByRole("option", { name: /Recovered/ })).toBeInTheDocument();
  });

  it("does not describe a degraded empty search as no matches", async () => {
    vi.useFakeTimers();
    search.mockResolvedValue({ query: "road", total: 0, returned: 0, total_matches: 0, results: [], degraded: true });
    setup();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "road" } });
    await act(() => vi.advanceTimersByTimeAsync(260));
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByText(/no matching resources/i)).not.toBeInTheDocument();
  });

  it("keeps the keyboard active option visible and Escape only closes lookup", () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    const { props } = setup({ nodes: [documentNode, { ...documentNode, uri: "akb://team/doc/second.md", name: "Roadmap two" }] });
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "road" } });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    const active = screen.getAllByRole("option")[1];
    expect(input).toHaveAttribute("aria-activedescendant", active.id);
    expect(scroll).toHaveBeenLastCalledWith({ block: "nearest" });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(props.onChange).not.toHaveBeenCalled();
  });
});

describe("GraphToolbar commands", () => {
  it("keeps the icon-only More control named and keyboard operable", async () => {
    const user = userEvent.setup();
    setup();
    const more = screen.getByRole("button", { name: "More graph actions" });
    expect(more).toHaveTextContent(/^$/);
    expect(more).toHaveAttribute("title", "More graph actions");
    more.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("menuitem", { name: /Graph help/ })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(more).toHaveFocus();
  });

  it("composes rapid menu and legend filters before the parent commits the next view", async () => {
    const user = userEvent.setup();
    const view = { ...DEFAULT_VIEW, entry: documentNode.uri, selected: documentNode.uri, hops: 2 as const };
    const { props, rerender } = setup({ view, orphanCount: 6 });
    await user.click(screen.getByRole("button", { name: "Filters" }));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "table" }));
    // The router may defer the new view while local controls rerender urgently.
    rerender({ hideOrphans: true });
    await user.click(screen.getByRole("menuitemcheckbox", { name: "file" }));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "references" }));
    expect(props.onChange).toHaveBeenLastCalledWith({
      ...view,
      types: new Set(["document"]),
      relations: new Set(["depends_on", "implements", "related_to", "attached_to", "derived_from", "links_to"]),
    });
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Show documents" }));
    expect(props.onChange).toHaveBeenLastCalledWith({
      ...view,
      types: new Set(),
      relations: new Set(["depends_on", "implements", "related_to", "attached_to", "derived_from", "links_to"]),
    });

    // A committed external navigation replaces pending filter state.
    const restored = { ...DEFAULT_VIEW, types: new Set<NodeKind>(["file"]) };
    rerender({ view: restored });
    await user.click(screen.getByRole("button", { name: "Show tables" }));
    expect(props.onChange).toHaveBeenLastCalledWith({ ...restored, types: new Set(["file", "table"]) });
  });

  it("toggles resource types directly while preserving the focused selection and relationship filters", async () => {
    const user = userEvent.setup();
    const view = {
      ...DEFAULT_VIEW,
      entry: documentNode.uri,
      selected: "akb://team/coll/metrics/table/usage",
      hops: 2 as const,
      relations: new Set<RelationKind>(["references"]),
    };
    const { props, rerender } = setup({ view, resourceCounts: { document: 8, table: 2, file: 0 } });
    const tables = screen.getByRole("button", { name: "Show tables" });
    expect(tables).toHaveAttribute("aria-pressed", "true");
    expect(tables).toHaveAccessibleDescription("2 tables loaded in this graph");
    await user.click(tables);
    expect(props.onChange).toHaveBeenCalledWith({ ...view, types: new Set(["document", "file"]) });
    expect(view.types).toEqual(new Set(["document", "table", "file"]));

    rerender({ view: { ...view, types: new Set<NodeKind>(["document", "file"]) } });
    expect(tables).toHaveAttribute("aria-pressed", "false");
    expect(tables).toHaveTextContent("2");
    await user.click(tables);
    expect(props.onChange).toHaveBeenLastCalledWith({ ...view, types: new Set(["document", "file", "table"]) });
  });

  it("allows zero-count types and the last visible type to be toggled", async () => {
    const user = userEvent.setup();
    const view = { ...DEFAULT_VIEW, types: new Set<NodeKind>(["file"]) };
    const { props, rerender } = setup({ view, resourceCounts: { document: 0, table: 0, file: 0 } });
    const files = screen.getByRole("button", { name: "Show files" });
    expect(files).toBeEnabled();
    expect(files).toHaveAccessibleDescription("0 files loaded in this graph");
    await user.click(files);
    expect(props.onChange).toHaveBeenCalledWith({ ...view, types: new Set() });

    rerender({ view: { ...view, types: new Set<NodeKind>() } });
    expect(files).toHaveAttribute("aria-pressed", "false");
    await user.click(files);
    expect(props.onChange).toHaveBeenLastCalledWith({ ...view, types: new Set(["file"]) });
  });

  it("does not invent resource counts when the loaded graph totals are unavailable", () => {
    setup();
    for (const name of ["Show documents", "Show tables", "Show files"]) {
      const toggle = screen.getByRole("button", { name });
      expect(toggle).not.toHaveTextContent(/\d/);
      expect(toggle).not.toHaveAttribute("aria-describedby");
    }
  });

  it("preserves saving and reopening a focused view from More", async () => {
    const user = userEvent.setup();
    const { props } = setup({ view: { ...DEFAULT_VIEW, entry: documentNode.uri, hops: 1 } });
    await user.click(screen.getByRole("button", { name: "More graph actions" }));
    await user.click(screen.getByRole("menuitem", { name: /saved views/i }));
    const name = screen.getByRole("textbox", { name: "View name" });
    await user.type(name, "Roadmap reading");
    expect(name).toHaveValue("Roadmap reading");
    await user.keyboard("{Enter}");
    expect(localStorage.getItem("akb-graph-saves:v2:reader:team")).toContain("Roadmap reading");
    await user.click(screen.getByRole("button", { name: "Roadmap reading" }));
    expect(props.onNavigate).toHaveBeenCalledWith(expect.stringContaining("entry=akb%3A%2F%2Fteam"));
  });

  it("keeps filtering within the loaded-resource scope", async () => {
    const user = userEvent.setup();
    const { props } = setup();
    await user.click(screen.getByRole("button", { name: "Filters" }));
    expect(screen.getByText("Filters apply to loaded resources.")).toBeInTheDocument();
    await user.click(screen.getByRole("menuitemcheckbox", { name: "table" }));
    expect(props.onChange).toHaveBeenCalledWith(expect.objectContaining({ types: new Set(["document", "file"]) }));
  });

  it("puts saved views, help and rearrange behind one More control", async () => {
    const user = userEvent.setup();
    const { props } = setup({ onRearrange: vi.fn() });
    expect(screen.queryByRole("button", { name: "Saved graph views" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Graph help" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "More graph actions" }));
    expect(screen.getByRole("menuitem", { name: /saved views/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /graph help/i })).toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: /rearrange/i }));
    expect(props.onRearrange).toHaveBeenCalledOnce();
  });

  it("only exposes neighborhood controls when focused, without a permanent resource-count row", () => {
    const { rerender } = setup();
    expect(screen.queryByText("1 resource")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "1 hop neighborhood" })).not.toBeInTheDocument();
    rerender({ view: { ...DEFAULT_VIEW, entry: documentNode.uri } });
    expect(screen.getByRole("button", { name: "1 hop neighborhood" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "3 hop neighborhood" }));
    expect(localStorage.getItem("akb:graph:hops")).toBeNull();
  });
});
