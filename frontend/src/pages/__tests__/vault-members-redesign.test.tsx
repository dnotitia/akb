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

import {
  getMe,
  getVaultInfo,
  getVaultMembers,
  grantAccess,
  revokeAccess,
  transferOwnership,
} from "@/lib/api";

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
  it("prioritizes one roster and discloses role guidance only on request", async () => {
    const user = userEvent.setup();
    renderPage();
    const roster = await screen.findByRole("table", { name: "Vault members" });
    expect(
      screen.getByRole("heading", { level: 1, name: "Members" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "Members" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("complementary", { name: "Member access context" }),
    ).toBeNull();
    expect(
      screen.queryByRole("list", {
        name: "Roles from highest to lowest access",
      }),
    ).toBeNull();
    expect(screen.getByText("3 members")).toBeInTheDocument();
    expect(within(roster).getByText("Vault Owner")).toBeInTheDocument();
    expect(within(roster).getByText("You")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Settings/ })).toBeNull();
    expect(screen.queryByText(/People not listed here/)).toBeNull();
    expect(screen.getByRole("button", { name: "Invite member" })).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Change role for mina" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "More actions for mina" }),
    ).toBeEnabled();
    const trigger = within(roster).getByRole("button", {
      name: "Role permissions",
    });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Role permissions" });
    const ladder = within(dialog).getByRole("list", {
      name: "Roles from highest to lowest access",
    });
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
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps unknown policy unknown rather than reporting Private", async () => {
    getVaultInfoMock.mockRejectedValue(new Error("Unavailable"));
    renderPage();
    expect(
      await screen.findByRole("table", { name: "Vault members" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/People not listed here/)).toBeNull();
    expect(screen.queryByText("Private", { exact: true })).toBeNull();
    expect(screen.queryByRole("button", { name: "Invite member" })).toBeNull();
  });

  it("retains filtering and its recovery without hiding the active filter", async () => {
    getVaultMembersMock.mockResolvedValue({
      members: [
        ...MEMBERS,
        ...Array.from({ length: 8 }, (_, i) => ({
          username: `user-${i}`,
          email: `user${i}@example.com`,
          role: "reader",
        })),
      ],
    });
    const user = userEvent.setup();
    renderPage();
    const search = await screen.findByRole("searchbox", {
      name: "Filter members",
    });
    await user.type(search, "MINA@");
    expect(
      screen.getByRole("table", { name: "Vault members" }),
    ).toHaveTextContent("Mina Park");
    expect(
      screen.getByRole("table", { name: "Vault members" }),
    ).not.toHaveTextContent("Dana Lee");
    await user.clear(search);
    await user.type(search, "missing");
    await user.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(search).toHaveValue("");
    expect(
      screen.getByRole("table", { name: "Vault members" }),
    ).toHaveTextContent("Dana Lee");
  });

  it.each(["is_archived", "is_external_git"])(
    "does not promise public writes when %s restricts content",
    async (restriction) => {
      getVaultInfoMock.mockResolvedValue({
        name: "platform-docs",
        role: "owner",
        public_access: "writer",
        [restriction]: true,
      });
      renderPage();
      await screen.findByRole("table", { name: "Vault members" });
      expect(
        screen.getByText(
          "People not listed here can also read this vault when signed in. Content is read-only.",
        ),
      ).toBeInTheDocument();
    },
  );

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
    expect(screen.queryByText(/People not listed here/)).toBeNull();
  });

  it("keeps role changes and Undo connected to the API", async () => {
    vi.mocked(grantAccess).mockResolvedValue({} as never);
    const user = userEvent.setup();
    renderPage();
    await user.click(
      await screen.findByRole("button", { name: "Change role for mina" }),
    );
    await user.click(screen.getByRole("menuitemradio", { name: "admin" }));
    expect(grantAccess).toHaveBeenCalledWith("platform-docs", "mina", "admin");
    await user.click(await screen.findByRole("button", { name: "Undo" }));
    expect(grantAccess).toHaveBeenLastCalledWith(
      "platform-docs",
      "mina",
      "writer",
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Undo" })).toBeNull(),
    );
  });

  it.each(["revoke", "transfer"] as const)(
    "requires confirmation before %s and refreshes afterward",
    async (action) => {
      const user = userEvent.setup();
      vi.mocked(revokeAccess).mockResolvedValue({} as never);
      vi.mocked(transferOwnership).mockResolvedValue({} as never);
      renderPage();
      await user.click(
        await screen.findByRole("button", { name: "More actions for mina" }),
      );
      const label =
        action === "revoke" ? "Revoke access" : "Transfer ownership";
      const api = action === "revoke" ? revokeAccess : transferOwnership;
      await user.click(screen.getByRole("menuitem", { name: label }));
      expect(api).not.toHaveBeenCalled();
      await user.click(
        within(screen.getByRole("dialog")).getByRole("button", { name: label }),
      );
      expect(api).toHaveBeenCalledWith("platform-docs", "mina");
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(getVaultMembers).toHaveBeenCalledTimes(2);
    },
  );

  it("waits for the current identity before exposing per-member management", async () => {
    getMeMock.mockRejectedValue(new Error("Session unavailable"));
    renderPage();
    await screen.findByRole("table", { name: "Vault members" });
    expect(
      screen.queryByRole("button", { name: /Change role for/ }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /More actions for/ }),
    ).toBeNull();
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
      screen.getByText(
        "People not listed here can also read this vault when signed in.",
      ),
    ).toBeInTheDocument();
  });

  it.each([
    [
      "reader",
      "People not listed here can also read this vault when signed in.",
    ],
    [
      "writer",
      "People not listed here can also read and change content when signed in.",
    ],
  ])(
    "explains effective %s access without turning the roster into public settings",
    async (publicAccess, note) => {
      getVaultInfoMock.mockResolvedValue({
        name: "platform-docs",
        role: "owner",
        public_access: publicAccess,
      });
      renderPage();
      const roster = await screen.findByRole("table", {
        name: "Vault members",
      });
      const notice = screen.getByText(note);
      expect(notice).toBeVisible();
      expect(
        roster.compareDocumentPosition(notice) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).not.toBe(0);
      expect(screen.queryByRole("link", { name: /Settings/ })).toBeNull();
      expect(
        screen.getByRole("button", { name: "Change role for mina" }),
      ).toBeEnabled();
    },
  );
});
