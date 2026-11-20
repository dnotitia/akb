import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import {
  ApiError,
  approveAdminSsoOperationRequest,
  denyAdminSsoOperationRequest,
  getAdminAuthConfig,
  getAdminRealmAccountResidue,
  getAdminSession,
  getAdminSsoCatalog,
  getAdminSsoOperationRequests,
  getAdminSsoOwnership,
  parseAdminSsoOperationRequests,
  setAdminSsoOwnership,
  setAdminSsoProviderEnabled,
  type AdminSsoOwnership,
  type AdminSsoProvider,
} from "@/lib/api";
import AdminPage from "../admin";


vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getAdminAuthConfig: vi.fn(),
    getAdminSession: vi.fn(),
    getAdminSsoCatalog: vi.fn(),
    getAdminRealmAccountResidue: vi.fn(),
    getAdminSsoOwnership: vi.fn(),
    setAdminSsoOwnership: vi.fn(),
    getAdminSsoOperationRequests: vi.fn(),
    approveAdminSsoOperationRequest: vi.fn(),
    denyAdminSsoOperationRequest: vi.fn(),
    setAdminSsoProviderEnabled: vi.fn(),
    configureAdminSsoProvider: vi.fn(),
  };
});

vi.mock("@/hooks/use-theme", () => ({
  useTheme: () => ({ theme: "light", setTheme: vi.fn() }),
}));

const requests = parseAdminSsoOperationRequests(JSON.parse(readFileSync(
  "../backend/tests/fixtures/admin_sso_operation_requests_v1.json",
  "utf8",
)));
const [claim, grant] = requests;

const provider: AdminSsoProvider = {
  provider_type: "keycloak-oidc",
  alias: "workforce",
  display_name: "Company SSO",
  state: "enabled",
  enabled: true,
  issuer: "https://accounts.example.com/realms/workforce",
  discovery_url: "https://accounts.example.com/realms/workforce/.well-known/openid-configuration",
  client_id: "akb-broker",
  client_secret_configured: true,
  redirect_uri: "https://auth.akb.example.com/realms/akb/broker/workforce/endpoint",
  post_logout_redirect_uri: "https://auth.akb.example.com/realms/akb/broker/workforce/endpoint/logout_response",
  capabilities: { supports_logout: true, supports_identity_migration: true },
};

