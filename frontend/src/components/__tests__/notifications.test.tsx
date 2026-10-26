import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { NotificationBell } from "@/components/notification-bell";
import NotificationsPage from "@/pages/notifications";
import { DocumentWatch } from "@/components/document-watch";
import * as api from "@/lib/api-notifications";
import { registerModal } from "@/lib/modal-visibility";

vi.mock("@/lib/api-notifications", async original => ({
  ...await original<typeof import("@/lib/api-notifications")>(),
  notificationCount: vi.fn(), notificationPage: vi.fn(), markNotification: vi.fn(),
  markNotificationSnapshot: vi.fn(), documentSubscription: vi.fn(),
}));
const user = { user_id: "user-a", username: "reader", email: "reader@example.invalid", display_name: "Reader", is_admin: false, auth_method: "local", key_class: null };
const item: api.NotificationItem = { id: "n1", kind: "document.update", title: "Release notes updated", message: "A watched document changed.", created_at: "2026-09-08T00:00:00Z", updated_at: "2026-09-08T00:00:00Z", read: false, version: "v1", target: { uri: "akb://demo/doc/notes.md", vault: "demo" } };
const page: api.NotificationPage = { supported: true, items: [item], next_cursor: null, unread_count: 1, snapshot: "snapshot-before-arrival", retention_days: 90 };
let desktopViewport = true;
const viewportListeners = new Set<EventListenerOrEventListenerObject>();
function setDesktopViewport(desktop: boolean) {
  desktopViewport = desktop;
  const event = new Event("change");
  viewportListeners.forEach(listener => typeof listener === "function" ? listener(event) : listener.handleEvent(event));
}
async function openNotificationActions(actor: ReturnType<typeof userEvent.setup>, title = item.title) {
  await actor.click(await screen.findByRole("button", { name: `Actions for notification: ${title}` }));
}
async function markNotificationRead(actor: ReturnType<typeof userEvent.setup>, title = item.title) {
  await openNotificationActions(actor, title);
  await actor.click(screen.getByRole("menuitem", { name: "Mark read" }));
}
function mount(children: React.ReactNode, initialEntry = "/", revision = 0) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><CurrentUserProvider user={user} revision={revision}><MemoryRouter initialEntries={[initialEntry]}>{children}</MemoryRouter></CurrentUserProvider></QueryClientProvider>);
}
function Destination() { const location = useLocation(); return <output data-testid="notification-destination">{location.pathname}</output>; }
function InboxRoutes() {
  return <><NotificationBell /><Routes>
    <Route path="/notifications" element={<NotificationsPage />} />
    <Route path="/vault/:name" element={<Destination />} />
    <Route path="/vault/:name/doc/:id" element={<Destination />} />
  </Routes></>;
}
beforeEach(() => {
  desktopViewport = true;
  vi.spyOn(window, "matchMedia").mockImplementation(query => ({
    matches: query === "(min-width: 640px)" && desktopViewport,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => { viewportListeners.add(listener); },
    removeEventListener: (_type: string, listener: EventListenerOrEventListenerObject) => { viewportListeners.delete(listener); },
    dispatchEvent: () => false,
  } as MediaQueryList));
  vi.mocked(api.notificationCount).mockResolvedValue(page);
  vi.mocked(api.notificationPage).mockResolvedValue(page);
  vi.mocked(api.markNotification).mockResolvedValue({});
  vi.mocked(api.markNotificationSnapshot).mockResolvedValue({});
});
afterEach(() => { cleanup(); viewportListeners.clear(); vi.restoreAllMocks(); vi.clearAllMocks(); });

