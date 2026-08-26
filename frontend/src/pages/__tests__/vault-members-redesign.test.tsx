import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import VaultMembersPage from "@/pages/vault-members";

vi.mock("@/lib/api", () => ({
  getMe: vi.fn(),
  getVaultInfo: vi.fn(),
  getVaultMembers: vi.fn(),
  grantAccess: vi.fn(),
  revokeAccess: vi.fn(),
  searchUsers: vi.fn().mockResolvedValue({ users: [] }),
  transferOwnership: vi.fn(),
}));

import { getMe, getVaultInfo, getVaultMembers } from "@/lib/api";

const getMeMock = getMe as unknown as ReturnType<typeof vi.fn>;
const getVaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const getVaultMembersMock = getVaultMembers as unknown as ReturnType<
  typeof vi.fn
>;

const MEMBERS = [
  {
    username: "kwoo",
    display_name: "Vault Owner",
    email: "owner@example.com",
    role: "owner",
    since: "2026-03-14T00:00:00Z",
  },
  {
    username: "mina",
    display_name: "Mina Park",
    email: "mina@example.com",
    role: "writer",
    since: "2026-04-02T00:00:00Z",
  },
  {
    username: "dana",
    display_name: "Dana Lee",
    email: "dana@example.com",
    role: "reader",
    since: "2026-06-20T00:00:00Z",
  },
] as const;

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/vault/platform-docs/members"]}>
      <Routes>
        <Route path="/vault/:name/members" element={<VaultMembersPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getMeMock.mockResolvedValue({ username: "kwoo" });
  getVaultInfoMock.mockResolvedValue({
    name: "platform-docs",
    role: "owner",
    role_source: "member",
    public_access: "none",
  });
  getVaultMembersMock.mockResolvedValue({ members: MEMBERS });
});

afterEach(cleanup);

describe("Vault Members redesign", () => {
  it("presents the roster, current access context, and Settings connection", async () => {
    renderPage();

    const heading = await screen.findByRole("heading", {
      level: 1,
      name: "Members",
    });
    expect(heading).toBeInTheDocument();
    expect(heading.parentElement).toHaveClass(
      "xl:h-full",
      "xl:min-h-0",
      "xl:p-4",
      "2xl:p-5",
    );
    expect(heading.parentElement).not.toHaveClass("fade-up");
    expect(screen.getByTestId("members-workspace-frame")).toHaveClass(
      "xl:rounded-[var(--radius-md)]",
      "xl:border",
      "xl:overflow-hidden",
    );
    expect(heading.parentElement?.querySelector("main")).toHaveClass(
      "rail-scroll",
      "rail-scroll-auto",
    );
    const roster = screen.getByRole("region", { name: "Members" });
    expect(roster).toBeInTheDocument();
    expect(roster).not.toHaveClass("min-h-80", "xl:flex-1");
    expect(
      screen.getByRole("table", { name: "Vault members" }),
    ).toBeInTheDocument();
    const inspector = screen.getByRole("complementary", {
      name: "Member access context",
    });
    expect(inspector).toHaveClass("xl:border-l");
    expect(inspector).not.toHaveClass("self-start", "border-y");
    expect(inspector).not.toHaveClass("xl:h-full");
    expect(screen.getByTestId("member-access-panel")).toHaveClass(
      "border-b",
      "bg-surface",
    );
    expect(screen.getByTestId("member-roster-header")).toHaveClass(
      "bg-surface-2/55",
      "border-border-strong",
      "px-4",
      "lg:px-5",
    );
    expect(screen.getByRole("columnheader", { name: "Member" })).toHaveClass(
      "px-4",
      "lg:px-5",
    );
    expect(screen.getByTestId("member-access-header")).toHaveClass(
      "bg-surface-2/55",
      "border-border-strong",
    );
    expect(screen.getByTestId("role-ladder-header")).toHaveClass(
      "bg-surface-2/40",
    );
    expect(screen.getByTestId("public-access-header")).toHaveClass(
      "bg-surface-2/40",
    );
    const directAccessHeading = screen.getByRole("heading", {
      level: 2,
      name: "Direct access",
    });
    expect(directAccessHeading).toHaveClass("text-sm", "font-semibold");
    const accessSummary = directAccessHeading.parentElement;
    expect(accessSummary).not.toBeNull();
    expect(
      within(accessSummary as HTMLElement).getByText("3 members"),
    ).toBeInTheDocument();
    expect(screen.getByText("Vault Owner")).toBeInTheDocument();
    expect(screen.getByText("You")).toBeInTheDocument();

    const ladder = screen.getByRole("list", {
      name: "Roles from highest to lowest access",
    });
    expect(ladder).toHaveTextContent("Owner");
    expect(ladder).toHaveTextContent("Admin");
    expect(ladder).toHaveTextContent("Writer");
    expect(ladder).toHaveTextContent("Reader");
    expect(ladder).toHaveTextContent("Your role");
    expect(
      within(ladder)
        .getAllByRole("listitem")
        .map(
          (item) =>
            within(item).getByText(/^(Owner|Admin|Writer|Reader)$/).textContent,
        ),
    ).toEqual(["Owner", "Admin", "Writer", "Reader"]);
    expect(within(ladder).getAllByRole("listitem")[0]).toHaveAttribute(
      "aria-current",
      "true",
    );

    const policyLink = screen.getByRole("link", {
      name: /Change in Settings/i,
    });
    expect(policyLink.getAttribute("href")).toBe(
      "/vault/platform-docs/settings#access",
    );
    expect(
      screen.getByRole("button", { name: "Invite member" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Change role for mina" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "More actions for mina" }),
    ).toBeInTheDocument();
  });

  it("opens the member action menu and keeps destructive actions explicit", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(
      await screen.findByRole("button", { name: "More actions for mina" }),
    );
    expect(
      screen.getByRole("menuitem", { name: "Transfer ownership" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: "Revoke access" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("menuitem", { name: "Revoke access" }));
    expect(await screen.findByText("Revoke mina?")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Revoke access" }),
    ).toBeInTheDocument();
  });

  it("renders a genuinely read-only roster for readers", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "platform-docs",
      role: "reader",
      role_source: "member",
      public_access: "none",
    });
    getMeMock.mockResolvedValue({ username: "dana" });
    renderPage();

    await waitFor(() =>
      expect(screen.getByText(/roster is read-only/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "Invite member" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Change role for/i }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /More actions for/i }),
    ).toBeNull();
    expect(
      screen.queryByRole("link", { name: /Change in Settings/i }),
    ).toBeNull();
    expect(
      screen.getByText("Only the vault owner can change public access."),
    ).toBeInTheDocument();
  });

  it("keeps member management for admins without implying owner-only policy control", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "platform-docs",
      role: "admin",
      role_source: "member",
      public_access: "reader",
    });
    getMeMock.mockResolvedValue({ username: "admin-user" });
    renderPage();

    expect(
      await screen.findByRole("button", { name: "Invite member" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Change in Settings/i }),
    ).toBeNull();
    expect(
      screen.getByText("Only the vault owner can change public access."),
    ).toBeInTheDocument();
    expect(screen.getByText("View only")).toBeInTheDocument();
  });
});