const unowned: AdminSsoOwnership = {
  schema_version: 1,
  target_id: "0b7e1a33-58f1-4f2f-8f3e-2b9c6d7e8a02",
  provider_alias: "workforce",
  owner_id: null,
  revision: 0,
  fingerprint: "a".repeat(64),
  provider_secret_revision: null,
  drifted: false,
  emergency_disabled: false,
};
const owned: AdminSsoOwnership = {
  ...unowned,
  owner_id: "controller-instance",
  revision: 3,
  fingerprint: "b".repeat(64),
  provider_secret_revision: "secret-v2", // pragma: allowlist secret
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/admin"]}>
        <AdminPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function catalog(providers: AdminSsoProvider[] = [provider]) {
  return {
    schema_version: 1 as const,
    auth_mode: "sso" as const,
    control_mode: "direct" as const,
    supported_provider_types: ["keycloak-oidc"],
    providers,
  };
}

beforeEach(() => {
  vi.mocked(getAdminAuthConfig).mockResolvedValue({
    available: true,
    schema_version: 1,
    auth_mode: "sso",
    local: { enabled: false, login_url: null },
    keycloak: { enabled: true, login_url: "/api/v1/admin/auth/keycloak/login" },
  });
  vi.mocked(getAdminSession).mockResolvedValue({
    schema_version: 1,
    auth_mode: "sso",
    user: {
      id: "11111111-1111-1111-1111-111111111111",
      username: "admin",
      email: "admin@example.com",
      display_name: "Admin",
      is_admin: true,
    },
  });
  vi.mocked(getAdminSsoCatalog).mockResolvedValue(catalog());
  vi.mocked(getAdminRealmAccountResidue).mockResolvedValue({
    schema_version: 1,
    state: "unavailable",
    unavailable_code: "keycloak_provider_control_delegated",
    scan_limit: 1000,
    truncated: null,
    realm_account_count: null,
    bound_count: null,
    pending_arrival_count: null,
    residue_count: null,
    accounts: null,
  });
  vi.mocked(getAdminSsoOwnership).mockResolvedValue(unowned);
  vi.mocked(getAdminSsoOperationRequests).mockResolvedValue(requests);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("operation requests", () => {
  it("renders and approves a service-key claim and grant with the optional UUIDs", async () => {
    const modern = requests.slice(0, 2).map((entry, index) => {
      const request = {
        ...entry.request,
        request_nonce: `12345678-1234-4567-89ab-123456789ab${index}`,
        expected_realm_id: "abcdefab-1234-4567-89ab-123456789abc",
      };
      // Matches the backend's sorted-key, compact JSON for this ASCII fixture.
      const canonical = JSON.stringify(request, (_key, value: unknown) => {
        if (value === null || typeof value !== "object" || Array.isArray(value)) return value;
        return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0));
      });
      return { ...entry, request, request_sha256: createHash("sha256").update(canonical).digest("hex") };
    });
    const parsed = parseAdminSsoOperationRequests({ schema_version: 1, requests: modern });
    vi.mocked(getAdminSsoOperationRequests).mockResolvedValue(parsed);
    vi.mocked(approveAdminSsoOperationRequest).mockResolvedValue({ ...parsed[0], status: "approved" });
    const user = userEvent.setup();
    renderPage();

    const claimCard = await screen.findByRole("listitem", { name: "Ownership claim for workforce" });
    const grantCard = screen.getByRole("listitem", { name: "Provider operation for workforce" });
    expect(within(claimCard).getByText(modern[0].request_sha256)).toBeInTheDocument();
    expect(within(grantCard).getByText(modern[1].request_sha256)).toBeInTheDocument();
    expect(within(grantCard).getByText("configure, enable")).toBeInTheDocument();
    expect(screen.queryByText("Operation requests could not be loaded")).toBeNull();
    await user.click(within(claimCard).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(approveAdminSsoOperationRequest).toHaveBeenCalledWith(
      modern[0].request_id, modern[0].request_sha256,
    ));
  });

  it("shows what a pending request asks for and approves exactly the digest it showed", async () => {
    vi.mocked(approveAdminSsoOperationRequest).mockResolvedValue({ ...claim, status: "approved" });
    const user = userEvent.setup();
    renderPage();

    const card = await screen.findByRole("listitem", { name: "Ownership claim for workforce" });
    expect(within(card).getByText("platform-controller")).toBeInTheDocument();
    expect(within(card).getByText("controller-instance")).toBeInTheDocument();
    expect(within(card).getByText(claim.request_sha256)).toBeInTheDocument();
    const grantCard = screen.getByRole("listitem", { name: "Provider operation for workforce" });
    expect(within(grantCard).getByText("configure, enable")).toBeInTheDocument();
    expect(within(grantCard).getByText("akb-broker-next")).toBeInTheDocument();
    expect(within(grantCard).getByText("600 seconds after approval")).toBeInTheDocument();
    expect(within(grantCard).getByText("Secret held by the requester, not shown")).toBeInTheDocument();
    expect(screen.getByText("Approved, not yet collected")).toBeInTheDocument();

    await user.click(within(card).getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(approveAdminSsoOperationRequest).toHaveBeenCalledWith(claim.request_id, claim.request_sha256);
    });
    expect(await screen.findByText("controller-instance now owns workforce.")).toBeInTheDocument();
    // Ownership and the list are read again after the decision.
    await waitFor(() => expect(getAdminSsoOperationRequests).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getAdminSsoOwnership).toHaveBeenCalledTimes(2));
    expect(denyAdminSsoOperationRequest).not.toHaveBeenCalled();
  });

  it("denies exactly the digest it showed", async () => {
    vi.mocked(denyAdminSsoOperationRequest).mockResolvedValue({ ...grant, status: "denied" });
    const user = userEvent.setup();
    renderPage();

    const card = await screen.findByRole("listitem", { name: "Provider operation for workforce" });
    await user.click(within(card).getByRole("button", { name: "Deny" }));

    await waitFor(() => {
      expect(denyAdminSsoOperationRequest).toHaveBeenCalledWith(grant.request_id, grant.request_sha256);
    });
    expect(await screen.findByText("The request for workforce was denied.")).toBeInTheDocument();
    expect(approveAdminSsoOperationRequest).not.toHaveBeenCalled();
  });

  it("shows a stale screen's approval as a conflict and replaces the list with the current one", async () => {
    vi.mocked(approveAdminSsoOperationRequest).mockRejectedValue(
      new ApiError("conflict", 409, { code: "sso_operation_request_not_pending" }),
    );
    vi.mocked(getAdminSsoOperationRequests).mockResolvedValueOnce(requests).mockResolvedValue([]);
    const user = userEvent.setup();
    renderPage();

    const card = await screen.findByRole("listitem", { name: "Ownership claim for workforce" });
    await user.click(within(card).getByRole("button", { name: "Approve" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The screen was out of date");
    expect(alert).toHaveTextContent("sso_operation_request_not_pending");
    expect(await screen.findByText("No operation request is waiting for a decision.")).toBeInTheDocument();
    expect(screen.queryByText(/now owns/)).toBeNull();
  });

  it("shows an installation with no provider and no request as empty, not as an error", async () => {
    vi.mocked(getAdminSsoCatalog).mockResolvedValue(catalog([]));
    vi.mocked(getAdminSsoOperationRequests).mockResolvedValue([]);
    renderPage();

    expect(await screen.findByText("No operation request is waiting for a decision.")).toBeInTheDocument();
    expect(screen.getByText("No upstream SSO provider is configured yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(getAdminSsoOwnership).not.toHaveBeenCalled();
  });
});

describe("provider ownership", () => {
  it("replaces configure and enable with the external owner, and keeps disable", async () => {
    vi.mocked(getAdminSsoOwnership).mockResolvedValue(owned);
    vi.mocked(setAdminSsoProviderEnabled).mockResolvedValue({ ...provider, state: "configured_disabled", enabled: false });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Managed by an external owner")).toBeInTheDocument();
    expect(screen.getByText("Revision 3")).toBeInTheDocument();
    expect(screen.getByText(/Secret revision recorded/)).toHaveTextContent("secret-v2");
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Enable" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(setAdminSsoProviderEnabled).toHaveBeenCalledWith("workforce", false));
  });

  it("offers no enable for a disabled owned provider and shows latch and drift", async () => {
    vi.mocked(getAdminSsoCatalog).mockResolvedValue(catalog([{ ...provider, state: "configured_disabled", enabled: false }]));
    vi.mocked(getAdminSsoOwnership).mockResolvedValue({ ...owned, emergency_disabled: true, drifted: true });
    renderPage();

    expect(await screen.findByText("Managed by an external owner")).toBeInTheDocument();
    expect(screen.getByText("Emergency latch set")).toBeInTheDocument();
    expect(screen.getByText("Drift detected")).toBeInTheDocument();
    for (const name of ["Edit", "Enable", "Disable"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("keeps the ordinary controls for an unowned provider", async () => {
    renderPage();
    expect(await screen.findByText("No external owner")).toBeInTheDocument();
    expect(screen.getByText("No secret revision recorded.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.queryByText("Managed by an external owner")).toBeNull();
    expect(screen.queryByRole("button", { name: "Release ownership" })).toBeNull();
  });

  it("releases ownership with a compare-and-set on what the screen showed", async () => {
    vi.mocked(getAdminSsoOwnership).mockResolvedValue(owned);
    vi.mocked(setAdminSsoOwnership).mockResolvedValue({ ...owned, owner_id: null, revision: 4, provider_secret_revision: null });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Release ownership" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Releasing clears the recorded secret revision (secret-v2)");
    await user.click(within(dialog).getByRole("button", { name: "Release ownership" }));

    await waitFor(() => {
      expect(setAdminSsoOwnership).toHaveBeenCalledWith("workforce", {
        owner_id: null,
        expected_revision: 3,
        expected_fingerprint: "b".repeat(64),
        clear_emergency: false,
      });
    });
    expect(await screen.findByText("Ownership of workforce was released.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("refuses a release from a stale screen and keeps the dialog open with the reason", async () => {
    vi.mocked(getAdminSsoOwnership).mockResolvedValue(owned);
    vi.mocked(setAdminSsoOwnership).mockRejectedValue(new ApiError("conflict", 409, { code: "sso_provider_revision_conflict" }));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Release ownership" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Release ownership" }));

    expect(await within(dialog).findByText(/changed since this screen loaded/)).toBeInTheDocument();
    await waitFor(() => expect(getAdminSsoOwnership).toHaveBeenCalledTimes(2));
  });

  it("clears the emergency latch without changing the owner", async () => {
    vi.mocked(getAdminSsoOwnership).mockResolvedValue({ ...owned, emergency_disabled: true });
    vi.mocked(setAdminSsoOwnership).mockResolvedValue({ ...owned, revision: 4 });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Clear emergency latch" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("It does not enable the provider.");
    await user.click(within(dialog).getByRole("button", { name: "Clear emergency latch" }));

    await waitFor(() => {
      expect(setAdminSsoOwnership).toHaveBeenCalledWith("workforce", {
        owner_id: "controller-instance",
        expected_revision: 3,
        expected_fingerprint: "b".repeat(64),
        clear_emergency: true,
      });
    });
    expect(await screen.findByText(/The emergency latch on workforce was cleared/)).toBeInTheDocument();
    expect(screen.queryByText("Emergency latch set")).toBeNull();
  });
});
