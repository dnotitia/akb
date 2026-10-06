import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render as renderBase, screen, waitFor, cleanup, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { GraphDetailPanel } from "../GraphDetailPanel";
import { CurrentUserProvider } from "@/contexts/current-user-context";

function render(ui: React.ReactNode) {
  return renderBase(ui, { wrapper: ({ children }) => <CurrentUserProvider user={{ user_id: "panel-test", username: "reader", email: "reader@example.com", display_name: null, is_admin: false, auth_method: "local", key_class: null }}>{children}</CurrentUserProvider> });
}

const getDocument = vi.fn();
const getRelations = vi.fn();

vi.mock("@/lib/api", () => ({
  getDocument: (...args: unknown[]) => getDocument(...args),
  getRelations: (...args: unknown[]) => getRelations(...args),
}));

const baseProps = {
  vault: "akb", docId: "d-1", name: "Selected resource", kind: "document" as const,
  uri: "akb://akb/coll/Engineering/doc/x.md",
  onSelectRelated: vi.fn(), onFitToNode: vi.fn(), onClose: vi.fn(),
};

function renderPanel(props: Partial<React.ComponentProps<typeof GraphDetailPanel>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><GraphDetailPanel {...baseProps} {...props} /></QueryClientProvider>);
}

function relation(overrides: Record<string, unknown> = {}) {
  return {
    direction: "outgoing", relation: "depends_on", uri: "akb://akb/doc/y",
    name: "Related document", resource_type: "document", kind: "explicit", ...overrides,
  };
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  getDocument.mockReset().mockResolvedValue({ doc_id: "d-1", title: "Selected resource", summary: "A short description", content: "Raw document body" });
  getRelations.mockReset().mockResolvedValue({ uri: baseProps.uri, relations: [] });
});

