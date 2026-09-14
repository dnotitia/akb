import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { PreferencesSection } from "@/pages/settings/preferences-section";
import { NotificationsSection } from "@/pages/settings/notifications-section";
import * as api from "@/lib/api-notifications";

vi.mock("@/lib/api-notifications", async original => ({
  ...await original<typeof import("@/lib/api-notifications")>(),
  notificationSubscriptions: vi.fn(),
  documentSubscription: vi.fn(),
}));

const account = { user_id: "user-a", username: "reader", email: "reader@example.invalid", display_name: "Reader", is_admin: false, auth_method: "local", key_class: null };
const watched = { resource_id: "doc-a", uri: "akb://demo/coll/guides/doc/start.md", title: "Getting started", vault: "demo" };

function mountWatched() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><CurrentUserProvider user={account}><MemoryRouter><NotificationsSection /></MemoryRouter></CurrentUserProvider></QueryClientProvider>);
}

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  vi.mocked(api.notificationSubscriptions).mockResolvedValue({ items: [watched] });
  vi.mocked(api.documentSubscription).mockResolvedValue({ subscribed: false, resource_id: watched.resource_id });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  document.documentElement.classList.remove("dark");
});

describe("Settings appearance", () => {
  it("provides three native radio options with immediate theme persistence and keyboard selection", async () => {
    const actor = userEvent.setup();
    render(<PreferencesSection />);
    expect(screen.getAllByRole("radio")).toHaveLength(3);
    expect(screen.getByRole("radio", { name: "System" })).toBeChecked();
    await actor.tab();
    expect(screen.getByRole("radio", { name: "System" })).toHaveFocus();
    await actor.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Light" })).toBeChecked();
    expect(localStorage.getItem("akb_theme")).toBe("light");
    await actor.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Dark" })).toBeChecked();
    expect(document.documentElement).toHaveClass("dark");
    await actor.click(screen.getByRole("radio", { name: "System" }));
    expect(localStorage.getItem("akb_theme")).toBeNull();
    expect(screen.queryByText("Interface preview")).not.toBeInTheDocument();
  });
});

describe("Settings watched documents", () => {
  it("links canonical document paths and removes a subscription without adding an Inbox entry", async () => {
    const actor = userEvent.setup();
    mountWatched();
    expect(await screen.findByRole("link", { name: watched.title })).toHaveAttribute("href", "/vault/demo/doc/guides%2Fstart.md");
    expect(screen.queryByRole("link", { name: /inbox/i })).not.toBeInTheDocument();
    vi.mocked(api.notificationSubscriptions).mockResolvedValue({ items: [] });
    await actor.click(screen.getByRole("button", { name: `Unwatch ${watched.title}` }));
    await waitFor(() => expect(api.documentSubscription).toHaveBeenCalledWith(watched.uri, "DELETE"));
    expect(await screen.findByText("You aren’t watching any documents yet.")).toBeVisible();
  });

  it("retains failed subscriptions and offers a dismissible error without pretending a refetch retries the deletion", async () => {
    const actor = userEvent.setup();
    vi.mocked(api.documentSubscription).mockRejectedValue(new Error("offline"));
    mountWatched();
    await actor.click(await screen.findByRole("button", { name: `Unwatch ${watched.title}` }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not stop watching");
    expect(screen.getByRole("link", { name: watched.title })).toBeVisible();
    await actor.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: `Unwatch ${watched.title}` })).toBeEnabled();
  });

  it("does not turn unavailable server support into an empty list", async () => {
    vi.mocked(api.notificationSubscriptions).mockRejectedValue(new api.NotificationsUnavailable("Notifications are not available on this server."));
    mountWatched();
    expect(await screen.findByText("Notifications are not available on this server.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();
    expect(screen.queryByText("You aren’t watching any documents yet.")).not.toBeInTheDocument();
  });

  it("keeps malformed or cross-vault URIs as non-navigable titles", async () => {
    vi.mocked(api.notificationSubscriptions).mockResolvedValue({ items: [{ ...watched, uri: "akb://other/doc/start.md" }] });
    mountWatched();
    expect(await screen.findByText(watched.title)).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
