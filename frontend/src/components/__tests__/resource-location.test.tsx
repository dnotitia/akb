import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { browseVault, type CurrentUser } from "@/lib/api";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceLocationProvider, usePublishResourceLocation, useResourceLocation, type ResourceLocation } from "@/contexts/resource-location-context";
import { ResourceBreadcrumb } from "../resource-breadcrumb";
import { ResourceCommandRow } from "../resource-command-row";
import { AppPageLocation } from "../app-page-location";
import { VaultActions } from "../title-bar";

vi.mock("@/lib/api", () => ({ browseVault: vi.fn() }));

const account: CurrentUser = {
  user_id: "reader", username: "reader", display_name: "Reader", email: "reader@example.com",
  is_admin: false, auth_method: "jwt", key_class: null,
};
const location: ResourceLocation = {
  vault: "팀 Vault", title: "Human document name", kind: "Document", collectionPath: "engineering/platform/guides",
};
const directory = {
  vault: "팀 Vault", path: "",
  items: [
    { type: "collection", path: "engineering", name: "Engineering" },
    { type: "collection", path: "engineering/platform", name: "Platform" },
    { type: "collection", path: "engineering/platform/guides", name: "Team guides" },
  ],
};

function QueryScope({ children, user = account, revision = 0, checking = false }: {
  children: ReactNode; user?: CurrentUser | null; revision?: number; checking?: boolean;
}) {
  return <CurrentUserProvider user={user} revision={revision} checking={checking}>{children}</CurrentUserProvider>;
}

function renderResource(children: ReactNode, entry = "/vault/%ED%8C%80%20Vault/doc/83ba46c4-cf01-4ccd-992b-90721da26996") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[entry]}><QueryScope>{children}</QueryScope></MemoryRouter></QueryClientProvider>);
}

function Publish({ value = location, enabled = true }: { value?: ResourceLocation | null; enabled?: boolean }) {
  usePublishResourceLocation(value, enabled);
  return null;
}

function ReadPublished() {
  const value = useResourceLocation();
  return <output data-testid="published">{value?.title ?? "No resource"}</output>;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(browseVault).mockResolvedValue(directory);
});