describe("GraphDetailPanel resource actions", () => {
  it("keeps preview, navigation and exploration usable while optional document details are pending", async () => {
    getDocument.mockReturnValue(new Promise(() => {}));
    const onOpen = vi.fn();
    const onPreview = vi.fn();
    const onFocus = vi.fn();
    const onExpand = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onOpen, onPreview, onFocus, onExpand });
    expect(screen.getByRole("heading", { name: "Selected resource" })).toBeTruthy();
    expect(screen.getByText("Engineering")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await user.click(screen.getByRole("button", { name: "Open in vault" }));
    await user.click(screen.getByRole("button", { name: "Explore connections" }));
    await user.click(screen.getByRole("button", { name: "Load connections" }));
    expect(onPreview).toHaveBeenCalledOnce();
    expect(onOpen).toHaveBeenCalledOnce();
    expect(onFocus).toHaveBeenCalledOnce();
    expect(onExpand).toHaveBeenCalledOnce();
  });

  it("keeps relation results and navigation available after optional details fail", async () => {
    getDocument.mockRejectedValue(new Error("503 unavailable"));
    getRelations.mockResolvedValue({ uri: baseProps.uri, relations: [relation()] });
    renderPanel({ onOpen: vi.fn(), onPreview: vi.fn() });
    expect(await screen.findByRole("button", { name: "Related document" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open in vault" })).not.toHaveAttribute("disabled");
    expect(screen.getByRole("button", { name: "Preview" })).not.toHaveAttribute("disabled");
  });

  it("uses a short summary instead of mounting raw body and duplicate metadata sections", async () => {
    renderPanel({ onPreview: vi.fn() });
    expect(await screen.findByText("A short description")).toBeTruthy();
    expect(screen.queryByText("Raw document body")).toBeNull();
    expect(screen.queryByRole("button", { name: /outline|metadata/i })).toBeNull();
  });

  it.each([
    ["table", "akb://akb/coll/Engineering/table/things"],
    ["file", "akb://akb/coll/Engineering/file/f-1"],
  ] as const)("loads canonical %s relations without inventing unavailable metadata", async (kind, uri) => {
    renderPanel({ kind, uri, onOpen: vi.fn(), onPreview: vi.fn() });
    await screen.findByText("No direct connections");
    expect(getDocument).not.toHaveBeenCalled();
    expect(getRelations).toHaveBeenCalledWith("akb", uri);
    expect(screen.getByRole("button", { name: "Open in vault" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
    expect(screen.queryByText(/not provided/i)).toBeNull();
  });

  it("disables a pending expansion and retries a failed expansion explicitly", async () => {
    const onExpand = vi.fn();
    const pending = renderPanel({ onExpand, expanding: true });
    expect(screen.getByRole("button", { name: /loading connections/i })).toBeDisabled();
    pending.unmount();
    const user = userEvent.setup();
    renderPanel({ onExpand, expansionError: "Network unavailable" });
    expect(screen.getByRole("alert")).toHaveTextContent("Network unavailable");
    await user.click(screen.getByRole("button", { name: "Retry loading connections" }));
    expect(onExpand).toHaveBeenCalledOnce();
  });
});

describe("GraphDetailPanel relations", () => {
  it.each([
    ["incoming", "explicit"], ["outgoing", "explicit"],
    ["incoming", "implicit"], ["outgoing", "implicit"],
    ["incoming", undefined], ["outgoing", undefined],
  ] as const)("preserves %s relation provenance (%s) when following a connection", async (direction, source) => {
    getRelations.mockResolvedValue({ uri: baseProps.uri, relations: [relation({ direction, kind: source })] });
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Related document" }));
    expect(baseProps.onSelectRelated).toHaveBeenCalledWith({
      uri: "akb://akb/doc/y", name: "Related document", kind: "document",
      relation: "depends_on", direction, source,
    });
  });

  it("distinguishes loading, failed and successfully empty relation results", async () => {
    let reject!: (error: Error) => void;
    getRelations.mockReturnValueOnce(new Promise((_, rejectRequest) => { reject = rejectRequest; }));
    const user = userEvent.setup();
    renderPanel();
    expect(screen.getByRole("status", { name: "Loading connections" })).toBeTruthy();
    expect(screen.queryByText("No direct connections")).toBeNull();
    reject(new Error("Connection lookup failed"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection lookup failed");
    expect(screen.queryByText("No direct connections")).toBeNull();
    getRelations.mockResolvedValue({ uri: baseProps.uri, relations: [] });
    await user.click(screen.getByRole("button", { name: "Retry connections" }));
    expect(await screen.findByText("No direct connections")).toBeTruthy();
  });

  it("labels direction and preserves resource kind, relation and URI when traversing", async () => {
    getRelations.mockResolvedValue({ uri: baseProps.uri, relations: [
      relation(),
      relation({ direction: "incoming", relation: "references", uri: "akb://akb/table/reports", name: "Reports", resource_type: "table" }),
    ] });
    const user = userEvent.setup();
    renderPanel();
    const outgoing = await screen.findByRole("region", { name: "Outgoing connections" });
    const incoming = screen.getByRole("region", { name: "Incoming connections" });
    expect(within(outgoing).getByText(/depends on/i)).toBeTruthy();
    await user.click(within(incoming).getByRole("button", { name: "Reports" }));
    expect(baseProps.onSelectRelated).toHaveBeenCalledWith({ uri: "akb://akb/table/reports", name: "Reports", kind: "table", relation: "references", direction: "incoming", source: "explicit" });
    expect(baseProps.onFitToNode).toHaveBeenCalledWith("akb://akb/table/reports");
  });

  it("bounds the initial relation list and progressively reveals more connections", async () => {
    getRelations.mockResolvedValue({ uri: baseProps.uri, relations: Array.from({ length: 80 }, (_, index) => relation({ uri: `akb://akb/doc/${index}`, name: `Neighbor ${index}` })) });
    const user = userEvent.setup();
    renderPanel();
    const region = await screen.findByRole("region", { name: "Outgoing connections" });
    expect(within(region).getAllByRole("button", { name: /^Neighbor/ })).toHaveLength(25);
    expect(screen.queryByRole("button", { name: "Neighbor 79" })).toBeNull();
    await user.click(within(region).getByRole("button", { name: /show more/i }));
    expect(within(region).getAllByRole("button", { name: /^Neighbor/ })).toHaveLength(50);
  });

  it("infers a file target from its canonical URI when legacy responses omit resource_type", async () => {
    getRelations.mockResolvedValue({ uri: baseProps.uri, relations: [relation({ resource_type: undefined, uri: "akb://akb/coll/Engineering/file/f-2", name: "Architecture image", relation: "attached_to" })] });
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Architecture image" }));
    expect(baseProps.onSelectRelated).toHaveBeenCalledWith({ uri: "akb://akb/coll/Engineering/file/f-2", name: "Architecture image", kind: "file", relation: "attached_to", direction: "outgoing", source: "explicit" });
  });

  it("does not replace a newly selected resource's connections with a late response", async () => {
    let resolveOld!: (value: unknown) => void;
    getRelations.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const panel = render(<QueryClientProvider client={client}><GraphDetailPanel {...baseProps} /></QueryClientProvider>);
    getRelations.mockResolvedValue({ uri: "akb://akb/file/f-new", relations: [relation({ name: "Current connection" })] });
    panel.rerender(<QueryClientProvider client={client}><GraphDetailPanel {...baseProps} kind="file" docId="f-new" uri="akb://akb/file/f-new" name="Current file" /></QueryClientProvider>);
    expect(await screen.findByRole("button", { name: "Current connection" })).toBeTruthy();
    resolveOld({ uri: baseProps.uri, relations: [relation({ name: "Old connection" })] });
    await waitFor(() => expect(client.getQueryState(["relations", "panel-test", 0, "akb", baseProps.uri])?.status).toBe("success"));
    expect(screen.queryByRole("button", { name: "Old connection" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Current file" })).toBeTruthy();
  });
});

describe("GraphDetailPanel secondary controls and focus", () => {
  it("keeps pin, center and hide in a nonmodal menu whose Escape does not close the inspector", async () => {
    const onTogglePin = vi.fn();
    const onHide = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onTogglePin, onHide });
    expect(screen.queryByRole("menuitem", { name: "Pin position" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Resource actions" }));
    expect(document.body.style.pointerEvents).not.toBe("none");
    expect(screen.getByRole("menuitem", { name: "Center on resource" })).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: "Hide from this view" })).toBeTruthy();
    await user.click(screen.getByRole("menuitem", { name: "Pin position" }));
    expect(onTogglePin).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "Resource actions" }));
    await user.keyboard("{Escape}");
    expect(baseProps.onClose).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("handles Escape only inside the inspector and restores its launching control without scrolling", async () => {
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    trigger.focus();
    const focusSpy = vi.spyOn(trigger, "focus");
    const user = userEvent.setup();
    renderPanel();
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Selected resource" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(baseProps.onClose).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Close inspector" }));
    expect(baseProps.onClose).toHaveBeenCalledOnce();
    expect(focusSpy).toHaveBeenCalledWith({ preventScroll: true });
    trigger.remove();
  });

  it("closes on a local Escape and does not forward it to graph shortcuts", async () => {
    const shortcut = vi.fn();
    document.addEventListener("keydown", shortcut);
    renderPanel();
    fireEvent.keyDown(screen.getByRole("heading", { name: "Selected resource" }), { key: "Escape" });
    await waitFor(() => expect(baseProps.onClose).toHaveBeenCalledOnce());
    expect(shortcut).not.toHaveBeenCalled();
    document.removeEventListener("keydown", shortcut);
  });

  it("provides the canonical URI for manual selection when clipboard access fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValueOnce(new Error("Clipboard denied"));
    renderPanel();
    await user.click(screen.getByRole("button", { name: "Resource actions" }));
    await user.click(screen.getByRole("menuitem", { name: "Copy URI" }));
    expect(await screen.findByText("Couldn't copy. Select the URI below.")).toBeTruthy();
    expect(screen.getByText(baseProps.uri)).toBeTruthy();
    expect(screen.queryByText("URI copied")).toBeNull();
  });
});
