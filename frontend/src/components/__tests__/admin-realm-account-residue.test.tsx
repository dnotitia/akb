import { readFileSync } from "node:fs";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, getAdminRealmAccountResidue, type AdminRealmAccountResidue } from "@/lib/api";
import { RealmAccountResidue } from "../admin-realm-account-residue";


vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, getAdminRealmAccountResidue: vi.fn() };
});

const available = JSON.parse(readFileSync(
  "../backend/tests/fixtures/admin_sso_realm_account_residue_v1.json",
  "utf8",
)) as Extract<AdminRealmAccountResidue, { state: "available" }>;

function unavailable(code: string): AdminRealmAccountResidue {
  return {
    schema_version: 1,
    state: "unavailable",
    unavailable_code: code,
    scan_limit: 1000,
    truncated: null,
    realm_account_count: null,
    bound_count: null,
    pending_arrival_count: null,
    residue_count: null,
    accounts: null,
  };
}

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <RealmAccountResidue />
    </QueryClientProvider>,
  );
}

function tile(label: string): HTMLElement {
  const element = screen.getByText(label).parentElement;
  if (!element) throw new Error(`no tile ${label}`);
  return element;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("RealmAccountResidue", () => {
  it("shows the residue count and lists residue with no record ahead of pending arrivals", async () => {
    vi.mocked(getAdminRealmAccountResidue).mockResolvedValue(available);
    renderSection();

    expect(await screen.findByText("former.contractor")).toBeInTheDocument();
    expect(within(tile("Residue")).getByText("2")).toBeInTheDocument();
    expect(within(tile("Pending arrivals")).getByText("1")).toBeInTheDocument();
    expect(within(tile("Realm accounts read")).getByText("12")).toBeInTheDocument();

    const rows = screen.getAllByRole("listitem");
    expect(rows.map((row) => within(row).getByText(/\./, { selector: "p.font-medium" }).textContent)).toEqual([
      "former.contractor",
      "unknown.visitor",
      "invited.person",
    ]);
    expect(within(rows[0]).getByText("No record in AKB")).toBeInTheDocument();
    expect(within(rows[0]).getByText(/Created Aug 14, 2026/)).toBeInTheDocument();
    expect(within(rows[1]).getByText("Created at an unknown time")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Arrival pending")).toBeInTheDocument();
    expect(within(rows[2]).getByText("5e9b0c3a-7f21-4d68-8b4e-a03c6d2f1e95")).toBeInTheDocument();
    // Read-only: the section offers no way to delete, disable or expire.
    expect(screen.queryByRole("button", { name: /delete|remove|disable|expire/i })).toBeNull();
  });

  it("states an empty reconciliation as measured, not as missing", async () => {
    vi.mocked(getAdminRealmAccountResidue).mockResolvedValue({
      ...available,
      realm_account_count: 9,
      bound_count: 9,
      pending_arrival_count: 0,
      residue_count: 0,
      accounts: [],
    });
    renderSection();

    expect(await screen.findByText("Every realm account that was read has an AKB binding.")).toBeInTheDocument();
    expect(within(tile("Residue")).getByText("0")).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).toBeNull();
  });

  it("renders an unavailable realm read without any count", async () => {
    vi.mocked(getAdminRealmAccountResidue).mockResolvedValue(unavailable("realm_accounts_unavailable"));
    renderSection();

    expect(await screen.findByText("Realm accounts are unavailable")).toBeInTheDocument();
    expect(screen.getByText("realm_accounts_unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Residue")).toBeNull();
    expect(screen.queryByText("0")).toBeNull();
  });

  it("explains a delegated installation instead of reporting a failure", async () => {
    vi.mocked(getAdminRealmAccountResidue).mockResolvedValue(unavailable("keycloak_provider_control_delegated"));
    renderSection();

    expect(await screen.findByText("The realm is managed by the deployment operator")).toBeInTheDocument();
    expect(screen.queryByText("Residue")).toBeNull();
  });

  it("treats a failed request as unavailable and lets the administrator retry", async () => {
    vi.mocked(getAdminRealmAccountResidue)
      .mockRejectedValueOnce(new Error("Invalid realm account residue response"))
      .mockResolvedValueOnce(available);
    const user = userEvent.setup();
    renderSection();

    expect(await screen.findByText("Realm accounts could not be reconciled")).toBeInTheDocument();
    expect(screen.queryByText("Residue")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("former.contractor")).toBeInTheDocument();
  });

  it("says when the realm listing was truncated", async () => {
    vi.mocked(getAdminRealmAccountResidue).mockResolvedValue({ ...available, truncated: true });
    renderSection();

    expect(await screen.findByText("Realm listing truncated")).toBeInTheDocument();
    expect(screen.getByText(/more than 1,000 accounts/)).toBeInTheDocument();
  });

  it("says when the list shows fewer accounts than are unbound", async () => {
    vi.mocked(getAdminRealmAccountResidue).mockResolvedValue({
      ...available,
      accounts: available.accounts.slice(0, 1),
    });
    renderSection();

    expect(await screen.findByText(/Showing 1 of 3/)).toBeInTheDocument();
  });

  it("hides an earlier read once the admin session is refused", async () => {
    vi.mocked(getAdminRealmAccountResidue)
      .mockResolvedValueOnce(available)
      .mockRejectedValueOnce(new ApiError("Admin session required", 401, null));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <RealmAccountResidue />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("former.contractor")).toBeInTheDocument();

    await queryClient.refetchQueries();
    expect(await screen.findByText("Realm accounts could not be reconciled")).toBeInTheDocument();
    expect(screen.queryByText("former.contractor")).toBeNull();
  });
});
