import { localAuthConfig } from "@/stories/page-story-fixtures";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionSetup } from "../connection-setup";
import { QuickstartDialog } from "../quickstart-dialog";
import { ApiError, AUTH_CONFIG_UNAVAILABLE, configureAuthTransport, parseAuthConfig, setToken } from "@/lib/api";
import { issuePat, getPatCapabilities } from "@/lib/api-pat-issuance";

const connectionConfig = (oauth: boolean, pat = true) => parseAuthConfig({
  ...localAuthConfig,
  mcp_oauth: { enabled: oauth },
  personal_access_tokens: { enabled: pat },
  mcp_endpoint_url: "https://knowledge.example.test/workspace/mcp/",
});

vi.mock("@/lib/api-pat-issuance", async () => ({ ...await vi.importActual<typeof import("@/lib/api-pat-issuance")>("@/lib/api-pat-issuance"), issuePat: vi.fn(), getPatCapabilities: vi.fn() }));
beforeEach(() => { vi.clearAllMocks(); configureAuthTransport("local"); setToken("fixture-session"); vi.mocked(getPatCapabilities).mockResolvedValue({ contract_version: 1, user_id: "00000000-0000-0000-0000-000000000001", name_max_length: 255, permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"], vault_scope_semantics: "write_restriction_sql_read_write" }); });
const fresh = { token: "akb_fresh_secret", token_id: "fresh-id", name: "laptop", prefix: "akb_fresh", verified: true };
const setup = async (user: ReturnType<typeof userEvent.setup>, tool = "Claude Code") => user.click(screen.getByRole("button", { name: `Continue with ${tool}` }));
const method = async (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("radio", { name: "Personal connection key (PAT)" }));
const backToTool = async (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("button", { name: "Choose a tool" }));

describe("Connection setup", () => {
  it("starts with a tool choice and only shows the requested step", async () => {
    const user = userEvent.setup();
    render(<ConnectionSetup layout="settings" authConfig={connectionConfig(false)} />);
    expect(screen.getByRole("radio", { name: "Claude Code" })).toBeChecked();
    expect(screen.queryByLabelText("Connection key name")).toBeNull();
    expect(screen.getAllByRole("heading", { level: 3 })).toHaveLength(1);
    expect(issuePat).not.toHaveBeenCalled();
    expect(getPatCapabilities).not.toHaveBeenCalled();
    await setup(user);
    expect(screen.getByRole("heading", { name: "2. Add AKB to Claude Code" })).toHaveFocus();
    await user.type(screen.getByLabelText("Connection key name"), "work laptop");
    await backToTool(user);
    await user.click(screen.getByRole("radio", { name: "Codex" }));
    await setup(user, "Codex");
    expect(screen.getByLabelText("Connection key name")).toHaveValue("work laptop");
    await user.click(screen.getByRole("button", { name: "Go to step 3: Check in your tool" }));
    expect(screen.getByRole("heading", { name: "3. Check the connection" })).toHaveFocus();
    expect(screen.getByText(/This browser cannot verify/)).toBeVisible();
    expect(issuePat).not.toHaveBeenCalled();
  });

  it("never offers placeholder credentials or creates a key on navigation", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup authConfig={connectionConfig(false)} />);
    await setup(user);
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    expect(screen.getByRole("button", { name: "Check in Claude Code" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Go to step 3: Check in your tool" }));
    expect(screen.getByText(/This browser cannot verify/)).toBeVisible();
    expect(issuePat).not.toHaveBeenCalled();
  });

  it("guards repeated submission and keeps an uncertain result recoverable", async () => {
    let reject!: (error: Error) => void;
    vi.mocked(issuePat).mockImplementation(() => new Promise((_, no) => { reject = no; }));
    const user = userEvent.setup(); render(<ConnectionSetup authConfig={connectionConfig(false)} />); await setup(user);
    fireEvent.change(screen.getByLabelText("Connection key name"), { target: { value: "laptop" } });
    const form = screen.getByLabelText("Connection key name").closest("form")!;
    await waitFor(() => expect(screen.getByRole("button", { name: "Create connection key" })).toBeEnabled());
    fireEvent.submit(form); fireEvent.submit(form);
    expect(issuePat).toHaveBeenCalledTimes(1);
    await act(async () => reject(new Error("Token creation unavailable")));
    expect(screen.getByRole("alert")).toHaveTextContent("We could not confirm whether the key was created");
    expect(screen.getByLabelText("Connection key name")).toHaveValue("laptop");
  });

  it("uses the same URL for stdio pre-login and proxy setup, and delegates HTTP sign-in to the tool", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup authConfig={connectionConfig(true)} />); await setup(user);
    expect(screen.getByRole("radio", { name: "Sign in with AKB (OAuth/SSO)" })).toBeChecked();
    expect(screen.queryByLabelText("Connection key name")).toBeNull();
    expect(screen.getByRole("radio", { name: "Local helper (stdio)" })).toBeChecked();
    const login = screen.getByLabelText("Terminal · AKB sign-in");
    const proxy = screen.getByLabelText("Terminal · Claude Code");
    expect(login.textContent).toBe("npx akb-mcp login --url https://knowledge.example.test/workspace/mcp/");
    expect(proxy.textContent).toBe("claude mcp add --scope user akb -- npx akb-mcp --url https://knowledge.example.test/workspace/mcp/");
    expect(login.compareDocumentPosition(proxy) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await user.click(screen.getByRole("radio", { name: "Direct connection (HTTP)" }));
    expect(screen.getByRole("radio", { name: "Sign in with AKB (OAuth/SSO)" })).toBeChecked();
    expect(screen.queryByLabelText("Terminal · AKB sign-in")).toBeNull();
    expect(screen.getByText(/--transport http/)).toBeVisible();
    expect(screen.getByText(/Open Claude Code, enter \/mcp/)).toBeVisible();
    expect(screen.getByText(/no tested Direct connection.*Personal connection key/)).toBeVisible();
    expect(issuePat).not.toHaveBeenCalled();
  });

  it("uses the published endpoint for OAuth even when PATs are disabled", async () => {
    const user = userEvent.setup();
    render(<ConnectionSetup authConfig={connectionConfig(true, false)} />);
    await setup(user);
    expect(screen.getByLabelText("Terminal · AKB sign-in")).toHaveTextContent("https://knowledge.example.test/workspace/mcp/");
    expect(screen.queryByRole("radio", { name: "Personal connection key (PAT)" })).toBeNull();
    expect(getPatCapabilities).not.toHaveBeenCalled();
    await backToTool(user);
    await user.click(screen.getByRole("radio", { name: "Codex" }));
    await setup(user, "Codex");
    expect(screen.getByLabelText("Terminal · AKB sign-in")).toBeVisible();
    expect(screen.getByRole("radio", { name: "Direct connection (HTTP)" })).toBeDisabled();
    expect(screen.getByText(/no tested direct HTTP preset for Codex/)).toBeVisible();
    expect(screen.queryByRole("radio", { name: "Use an existing key" })).toBeNull();
  });

  it("withholds a proxy command when the public endpoint uses non-loopback HTTP", async () => {
    const user = userEvent.setup();
    const config = parseAuthConfig({ ...localAuthConfig, mcp_endpoint_url: "http://akb.example.test/mcp/" });
    render(<ConnectionSetup authConfig={config} />);
    await setup(user);
    expect(screen.queryByRole("radio", { name: "Use an existing key" })).toBeNull();
    expect(getPatCapabilities).not.toHaveBeenCalled();
    expect(screen.getByText(/requires HTTPS or loopback HTTP/)).toBeVisible();
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
  });

  it("distinguishes policy denial from unavailable connection options", async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    const { rerender } = render(<ConnectionSetup authConfig={connectionConfig(false, false)} />);
    await setup(user);
    expect(screen.getByText(/no human MCP connection method/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Retry connection options" })).toBeNull();
    rerender(<ConnectionSetup authConfig={AUTH_CONFIG_UNAVAILABLE} onRetryAuthConfig={retry} />);
    await user.click(screen.getByRole("button", { name: "Retry connection options" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(screen.queryByRole("radio", { name: "Create a key" })).toBeNull();
    expect(screen.queryByRole("radio", { name: "Use an existing key" })).toBeNull();
    expect(getPatCapabilities).not.toHaveBeenCalled();
  });

  it("stops offering PAT configuration while preserving an in-flight one-time receipt", async () => {
    let complete: (value: typeof fresh) => void = () => { throw new Error("Issuance never started"); };
    vi.mocked(issuePat).mockImplementation(() => new Promise(resolve => { complete = resolve; }));
    const user = userEvent.setup();
    const { rerender } = render(<ConnectionSetup authConfig={connectionConfig(false)} />);
    await setup(user);
    await user.click(screen.getByRole("radio", { name: "Use an existing key" }));
    await user.type(screen.getByLabelText("Full connection key"), "akb_saved");
    expect(screen.getByText(/npx akb-mcp/)).toHaveTextContent("https://knowledge.example.test/workspace/mcp/");
    await user.click(screen.getByRole("radio", { name: "Create a key" }));
    await user.type(screen.getByLabelText("Connection key name"), "laptop");
    await user.click(screen.getByRole("button", { name: "Create connection key" }));
    rerender(<ConnectionSetup authConfig={connectionConfig(false, false)} />);
    expect(screen.queryByRole("button", { name: "Create connection key" })).toBeNull();
    await act(async () => complete(fresh));
    expect(screen.getByText("Connection key created")).toBeVisible();
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
  });

  it("blocks stale key setup when issuance reports that the deployment disabled it", async () => {
    vi.mocked(issuePat).mockRejectedValue(new ApiError("Personal access tokens are disabled", 403, { code: "personal_access_tokens_disabled" }));
    const retry = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<ConnectionSetup authConfig={connectionConfig(true)} onRetryAuthConfig={retry} />);
    await setup(user);
    await method(user);
    await user.type(screen.getByLabelText("Connection key name"), "work laptop");
    await user.click(screen.getByRole("button", { name: "Create connection key" }));

    expect(await screen.findByText("Personal access tokens are disabled")).toBeVisible();
    expect(retry).toHaveBeenCalledOnce();
    rerender(<ConnectionSetup authConfig={connectionConfig(true)} onRetryAuthConfig={retry} />);
    expect(screen.queryByRole("radio", { name: "Personal connection key (PAT)" })).toBeNull();
    expect(screen.getByRole("radio", { name: "Sign in with AKB (OAuth/SSO)" })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Check in Claude Code" })).toBeDisabled();
  });

  it("uses native radio keys for separate transport and authentication choices", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup authConfig={connectionConfig(true)} />);
    await user.click(screen.getByRole("radio", { name: "Cursor" }));
    await setup(user, "Cursor");
    await user.tab();
    expect(screen.getByRole("radio", { name: "Local helper (stdio)" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Direct connection (HTTP)" })).toBeChecked();
    await user.tab();
    expect(screen.getByRole("radio", { name: "Sign in with AKB (OAuth/SSO)" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Personal connection key (PAT)" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Direct connection (HTTP)" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Use an existing key" }));
    expect(screen.getByLabelText("Full connection key")).toBeVisible();
  });

  it("rejects prefixes and shell text instead of putting them in a command", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup authConfig={connectionConfig(false)} />); await setup(user);
    await user.click(screen.getByRole("radio", { name: "Use an existing key" }));
    const field = screen.getByLabelText("Full connection key");
    for (const value of ["akb_prefix…", "akb_$(command)"]) {
      await user.clear(field); await user.type(field, value);
      expect(screen.getByRole("alert")).toHaveTextContent("Enter the full key");
      expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    }
    await user.clear(field); await user.type(field, "akb_complete_saved_secret");
    expect(screen.getByText(/npx akb-mcp/)).toHaveTextContent("akb_complete_saved_secret");
    expect(issuePat).not.toHaveBeenCalled();
  });

  it("preserves a chosen key method and draft when changing tools", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup authConfig={connectionConfig(true)} />); await setup(user); await method(user);
    await user.type(screen.getByLabelText("Connection key name"), "retained draft");
    await backToTool(user); await user.click(screen.getByRole("radio", { name: "Cursor" })); await setup(user, "Cursor");
    expect(screen.getByRole("radio", { name: "Personal connection key (PAT)" })).toBeChecked();
    expect(screen.getByLabelText("Connection key name")).toHaveValue("retained draft");
  });

  it("keeps a created key in the configuration across step navigation and hiding", async () => {
    vi.mocked(issuePat).mockResolvedValue(fresh);
    const user = userEvent.setup(); render(<ConnectionSetup authConfig={connectionConfig(false)} />); await setup(user);
    await user.type(screen.getByLabelText("Connection key name"), "laptop");
    await user.click(screen.getByRole("button", { name: "Create connection key" }));
    await screen.findByText("Connection key created");
    expect(screen.getByText(/npx akb-mcp/)).toHaveTextContent("akb_fresh_secret");
    expect(screen.queryByRole("button", { name: /dismiss token/ })).toBeNull();
    await backToTool(user);
    expect(document.body.textContent).not.toContain("akb_fresh_secret");
    await setup(user);
    expect(screen.getByText(/npx akb-mcp/)).toHaveTextContent("akb_fresh_secret");
    await user.click(screen.getByRole("button", { name: "Hide key and configuration" }));
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    await backToTool(user); await setup(user);
    await user.click(screen.getByRole("button", { name: "Show key and configuration" }));
    expect(screen.getByText(/npx akb-mcp/)).toHaveTextContent("akb_fresh_secret");
    expect(issuePat).toHaveBeenCalledTimes(1);
  });

  it("uses different close guidance for an unissued draft and a fresh secret", async () => {
    vi.mocked(issuePat).mockResolvedValue(fresh);
    const user = userEvent.setup(), onOpenChange = vi.fn();
    render(<QuickstartDialog open onOpenChange={onOpenChange} authConfig={connectionConfig(false)} />); await setup(user);
    await user.type(screen.getByLabelText("Connection key name"), "laptop");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(await screen.findByRole("dialog", { name: "Leave connection setup?" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Keep setup open" }));
    await user.click(screen.getByRole("button", { name: "Create connection key" }));
    await screen.findByText("Connection key created");
    await user.click(screen.getByRole("button", { name: "Close" }));
    const confirm = await screen.findByRole("dialog", { name: "Have you saved your connection key?" });
    expect(onOpenChange).not.toHaveBeenCalled();
    await user.click(within(confirm).getByRole("button", { name: "I've saved it — close" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("invalidates only the matching fresh key and cannot resurrect it", async () => {
    vi.mocked(issuePat).mockResolvedValue(fresh);
    const user = userEvent.setup(); const { rerender } = render(<ConnectionSetup authConfig={connectionConfig(false)} />); await setup(user);
    await user.type(screen.getByLabelText("Connection key name"), "laptop"); await user.click(screen.getByRole("button", { name: "Create connection key" }));
    await screen.findByText("Connection key created");
    rerender(<ConnectionSetup authConfig={connectionConfig(false)} invalidatedTokenId="other-id" />);
    expect(screen.getByText(/npx akb-mcp/)).toHaveTextContent("akb_fresh_secret");
    rerender(<ConnectionSetup authConfig={connectionConfig(false)} invalidatedTokenId="fresh-id" />);
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("This setup token was revoked");
    rerender(<ConnectionSetup authConfig={connectionConfig(false)} invalidatedTokenId="third-id" />);
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
  });

  it("clears pasted keys after revocation because their ID is unknown", async () => {
    const user = userEvent.setup(); const { rerender } = render(<ConnectionSetup authConfig={connectionConfig(false)} />); await setup(user);
    await user.click(screen.getByRole("radio", { name: "Use an existing key" }));
    await user.type(screen.getByLabelText("Full connection key"), "akb_saved_secret");
    rerender(<ConnectionSetup authConfig={connectionConfig(false)} invalidatedTokenId="revoked-id" />);
    expect(screen.getByLabelText("Full connection key")).toHaveValue("");
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your current full token again");
  });

  it("preserves a selected key across Cursor transports and clears its configuration for an untested client combination", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup authConfig={connectionConfig(true)} />);
    const endpoint = "https://knowledge.example.test/workspace/mcp/";
    await user.click(screen.getByRole("radio", { name: "Cursor" })); await setup(user, "Cursor");
    const config = () => JSON.parse(screen.getByLabelText("mcp.json").textContent ?? "").mcpServers.akb;
    expect(config()).toEqual({ command: "npx", args: ["akb-mcp", "--url", endpoint] });
    await user.click(screen.getByRole("radio", { name: "Direct connection (HTTP)" }));
    expect(config()).toEqual({ url: endpoint });
    await method(user);
    await user.click(screen.getByRole("radio", { name: "Use an existing key" }));
    await user.type(screen.getByLabelText("Full connection key"), "akb_saved_secret");
    expect(config()).toEqual({ url: endpoint, headers: { Authorization: "Bearer akb_saved_secret" } });
    await user.click(screen.getByRole("radio", { name: "Local helper (stdio)" }));
    expect(config()).toEqual({ command: "npx", args: ["akb-mcp", "--url", endpoint, "--pat", "akb_saved_secret"] });
    await user.click(screen.getByRole("radio", { name: "Direct connection (HTTP)" }));

    await backToTool(user);
    await user.click(screen.getByRole("radio", { name: "Claude Code" })); await setup(user);
    expect(screen.getByText(/no tested Direct connection/)).toBeVisible();
    expect(screen.getByRole("radio", { name: "Sign in with AKB (OAuth/SSO)" })).not.toBeChecked();
    expect(screen.getByLabelText("Full connection key")).not.toBeVisible();
    expect(screen.queryByLabelText("Terminal · Claude Code")).toBeNull();
    await user.tab();
    await user.tab();
    expect(screen.getByRole("radio", { name: "Sign in with AKB (OAuth/SSO)" })).toHaveFocus();
    await user.keyboard(" ");
    expect(screen.getByLabelText("Terminal · Claude Code")).toHaveTextContent("--transport http");
    expect(issuePat).not.toHaveBeenCalled();
  });

  it("labels manual confirmation honestly and resets it when configuration changes", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup authConfig={connectionConfig(true)} />); await setup(user);
    await user.click(screen.getByRole("button", { name: "Check in Claude Code" }));
    await user.click(screen.getByRole("checkbox", { name: "I received an AKB response in my AI tool" }));
    expect(screen.getByText(/Confirmed by you/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Back to setup" })); await backToTool(user);
    await user.click(screen.getByRole("radio", { name: "Cursor" }));
    await user.click(screen.getByRole("button", { name: "Go to step 3: Check in your tool" }));
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByText(/This browser cannot verify/)).toBeVisible();
  });
});
