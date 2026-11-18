import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { HomeFirstRunConnect } from "@/components/home-first-run-connect";
import { configureAuthTransport, listPATs, setToken, type AuthConfig } from "@/lib/api";
import { getPatCapabilities, issuePat } from "@/lib/api-pat-issuance";

vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), listPATs: vi.fn() }));
vi.mock("@/lib/api-pat-issuance", async () => ({ ...await vi.importActual<typeof import("@/lib/api-pat-issuance")>("@/lib/api-pat-issuance"), issuePat: vi.fn(), getPatCapabilities: vi.fn() }));

const sso: AuthConfig = {
  available: true, schema_version: 2, auth_mode: "sso",
  local_auth: { enabled: false }, keycloak: { enabled: true, browser_session_ready: true },
  providers: [{ provider_type: "oidc", alias: "workforce", display_name: "Company SSO", login_url: "/api/v1/auth/sso/workforce/login" }],
  mcp_oauth: { enabled: true },
};
const keyOnly: AuthConfig = { ...sso, mcp_oauth: { enabled: false } };

function renderGuide(config: AuthConfig, vaults: { name: string }[] = []) {
  const props = { onRefreshVaults: vi.fn(), onStarted: vi.fn(), onDismiss: vi.fn() };
  const view = render(<MemoryRouter><HomeFirstRunConnect authConfig={config} vaults={vaults} {...props} /></MemoryRouter>);
  return { ...props, view };
}
const decoded = (link: HTMLElement) => decodeURIComponent(link.getAttribute("href")!.split(/prompt=|q=/)[1]);

beforeEach(() => {
  vi.clearAllMocks();
  configureAuthTransport("local"); setToken("fixture-session");
  vi.mocked(getPatCapabilities).mockResolvedValue({ contract_version: 1, user_id: "00000000-0000-0000-0000-000000000001", name_max_length: 255, permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"], vault_scope_semantics: "write_restriction_sql_read_write" });
  vi.mocked(listPATs).mockResolvedValue({ tokens: [] });
});
afterEach(cleanup);

describe("Home first-run AI connection guide", () => {
  it("opens Claude Code with a sign-in setup request and records that the hand-off started", async () => {
    const { onStarted } = renderGuide(sso);
    expect(screen.getByRole("heading", { name: "Use AKB from your AI app" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Claude Code/ })).toBeChecked();
    expect(screen.queryByLabelText("Connection key name")).toBeNull();
    const open = screen.getByRole("link", { name: "Open in Claude Code" });
    expect(open.getAttribute("href")).toMatch(/^claude:\/\/code\/new\?q=/);
    expect(decoded(open)).toContain("claude mcp login akb");
    expect(screen.getByRole("heading", { name: "Approve and sign in with Company SSO" })).toBeInTheDocument();
    fireEvent.click(open);
    expect(onStarted).toHaveBeenCalled();
    expect(within(screen.getByRole("list", { name: "Connect Claude Code" })).getAllByRole("listitem")[0]).not.toHaveAttribute("aria-current");
  });

  it("switches to a Codex request that signs in while adding the server", async () => {
    const user = userEvent.setup();
    renderGuide(sso);
    await user.click(screen.getByRole("radio", { name: /Codex/ }));
    const open = screen.getByRole("link", { name: "Open in Codex" });
    expect(open.getAttribute("href")).toMatch(/^codex:\/\/new\?prompt=/);
    expect(decoded(open)).toMatch(/codex mcp add akb --url \S+\/mcp\//);
    expect(screen.getByRole("link", { name: "Open the vault request" }).getAttribute("href")).toMatch(/^codex:\/\/new/);
  });

  it("creates a connection key in place, embeds it, and notices when the app uses it", async () => {
    const user = userEvent.setup();
    vi.mocked(issuePat).mockResolvedValue({ token: "akb_fresh_secret", token_id: "fresh-id", name: "Claude Code", prefix: "akb_fresh", key_class: "pat", scopes: ["read", "write"], vault_scope: null, expires_at: "2026-12-16T00:00:00Z", verified: true });
    renderGuide(keyOnly);
    expect(screen.getByRole("button", { name: "Open in Claude Code" })).toBeDisabled();
    expect(await screen.findByLabelText("Connection key name")).toHaveValue("Claude Code");
    await user.click(await screen.findByRole("button", { name: "Create connection key" }));
    expect(await screen.findByText(/Key “Claude Code” created/)).toBeInTheDocument();
    expect(vi.mocked(issuePat).mock.calls[0][2]).toMatchObject({ name: "Claude Code", expires_days: 30, scopes: ["read", "write"] });
    const open = screen.getByRole("link", { name: "Open in Claude Code" });
    expect(decoded(open)).toContain("--pat akb_fresh_secret");
    vi.mocked(listPATs).mockResolvedValue({ tokens: [{ token_id: "fresh-id", name: "Claude Code", prefix: "akb_fresh", last_used_at: "2026-11-16T01:00:00Z" }] });
    fireEvent.click(open);
    expect(await screen.findByText("Claude Code is connected.")).toBeInTheDocument();
  });

  it("configures Claude Desktop chats from Claude Code with a key", async () => {
    const user = userEvent.setup();
    renderGuide(sso);
    await user.click(screen.getByRole("radio", { name: /Claude Desktop/ }));
    expect(await screen.findByLabelText("Connection key name")).toHaveValue("Claude Desktop");
    expect(screen.getByRole("heading", { name: "Open Claude Code with the setup request" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the vault request" }).getAttribute("href")).toMatch(/^claude:\/\/claude\.ai\/new/);
  });

  it("waits for the first vault after the vault request is opened", async () => {
    const { onRefreshVaults, onStarted } = renderGuide(sso);
    fireEvent.click(screen.getByRole("link", { name: "Open the vault request" }));
    expect(onStarted).toHaveBeenCalled();
    expect(await screen.findByText("This page updates when your first vault appears.")).toBeInTheDocument();
    fireEvent(window, new Event("focus"));
    expect(onRefreshVaults).toHaveBeenCalled();
  });

  it("shows the first vault and requests to try next", async () => {
    const { onDismiss } = renderGuide(sso, [{ name: "home-user-notes" }]);
    expect(screen.getByRole("heading", { name: "Your first vault is ready" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open home-user-notes" })).toHaveAttribute("href", "/vault/home-user-notes");
    const tries = screen.getAllByRole("link", { name: /Open in Claude Code/ });
    expect(tries).toHaveLength(3);
    expect(decoded(tries[2])).toContain("home-user-notes");
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onDismiss).toHaveBeenCalled();
  });

  it("sends other tools to the full setup guide", async () => {
    const user = userEvent.setup();
    renderGuide(sso);
    await user.click(screen.getByRole("radio", { name: /Other tools/ }));
    await user.click(screen.getByRole("button", { name: "Open the full setup guide" }));
    await waitFor(() => expect(screen.getByRole("dialog", { name: "Connect an AI tool" })).toBeInTheDocument());
  });
});