describe("personal notifications", () => {
  it.each([0, 1, 137])("uses a quiet unread dot, retaining the exact count in the bell name (%s)", async count => {
    vi.mocked(api.notificationCount).mockResolvedValue({ ...page, unread_count: count });
    mount(<NotificationBell />);
    const bell = await screen.findByRole("button", { name: `Notifications, ${count} unread` });
    expect(bell).toHaveTextContent("");
    expect(bell.querySelector("svg")).toHaveClass("lucide-bell", "h-4", "w-4", "stroke-[1.7]");
    const dot = bell.querySelector("[data-notification-indicator]");
    if (count) expect(dot).toHaveAttribute("aria-hidden", "true");
    else expect(dot).toBeNull();
  });
  it("shows one full-inbox heading without an extra Inbox title", async () => {
    mount(<NotificationsPage />);
    await screen.findByText(item.title);
    expect(screen.getAllByRole("heading", { name: "Notifications" })).toHaveLength(1);
    expect(screen.queryByRole("heading", { name: "Inbox" })).not.toBeInTheDocument();
  });
  it.each([true, false])("opens a nonmodal popover and preserves the first outside action (desktop=%s)", async desktop => {
    setDesktopViewport(desktop);
    const outsideAction = vi.fn();
    const actor = userEvent.setup();
    mount(<><NotificationBell /><button onClick={outsideAction}>Continue working</button></>);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    const panel = await screen.findByTestId("notification-panel");
    expect(panel).toHaveAttribute("data-slot", "popover-content");
    expect(screen.getAllByRole("heading", { name: "Notifications" })).toHaveLength(1);
    expect(document.querySelector('[data-slot="dialog-overlay"]')).not.toBeInTheDocument();
    expect(document.body).not.toHaveAttribute("data-scroll-locked");
    expect(document.body.style.pointerEvents).not.toBe("none");
    expect(document.documentElement).not.toHaveClass("notification-mobile-open");
    await actor.click(screen.getByRole("button", { name: "Continue working" }));
    expect(outsideAction).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByTestId("notification-panel")).not.toBeInTheDocument());
  });
  it.each([true, false])("focuses the selected state tab and closes on the first Escape (desktop=%s)", async desktop => {
    setDesktopViewport(desktop);
    const actor = userEvent.setup();
    mount(<NotificationBell />);
    const bell = await screen.findByRole("button", { name: "Notifications, 1 unread" });
    await actor.click(bell);
    await waitFor(() => expect(screen.getByRole("tab", { name: "All" })).toHaveFocus());
    await actor.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("notification-panel")).not.toBeInTheDocument());
    await waitFor(() => expect(bell).toHaveFocus());
    await actor.click(bell);
    await actor.click(screen.getByRole("tab", { name: "Unread" }));
    await waitFor(() => expect(api.notificationPage).toHaveBeenCalledWith("unread", undefined, expect.any(AbortSignal), "all"));
    await actor.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("notification-panel")).not.toBeInTheDocument());
    await waitFor(() => expect(bell).toHaveFocus());
    await actor.click(bell);
    await waitFor(() => expect(screen.getByRole("tab", { name: "Unread" })).toHaveFocus());
    expect(screen.getByRole("tab", { name: "Unread" })).toHaveAttribute("aria-selected", "true");
    await actor.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("notification-panel")).not.toBeInTheDocument());
    await waitFor(() => expect(bell).toHaveFocus());
  });
  it("keeps notification anchors and descriptions unique when the page and desktop popover coexist", async () => {
    const actor = userEvent.setup();
    mount(<><NotificationBell /><NotificationsPage /></>);
    await screen.findByRole("link", { name: item.title });
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    const destinations = screen.getAllByRole("link", { name: item.title });
    expect(destinations).toHaveLength(2);
    expect(new Set(destinations.map(link => link.id)).size).toBe(2);
    expect(document.getElementById(`notification-${item.id}`)).toBe(destinations[0]);
    const descriptions = destinations.map(link => link.getAttribute("aria-describedby"));
    expect(new Set(descriptions).size).toBe(2);
    destinations.forEach(link => expect(link).toHaveAccessibleDescription("Unread. Updated · demo. View document"));
  });
  it.each(["close", "unmount"])("never applies a mobile scroll lock before or after %s", async finish => {
    setDesktopViewport(false);
    const actor = userEvent.setup();
    const view = mount(<NotificationBell />);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    expect(await screen.findByRole("dialog", { name: "Notifications" })).toHaveAttribute("data-testid", "notification-panel");
    expect(screen.getByTestId("notification-panel")).toHaveAttribute("data-slot", "popover-content");
    expect(document.querySelector('[data-slot="dialog-overlay"]')).not.toBeInTheDocument();
    expect(document.documentElement).not.toHaveClass("notification-mobile-open");
    expect(document.body).not.toHaveAttribute("data-scroll-locked");
    if (finish === "close") await actor.click(screen.getByRole("button", { name: "Close notifications" }));
    else view.unmount();
    await waitFor(() => expect(document.documentElement).not.toHaveClass("notification-mobile-open"));
    expect(screen.queryByTestId("notification-panel")).not.toBeInTheDocument();
  });
  it("keeps the same popover and unread state through viewport changes", async () => {
    setDesktopViewport(false);
    const actor = userEvent.setup();
    mount(<NotificationBell />);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    const panel = screen.getByTestId("notification-panel");
    expect(screen.getByRole("tablist", { name: "Notification state" })).toHaveAttribute("aria-orientation", "horizontal");
    expect(screen.queryByRole("tab", { name: "Documents" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Vault access" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Unread only" })).not.toBeInTheDocument();
    await actor.click(screen.getByRole("tab", { name: "Unread" }));
    await waitFor(() => expect(api.notificationPage).toHaveBeenCalledWith("unread", undefined, expect.any(AbortSignal), "all"));
    act(() => setDesktopViewport(true));
    expect(screen.getByTestId("notification-panel")).toBe(panel);
    expect(screen.getByRole("tab", { name: "Unread" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tablist", { name: "Notification state" })).toHaveAttribute("aria-orientation", "horizontal");
    expect(document.documentElement).not.toHaveClass("notification-mobile-open");
    expect(document.querySelector('[data-slot="dialog-overlay"]')).not.toBeInTheDocument();
    expect(document.body).not.toHaveAttribute("data-scroll-locked");
  });
  it("retains an in-flight direct row mutation and its failure feedback through viewport changes", async () => {
    let fail!: (error: Error) => void;
    vi.mocked(api.markNotification).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    const actor = userEvent.setup();
    mount(<NotificationBell />);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    const read = await screen.findByRole("button", { name: `Mark read: ${item.title}` });
    await actor.click(read);
    expect(read).toBeEnabled();
    expect(read).toHaveAttribute("aria-busy", "true");
    expect(read).toHaveAttribute("aria-disabled", "true");
    await actor.click(read);
    act(() => setDesktopViewport(false));
    expect(screen.getByRole("button", { name: `Mark read: ${item.title}` })).toBe(read);
    expect(read).toBeEnabled();
    expect(read).toHaveAttribute("aria-busy", "true");
    expect(api.markNotification).toHaveBeenCalledTimes(1);
    await act(async () => fail(new Error("Read state unavailable")));
    expect(await screen.findByText("Read state unavailable")).toBeVisible();
    expect(read).toBeEnabled();
    expect(read).not.toHaveAttribute("aria-disabled", "true");
    expect(api.markNotification).toHaveBeenCalledWith(item, true);
  });
  it("provides a separate direct Mark unread action for a read compact row", async () => {
    const readItem = { ...item, read: true };
    vi.mocked(api.notificationPage).mockResolvedValue({ ...page, items: [readItem] });
    const actor = userEvent.setup();
    mount(<NotificationBell />);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    const markUnread = await screen.findByRole("button", { name: `Mark unread: ${item.title}` });
    expect(screen.getByRole("link", { name: item.title })).not.toContainElement(markUnread);
    expect(screen.queryByRole("button", { name: `Actions for notification: ${item.title}` })).not.toBeInTheDocument();
    await actor.click(markUnread);
    expect(api.markNotification).toHaveBeenCalledWith(readItem, false);
  });
  it("moves focus to the next read action or the Unread tab when a filtered row disappears", async () => {
    const nextItem = { ...item, id: "n2", title: "Another watched document" };
    const readIds = new Set<string>();
    vi.mocked(api.notificationPage).mockImplementation(async state => ({
      ...page,
      unread_count: 2 - readIds.size,
      items: [item, nextItem].filter(notification => state !== "unread" || !readIds.has(notification.id)).map(notification => ({ ...notification, read: readIds.has(notification.id) })),
    }));
    vi.mocked(api.markNotification).mockImplementation(async notification => { readIds.add(notification.id); return {}; });
    const actor = userEvent.setup();
    mount(<NotificationBell />);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    await actor.click(screen.getByRole("tab", { name: "Unread" }));
    const firstAction = await screen.findByRole("button", { name: `Mark read: ${item.title}` });
    firstAction.focus();
    await actor.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByRole("button", { name: `Mark read: ${item.title}` })).not.toBeInTheDocument());
    const remainingAction = screen.getByRole("button", { name: `Mark read: ${nextItem.title}` });
    await waitFor(() => expect(remainingAction).toHaveFocus());
    await actor.keyboard("{Enter}");
    await screen.findByText("You’re all caught up");
    await waitFor(() => expect(screen.getByRole("tab", { name: "Unread" })).toHaveFocus());
    expect(api.markNotification).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("notification-panel")).toBeVisible();
  });
  it.each(["row", "all"])("returns keyboard focus to the %s action trigger while its write is pending", async scope => {
    let finish!: (value: object) => void;
    const pending = new Promise<object>(resolve => { finish = resolve; });
    const write = scope === "row" ? vi.mocked(api.markNotification) : vi.mocked(api.markNotificationSnapshot);
    write.mockReturnValueOnce(pending);
    const actor = userEvent.setup();
    mount(<NotificationsPage />);
    const triggerName = scope === "row" ? `Actions for notification: ${item.title}` : "Notification actions";
    const actionName = scope === "row" ? "Mark read" : "Mark all notifications read";
    const trigger = await screen.findByRole("button", { name: triggerName });
    trigger.focus();
    await actor.keyboard("{Enter}");
    const action = await screen.findByRole("menuitem", { name: actionName });
    await waitFor(() => expect(action).toHaveFocus());
    await actor.keyboard("{Enter}");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(trigger).toBeEnabled();
    expect(trigger).toHaveAttribute("aria-busy", "true");
    await actor.keyboard("{Enter}");
    const blockedAction = await screen.findByRole("menuitem", { name: actionName });
    expect(blockedAction).toHaveAttribute("aria-disabled", "true");
    await actor.click(blockedAction);
    expect(write).toHaveBeenCalledTimes(1);
    await actor.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    await act(async () => finish({}));
    await waitFor(() => expect(trigger).not.toHaveAttribute("aria-busy", "true"));
    expect(trigger).toHaveFocus();
  });
  it("keeps the compact direct mark-all action focusable while preventing repeated pending writes", async () => {
    let finish!: (value: object) => void;
    vi.mocked(api.markNotificationSnapshot).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const actor = userEvent.setup();
    mount(<NotificationBell />);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    await screen.findByText(item.title);
    const markAll = screen.getByRole("button", { name: "Mark all notifications read" });
    expect(markAll).toHaveAccessibleName("Mark all notifications read");
    expect(markAll).not.toHaveAttribute("aria-disabled", "true");
    markAll.focus();
    await actor.keyboard("{Enter}");
    expect(api.markNotificationSnapshot).toHaveBeenCalledWith(page.snapshot);
    expect(markAll).toBeEnabled();
    expect(markAll).toHaveAttribute("aria-disabled", "true");
    expect(markAll).toHaveAttribute("aria-busy", "true");
    expect(markAll).toHaveFocus();
    await actor.keyboard("{Enter} ");
    await actor.click(markAll);
    expect(api.markNotificationSnapshot).toHaveBeenCalledTimes(1);
    vi.mocked(api.notificationPage).mockResolvedValue({ ...page, unread_count: 0, items: [{ ...item, read: true }] });
    await act(async () => finish({}));
    await waitFor(() => expect(markAll).not.toHaveAttribute("aria-busy", "true"));
    expect(markAll).toHaveAttribute("aria-disabled", "true");
    expect(markAll).toHaveFocus();
    await actor.keyboard("{Enter}");
    expect(api.markNotificationSnapshot).toHaveBeenCalledTimes(1);
  });
  it.each([true, false])("keeps the zero-unread direct mark-all action focusable without writing (desktop=%s)", async desktop => {
    setDesktopViewport(desktop);
    const emptyUnread = { ...page, unread_count: 0, items: [{ ...item, read: true }] };
    vi.mocked(api.notificationCount).mockResolvedValue(emptyUnread);
    vi.mocked(api.notificationPage).mockResolvedValue(emptyUnread);
    const actor = userEvent.setup();
    mount(<NotificationBell />);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 0 unread" }));
    await screen.findByText(item.title);
    const markAll = screen.getByRole("button", { name: "Mark all notifications read" });
    expect(markAll).toBeEnabled();
    expect(markAll).toHaveAttribute("aria-disabled", "true");
    markAll.focus();
    await actor.keyboard("{Enter} ");
    await actor.click(markAll);
    expect(markAll).toHaveFocus();
    expect(api.markNotificationSnapshot).not.toHaveBeenCalled();
  });
  it.each(["pending", "failed"])("does not describe an unknown unread count as zero while the inbox is %s", async state => {
    let finish: ((value: api.NotificationPage) => void) | undefined;
    if (state === "pending") vi.mocked(api.notificationPage).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    else vi.mocked(api.notificationPage).mockRejectedValueOnce(new Error("Offline"));
    const actor = userEvent.setup();
    mount(<NotificationBell />);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    if (state === "failed") await screen.findByRole("button", { name: "Retry" });
    const markAll = screen.getByRole("button", { name: "Mark all notifications read" });
    expect(markAll).toBeEnabled();
    expect(markAll).toHaveAttribute("aria-disabled", "true");
    act(() => markAll.focus());
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent(state === "pending" ? "Loading notifications" : "Unread count unavailable");
    expect(tooltip).not.toHaveTextContent("No unread notifications");
    await actor.keyboard("{Enter}");
    expect(api.markNotificationSnapshot).not.toHaveBeenCalled();
    if (finish) await act(async () => finish!(page));
  });
  it("opens Watched documents from the compact footer after closing its notification surface", async () => {
    const actor = userEvent.setup();
    mount(<><NotificationBell /><Destination /></>);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    await actor.click(screen.getByRole("button", { name: "Watched documents" }));
    await waitFor(() => expect(screen.getByTestId("notification-destination")).toHaveTextContent("/settings"));
    expect(screen.queryByTestId("notification-panel")).not.toBeInTheDocument();
  });
  it("combines category tabs and unread filtering in server requests", async () => {
    vi.mocked(api.notificationPage).mockImplementation(async (state, _cursor, _signal, category) => ({
      ...page, category, items: category === "access" ? [] : [{ ...item, title: state === "unread" ? "Unread document" : item.title }],
    }));
    const actor = userEvent.setup(); mount(<NotificationsPage />);
    await screen.findByText(item.title);
    await actor.click(screen.getByRole("tab", { name: "Documents" }));
    await waitFor(() => expect(api.notificationPage).toHaveBeenCalledWith("all", undefined, expect.any(AbortSignal), "documents"));
    await actor.click(screen.getByRole("checkbox", { name: "Unread only" }));
    await screen.findByText("Unread document");
    expect(api.notificationPage).toHaveBeenCalledWith("unread", undefined, expect.any(AbortSignal), "documents");
    await actor.click(screen.getByRole("tab", { name: "Vault access" }));
    await screen.findByText("You’re all caught up");
    expect(screen.queryByText("Unread document")).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Unread only" })).toBeChecked();
    await actor.click(screen.getByRole("button", { name: "Include read notifications" }));
    await screen.findByText("No vault access updates yet");
    expect(api.notificationPage).toHaveBeenCalledWith("all", undefined, expect.any(AbortSignal), "access");
  });
  it("keeps the compact unread state and all categories in pagination and full-inbox navigation", async () => {
    vi.mocked(api.notificationPage).mockImplementation(async (_state, cursor, _signal, category) => ({
      ...page, category, next_cursor: cursor ? null : "older", items: [{ ...item, id: cursor ? "n2" : "n1", title: cursor ? "Older document" : item.title }],
    }));
    const actor = userEvent.setup(); mount(<NotificationBell />);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    await actor.click(screen.getByRole("tab", { name: "Unread" }));
    await actor.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByText("Older document");
    expect(api.notificationPage).toHaveBeenCalledWith("unread", "older", expect.any(AbortSignal), "all");
    expect(screen.getByRole("link", { name: "View all notifications" })).toHaveAttribute("href", "/notifications?category=all&state=unread");
    expect(screen.getByRole("button", { name: "Mark all notifications read" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Watched documents" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Notification actions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Notification settings" })).not.toBeInTheDocument();
  });
  it("offers recovery instead of an empty category on older servers", async () => {
    vi.mocked(api.notificationPage).mockImplementation(async (_state, _cursor, _signal, category) => {
      if (category !== "all") throw new api.NotificationCategoriesUnavailable("Category filters are not available on this server yet.");
      return page;
    });
    const actor = userEvent.setup(); mount(<NotificationsPage />);
    await screen.findByText(item.title);
    await actor.click(screen.getByRole("tab", { name: "Documents" }));
    await screen.findByText("Category filters are not available on this server yet.");
    expect(screen.queryByText("No document updates yet")).not.toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: "Show all categories" }));
    await screen.findByText(item.title);
    expect(screen.getByRole("tab", { name: "All" })).toHaveAttribute("aria-selected", "true");
  });
  it("uses short change labels and keeps read actions separate from the destination", async () => {
    const actor = userEvent.setup();
    mount(<NotificationsPage />);
    const destination = await screen.findByRole("link", { name: item.title });
    expect(screen.getByText("Updated")).toBeVisible();
    expect(screen.queryByText(item.message)).not.toBeInTheDocument();
    expect(destination).toHaveAccessibleDescription("Unread. Updated · demo. View document");
    const actions = screen.getByRole("button", { name: `Actions for notification: ${item.title}` });
    expect(destination).not.toContainElement(actions);
    expect(screen.queryByRole("button", { name: "Mark read" })).not.toBeInTheDocument();
    await actor.click(actions);
    await actor.click(screen.getByRole("menuitem", { name: "Mark read" }));
    expect(api.markNotification).toHaveBeenCalledWith(item, true);
  });
  it("does not repeat a Vault name for an access notice", async () => {
    vi.mocked(api.notificationPage).mockResolvedValue({ ...page, items: [{ ...item, kind: "access.changed", title: "demo", message: "Your vault access changed", target: { uri: "akb://demo", vault: "demo" } }] });
    mount(<NotificationsPage />);
    await screen.findByText("Access changed");
    expect(screen.getAllByText("demo")).toHaveLength(1);
  });
  it("offers Mark unread only in the read notification's separate action menu", async () => {
    const readItem = { ...item, read: true };
    vi.mocked(api.notificationPage).mockResolvedValue({ ...page, items: [readItem] });
    const actor = userEvent.setup();
    mount(<NotificationsPage />);
    await openNotificationActions(actor);
    expect(screen.queryByRole("menuitem", { name: "Mark read" })).not.toBeInTheDocument();
    expect(document.body.style.pointerEvents).not.toBe("none");
    await actor.click(screen.getByRole("menuitem", { name: "Mark unread" }));
    expect(api.markNotification).toHaveBeenCalledWith(readItem, false);
  });
  it.each(["document", "vault"])("F26: opens a valid %s even when marking read fails", async kind => {
    vi.mocked(api.notificationPage).mockResolvedValue({ ...page, items: [{ ...item, target: { uri: kind === "vault" ? "akb://demo" : item.target!.uri, vault: "demo" } }] });
    let fail!: (error: Error) => void;
    vi.mocked(api.markNotification).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    const actor = userEvent.setup(); mount(<InboxRoutes />, "/notifications", 2);
    await actor.click(await screen.findByRole("link", { name: item.title }));
    expect(screen.getByTestId("notification-destination")).toHaveTextContent(kind === "vault" ? "/vault/demo" : "/vault/demo/doc/notes.md");
    expect(screen.queryByRole("heading", { name: "Notifications" })).not.toBeInTheDocument();
    // Failure arrives after the route really unmounted its Inbox.
    await act(async () => fail(new Error("Read state unavailable")));
    expect(await screen.findByRole("button", { name: "Retry marking read" })).toBeVisible();
    expect(api.markNotification).toHaveBeenCalledTimes(1);
    await actor.click(screen.getByRole("button", { name: "Retry marking read" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Retry marking read" })).not.toBeInTheDocument());
    expect(api.markNotification).toHaveBeenCalledTimes(2);
  });
  it.each(["initial", "retry"])("F26: %s 409 requires review, never replaying an unseen newer version", async phase => {
    const original = { ...item, kind: "access.changed", target: { uri: "akb://demo", vault: "demo" } };
    const current = { ...original, version: "v2", title: "Your access was removed", target: null };
    vi.mocked(api.notificationPage).mockResolvedValueOnce({ ...page, items: [original] }).mockResolvedValue({ ...page, items: [current] });
    if (phase === "retry") vi.mocked(api.markNotification).mockRejectedValueOnce(new Error("503"));
    vi.mocked(api.markNotification).mockRejectedValueOnce(new api.NotificationConflict("Changed")).mockResolvedValue({});
    const actor = userEvent.setup(); mount(<InboxRoutes />, "/notifications");
    await actor.click(await screen.findByRole("link", { name: original.title }));
    await screen.findByTestId("notification-destination");
    if (phase === "retry") await actor.click(await screen.findByRole("button", { name: "Retry marking read" }));
    const review = await screen.findByRole("button", { name: "Review current notification" });
    expect(screen.queryByRole("button", { name: "Retry marking read" })).not.toBeInTheDocument();
    const attempts = phase === "retry" ? 2 : 1;
    expect(api.markNotification).toHaveBeenCalledTimes(attempts);
    expect(vi.mocked(api.markNotification).mock.calls.every(([notification]) => notification.version === "v1")).toBe(true);
    await actor.click(review);
    await screen.findByText(current.title);
    expect(screen.queryByRole("link", { name: current.title })).not.toBeInTheDocument();
    expect(api.markNotification).toHaveBeenCalledTimes(attempts);
    // Reading the new notice and its separate explicit action owns v2.
    await actor.click(screen.getByRole("button", { name: `Mark read: ${current.title}` }));
    expect(api.markNotification).toHaveBeenLastCalledWith(current, true);
    expect(api.markNotification).toHaveBeenCalledTimes(attempts + 1);
  });
  it("F26: a failed review refresh retains review-only recovery, not stale version Retry", async () => {
    const original = { ...item, target: { uri: "akb://demo", vault: "demo" } };
    vi.mocked(api.notificationPage).mockResolvedValueOnce({ ...page, items: [original] }).mockRejectedValue(new Error("Offline"));
    vi.mocked(api.markNotification).mockRejectedValue(new api.NotificationConflict("Changed"));
    const actor = userEvent.setup(); mount(<InboxRoutes />, "/notifications");
    await actor.click(await screen.findByRole("link", { name: item.title }));
    await actor.click(await screen.findByRole("button", { name: "Review current notification" }));
    expect(await screen.findByText(/Could not refresh the current notification/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Review current notification" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Retry marking read" })).not.toBeInTheDocument();
    expect(api.markNotification).toHaveBeenCalledTimes(1);
  });
  it("F26: reports a conflict immediately even if count revalidation is still pending", async () => {
    let finishCount!: (value: api.NotificationSummary) => void;
    vi.mocked(api.notificationCount).mockResolvedValueOnce(page).mockImplementationOnce(() => new Promise(resolve => { finishCount = resolve; }));
    vi.mocked(api.notificationPage).mockResolvedValue({ ...page, items: [{ ...item, target: { uri: "akb://demo", vault: "demo" } }] });
    vi.mocked(api.markNotification).mockRejectedValue(new api.NotificationConflict("Changed"));
    const actor = userEvent.setup(); mount(<InboxRoutes />, "/notifications");
    await screen.findByRole("button", { name: "Notifications, 1 unread" });
    await actor.click(await screen.findByRole("link", { name: item.title }));
    expect(await screen.findByRole("button", { name: "Review current notification" })).toBeVisible();
    await waitFor(() => expect(api.notificationCount).toHaveBeenCalledTimes(2));
    await act(async () => finishCount(page));
  });
  it("F26: ignores a former account's late read failure after route navigation", async () => {
    let fail!: (error: Error) => void;
    vi.mocked(api.markNotification).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (userId: string) => <QueryClientProvider client={client}><CurrentUserProvider user={{ ...user, user_id: userId }}><MemoryRouter initialEntries={["/notifications"]}><InboxRoutes /></MemoryRouter></CurrentUserProvider></QueryClientProvider>;
    const view = render(tree("user-a")); const actor = userEvent.setup();
    await actor.click(await screen.findByRole("link", { name: item.title }));
    await screen.findByTestId("notification-destination");
    view.rerender(tree("user-b"));
    await act(async () => fail(new Error("503")));
    expect(screen.queryByRole("button", { name: "Retry marking read" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review current notification" })).not.toBeInTheDocument();
  });
  it("F26: a late review response cannot reopen the Bell after access ownership changes", async () => {
    let finish!: (value: api.NotificationPage) => void;
    vi.mocked(api.notificationPage).mockResolvedValueOnce({ ...page, items: [{ ...item, target: { uri: "akb://demo", vault: "demo" } }] })
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })).mockResolvedValue(page);
    vi.mocked(api.markNotification).mockRejectedValue(new api.NotificationConflict("Changed"));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (revision: number) => <QueryClientProvider client={client}><CurrentUserProvider user={user} revision={revision}><MemoryRouter initialEntries={["/notifications"]}><InboxRoutes /></MemoryRouter></CurrentUserProvider></QueryClientProvider>;
    const view = render(tree(0)); const actor = userEvent.setup();
    await actor.click(await screen.findByRole("link", { name: item.title }));
    await actor.click(await screen.findByRole("button", { name: "Review current notification" }));
    await waitFor(() => expect(api.notificationPage).toHaveBeenCalledTimes(2));
    view.rerender(tree(1));
    await act(async () => finish({ ...page, items: [{ ...item, title: "Old proof response", version: "v2" }] }));
    expect(screen.queryByTestId("notification-panel")).not.toBeInTheDocument();
    expect(screen.queryByText("Old proof response")).not.toBeInTheDocument();
    expect(api.markNotification).toHaveBeenCalledTimes(1);
  });
  it("F26: keeps compact navigation failure feedback and offers a separate read retry", async () => {
    vi.mocked(api.markNotification).mockRejectedValueOnce(new Error("Read state unavailable")).mockResolvedValueOnce({});
    const actor = userEvent.setup(); mount(<><NotificationBell /><Destination /></>);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    await actor.click(await screen.findByRole("link", { name: item.title }));
    await waitFor(() => expect(screen.getByTestId("notification-destination")).toHaveTextContent("/vault/demo/doc/notes.md"));
    await actor.click(await screen.findByRole("button", { name: "Retry marking read" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Retry marking read" })).not.toBeInTheDocument());
    expect(api.markNotification).toHaveBeenCalledTimes(2);
  });
  it("F26: collapses repeated activation while read acknowledgement is pending", async () => {
    let finish!: (value: object) => void;
    vi.mocked(api.markNotification).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const actor = userEvent.setup(); mount(<><NotificationsPage /><Destination /></>);
    const link = await screen.findByRole("link", { name: item.title });
    await actor.click(link); await actor.click(link);
    expect(api.markNotification).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("notification-destination")).toHaveTextContent("/vault/demo/doc/notes.md");
    await act(async () => finish({}));
  });
  it("F26: defers the nonmodal retry until the destination reader is closed", async () => {
    vi.mocked(api.markNotification).mockRejectedValue(new Error("Read state unavailable"));
    const actor = userEvent.setup(); mount(<><NotificationBell /><Destination /></>);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    await actor.click(await screen.findByRole("link", { name: item.title }));
    await screen.findByRole("button", { name: "Retry marking read" });
    let close!: () => void;
    act(() => { close = registerModal(); });
    expect(screen.queryByRole("button", { name: "Retry marking read" })).not.toBeInTheDocument();
    act(() => close());
    expect(await screen.findByRole("button", { name: "Retry marking read" })).toBeVisible();
  });
  it("F26: notice-only events have no destination and modified clicks retain native links", async () => {
    vi.mocked(api.notificationPage).mockResolvedValue({ ...page, items: [{ ...item, id: "notice", title: "Access removed", target: null }, item] });
    mount(<NotificationsPage />);
    await screen.findByText("Access removed");
    expect(screen.queryByRole("link", { name: "Access removed" })).not.toBeInTheDocument();
    const link = screen.getByRole("link", { name: item.title });
    expect(link).toHaveAttribute("href", "/vault/demo/doc/notes.md");
    link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true }));
    expect(api.markNotification).not.toHaveBeenCalled();
  });
  it("retains explanatory text for event kinds from a newer backend", async () => {
    vi.mocked(api.notificationPage).mockResolvedValue({ ...page, items: [{ ...item, kind: "future.event", message: "A new kind of update" }] });
    mount(<NotificationsPage />);
    expect(await screen.findByText("A new kind of update")).toBeVisible();
  });
  it("renders a safe notice once when its title and message match", async () => {
    vi.mocked(api.notificationPage).mockResolvedValue({ ...page, items: [{ ...item, title: "Your access was removed", message: "Your access was removed", target: null }] });
    mount(<NotificationsPage />);
    await screen.findByText("Your access was removed");
    expect(screen.getAllByText("Your access was removed")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Your access was removed" })).not.toBeInTheDocument();
    expect(screen.getByText("Notice only", { exact: true })).toBeVisible();
    const actor = userEvent.setup();
    await openNotificationActions(actor, "Your access was removed");
    expect(screen.getByRole("menuitem", { name: "Mark read" })).toBeEnabled();
  });
  it("opens without marking read and sends the displayed snapshot for mark all", async () => {
    const actor = userEvent.setup(); mount(<NotificationBell />);
    await actor.click(await screen.findByRole("button", { name: "Notifications, 1 unread" }));
    await screen.findByText("Release notes updated");
    expect(api.markNotification).not.toHaveBeenCalled();
    expect(api.markNotificationSnapshot).not.toHaveBeenCalled();
    await actor.click(screen.getByRole("button", { name: "Mark all notifications read" }));
    await waitFor(() => expect(api.markNotificationSnapshot).toHaveBeenCalledWith("snapshot-before-arrival"));
    await actor.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: "Notifications, 1 unread" })).toHaveFocus());
  });
  it("acknowledges only the observed version and paginates with the server cursor", async () => {
    vi.mocked(api.notificationPage).mockImplementation(async (_state, cursor) => cursor
      ? { ...page, items: [{ ...item, id: "n2", title: "Older update" }] }
      : { ...page, next_cursor: "cursor-two" });
    const actor = userEvent.setup(); mount(<NotificationsPage />);
    await markNotificationRead(actor);
    expect(api.markNotification).toHaveBeenCalledWith(item, true);
    await actor.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByText("Older update");
    expect(api.notificationPage).toHaveBeenCalledWith("all", "cursor-two", expect.any(AbortSignal), "all");
  });
  it("distinguishes unsupported from an empty inbox and keeps unknown count honest", async () => {
    vi.mocked(api.notificationCount).mockRejectedValue(new api.NotificationsUnavailable("Notifications unavailable"));
    vi.mocked(api.notificationPage).mockRejectedValue(new api.NotificationsUnavailable("Notifications unavailable"));
    const actor = userEvent.setup(); mount(<NotificationBell />);
    await actor.click(screen.getByRole("button", { name: "Notifications, unread count unavailable" }));
    expect(document.querySelector("#notifications-trigger [data-notification-indicator]")).toBeNull();
    await screen.findByText("Notifications unavailable");
    expect(screen.queryByText("No notifications yet")).not.toBeInTheDocument();
  });
  it("shows mutation failure inline and leaves state unchanged", async () => {
    vi.mocked(api.markNotification).mockRejectedValue(new api.NotificationConflict("This notification changed. Review its current update before marking it read."));
    const actor = userEvent.setup(); mount(<NotificationsPage />);
    await markNotificationRead(actor);
    await screen.findByText("This notification changed. Review its current update before marking it read.");
    await waitFor(() => expect(api.notificationPage).toHaveBeenCalledTimes(2));
    await openNotificationActions(actor);
    expect(screen.getByRole("menuitem", { name: "Mark read" })).toBeEnabled();
  });
  it("watches explicitly and keeps the URI in the subscription request", async () => {
    vi.mocked(api.documentSubscription).mockImplementation(async (_uri, method) => ({ resource_id: "doc-uuid", subscribed: method === "PUT" }));
    const actor = userEvent.setup(); mount(<DocumentWatch uri="akb://demo/doc/notes.md" />);
    const watch = await screen.findByRole("button", { name: "Watch" });
    await waitFor(() => expect(watch).toBeEnabled());
    await actor.click(watch);
    await screen.findByRole("button", { name: "Unwatch" });
    expect(api.documentSubscription).toHaveBeenCalledWith("akb://demo/doc/notes.md", "PUT");
  });
  it("cancels the former user's request and ignores its late count", async () => {
    let finishFirst!: (value: api.NotificationSummary) => void;
    let firstSignal: AbortSignal | undefined;
    vi.mocked(api.notificationCount)
      .mockImplementationOnce(signal => { firstSignal = signal; return new Promise(resolve => { finishFirst = resolve; }); })
      .mockResolvedValue({ ...page, unread_count: 2 });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (userId: string) => <QueryClientProvider client={client}><CurrentUserProvider user={{ ...user, user_id: userId }}><MemoryRouter><NotificationBell /></MemoryRouter></CurrentUserProvider></QueryClientProvider>;
    const view = render(tree("user-a"));
    await waitFor(() => expect(api.notificationCount).toHaveBeenCalledTimes(1));
    view.rerender(tree("user-b"));
    await screen.findByRole("button", { name: "Notifications, 2 unread" });
    expect(firstSignal?.aborted).toBe(true);
    finishFirst({ ...page, unread_count: 99 });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Notifications, 99 unread" })).not.toBeInTheDocument());
  });
});