describe("Resource location", () => {
  it("uses one directory query for shared desktop/mobile ancestry and links to real collection destinations", async () => {
    renderResource(<><ResourceBreadcrumb location={location} /><ResourceBreadcrumb location={location} /></>);
    const trails = screen.getAllByRole("navigation", { name: "Resource location" });
    await waitFor(() => expect(within(trails[0]).getByRole("link", { name: "Team guides" })).toBeInTheDocument());
    expect(browseVault).toHaveBeenCalledTimes(1);
    expect(browseVault).toHaveBeenCalledWith("팀 Vault", undefined, -1);
    for (const trail of trails) {
      expect(trail.querySelector("ol")).toBeInTheDocument();
      expect(within(trail).getByRole("link", { name: "팀 Vault" })).toHaveAttribute("href", "/vault/%ED%8C%80%20Vault");
      expect(within(trail).queryByRole("button", { name: /Explore vault/ })).not.toBeInTheDocument();
      expect(within(trail).getByRole("link", { name: "Team guides" })).toHaveAttribute("href", "/vault/%ED%8C%80%20Vault?collection=engineering%2Fplatform%2Fguides");
      expect(within(trail).getByText("Human document name")).toHaveAttribute("aria-current", "page");
      expect(trail).not.toHaveTextContent("83ba46c4");
      expect(within(trail).queryByRole("link", { name: "Home" })).not.toBeInTheDocument();
    }
  });

  it("keeps ancestry keyboard-accessible while the current title is passive text", async () => {
    const user = userEvent.setup();
    const longTitle = "아주 긴 한국어 문서 제목 and its complete English explanation ".repeat(4);
    renderResource(<ResourceBreadcrumb location={{ ...location, title: longTitle }} />);
    await screen.findByRole("link", { name: "Team guides" });
    const ancestry = screen.getByRole("button", { name: "Show collection ancestry" });
    ancestry.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("menuitem", { name: "Engineering" })).toHaveAttribute("href", "/vault/%ED%8C%80%20Vault?collection=engineering");
    expect(screen.getByRole("menuitem", { name: "Platform" })).toHaveAttribute("href", "/vault/%ED%8C%80%20Vault?collection=engineering%2Fplatform");
    expect(screen.getByRole("menuitem", { name: "Team guides" })).toHaveAttribute("href", "/vault/%ED%8C%80%20Vault?collection=engineering%2Fplatform%2Fguides");
    await user.keyboard("{Escape}");
    expect(ancestry).toHaveFocus();
    const fullName = screen.getByText(longTitle.trim());
    expect(fullName.tagName).toBe("SPAN");
    expect(fullName).toHaveAttribute("aria-current", "page");
    expect(fullName).not.toHaveAttribute("aria-haspopup");
    expect(fullName).not.toHaveAttribute("aria-expanded");
    fullName.focus();
    await user.keyboard("{Enter}");
    await user.click(fullName);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("omits invented root ancestry and does not fetch the directory for a root resource", () => {
    renderResource(<ResourceBreadcrumb location={{ vault: "팀 Vault", title: "Root note", kind: "Document" }} />);
    expect(screen.getByRole("navigation", { name: "Resource location" })).not.toHaveTextContent("Root collection");
    expect(screen.queryByRole("button", { name: "Show collection ancestry" })).not.toBeInTheDocument();
    expect(browseVault).not.toHaveBeenCalled();
  });


  it("navigates through the Collection ancestry disclosure", async () => {
    const user = userEvent.setup();
    renderResource(<Routes>
      <Route path="/vault/:name/doc/:ref" element={<ResourceBreadcrumb location={location} />} />
      <Route path="/vault/:name" element={<p>Collection destination</p>} />
    </Routes>);
    await screen.findByRole("link", { name: "Team guides" });
    await user.click(screen.getByRole("button", { name: "Show collection ancestry" }));
    await user.click(screen.getByRole("menuitem", { name: "Platform" }));
    expect(screen.getByText("Collection destination")).toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("falls back to human path segments when the authorized directory is unavailable", async () => {
    vi.mocked(browseVault).mockRejectedValue(new Error("Unavailable"));
    renderResource(<ResourceBreadcrumb location={location} />);
    expect(screen.getByRole("link", { name: "guides" })).toBeInTheDocument();
    await waitFor(() => expect(browseVault).toHaveBeenCalledOnce());
  });

  it("uses new access proof for collection names and suppresses the old directory during checking", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const renderAt = (checking: boolean, revision: number) => <QueryClientProvider client={client}><MemoryRouter><QueryScope checking={checking} revision={revision}><ResourceBreadcrumb location={location} /></QueryScope></MemoryRouter></QueryClientProvider>;
    const view = render(renderAt(false, 0));
    await screen.findByRole("link", { name: "Team guides" });
    view.rerender(renderAt(true, 0));
    expect(screen.queryByRole("link", { name: "Team guides" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "guides" })).toBeInTheDocument();
    vi.mocked(browseVault).mockResolvedValue({ vault: "팀 Vault", path: "", items: [] });
    view.rerender(renderAt(false, 1));
    await waitFor(() => expect(browseVault).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("link", { name: "Team guides" })).not.toBeInTheDocument();
  });

  it("publishes canonical identity to the desktop trail and does not let a preview replace it", async () => {
    renderResource(<ResourceLocationProvider identity="reader"><AppPageLocation /><ReadPublished /><Publish /><Publish enabled={false} value={{ ...location, title: "Preview title" }} /></ResourceLocationProvider>);
    expect(screen.getByTestId("published")).toHaveTextContent("Human document name");
    expect(screen.getByRole("navigation", { name: "Resource location" })).toHaveTextContent("Human document name");
    expect(screen.queryByText("Preview title")).not.toBeInTheDocument();
  });

  it("clears resource metadata when navigating to an unresolved or denied resource", async () => {
    const user = userEvent.setup();
    renderResource(<ResourceLocationProvider identity="reader"><ReadPublished /><Routes>
      <Route path="/vault/:name/doc/:ref" element={<><Publish /><Link to="/vault/other/file/pending">Pending file</Link></>} />
      <Route path="/vault/other/file/pending" element={<Publish value={null} />} />
    </Routes></ResourceLocationProvider>);
    expect(screen.getByTestId("published")).toHaveTextContent("Human document name");
    await user.click(screen.getByRole("link", { name: "Pending file" }));
    expect(screen.getByTestId("published")).toHaveTextContent("No resource");
  });

  it("suppresses titles during access checks and clears them when identity changes without a loaded page", () => {
    const frame = (identity: string, checking: boolean, loaded: boolean) => <MemoryRouter><ResourceLocationProvider identity={identity} checking={checking}><ReadPublished />{loaded && <Publish />}</ResourceLocationProvider></MemoryRouter>;
    const view = render(frame("alice", false, true));
    expect(screen.getByTestId("published")).toHaveTextContent("Human document name");
    view.rerender(frame("alice", true, true));
    expect(screen.getByTestId("published")).toHaveTextContent("No resource");
    view.rerender(frame("bob", false, false));
    expect(screen.getByTestId("published")).toHaveTextContent("No resource");
  });
});

describe("Resource command row", () => {
  it("keeps metadata and resource actions without a competing Vault navigation menu", () => {
    renderResource(<ResourceCommandRow meta={<span>Read only</span>}><button>Info</button></ResourceCommandRow>);
    expect(screen.getByText("Read only")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Info" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Vault pages" })).not.toBeInTheDocument();
  });

  it("does not mark Overview current on resource or new-document routes", () => {
    renderResource(<VaultActions vault="demo" page="resource" />);
    expect(screen.getByRole("link", { name: "Overview" })).not.toHaveAttribute("aria-current");
  });

  it("keeps essential identity above optional metadata and actions", () => {
    renderResource(<ResourceLocationProvider identity="reader"><AppPageLocation /><Publish /><ResourceCommandRow meta={<span>Unsaved changes</span>}><button>Save changes</button></ResourceCommandRow></ResourceLocationProvider>);
    expect(screen.queryByRole("button", { name: "Vault pages" })).not.toBeInTheDocument();
    expect(screen.getByText("Unsaved changes")).toBeVisible();
    expect(screen.getByRole("navigation", { name: "Resource location" }).parentElement).toHaveAttribute("data-slot", "app-page-location");
    expect(document.querySelector('[data-slot="resource-command-row"]')).not.toContainElement(screen.getByRole("navigation", { name: "Resource location" }));
    expect(screen.getByRole("button", { name: "Save changes" })).toBeVisible();
  });
});
