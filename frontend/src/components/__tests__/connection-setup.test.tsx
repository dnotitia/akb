import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionSetup } from "../connection-setup";
import { QuickstartDialog } from "../quickstart-dialog";
import { configureAuthTransport, setToken } from "@/lib/api";
import { issuePat, getPatCapabilities } from "@/lib/api-pat-issuance";

vi.mock("@/lib/api-pat-issuance", async () => ({ ...await vi.importActual<typeof import("@/lib/api-pat-issuance")>("@/lib/api-pat-issuance"), issuePat: vi.fn(), getPatCapabilities: vi.fn() }));
beforeEach(() => { vi.clearAllMocks(); configureAuthTransport("local"); setToken("fixture-session"); vi.mocked(getPatCapabilities).mockResolvedValue({ contract_version: 1, user_id: "00000000-0000-0000-0000-000000000001", name_max_length: 255, permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"], vault_scope_semantics: "write_restriction_sql_read_write" }); });
const fresh = { token: "akb_fresh_secret", token_id: "fresh-id", name: "laptop", prefix: "akb_fresh", verified: true };
const setup = async (user: ReturnType<typeof userEvent.setup>, tool = "Claude Code") => user.click(screen.getByRole("button", { name: `Continue with ${tool}` }));
const method = async (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("radio", { name: "Use a connection key" }));
const backToTool = async (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("button", { name: "Choose a tool" }));

describe("Connection setup", () => {
  it("starts with a tool choice and only shows the requested step", async () => {
    const user = userEvent.setup();
    render(<ConnectionSetup layout="settings" mcpOauthEnabled={false} />);
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
    const user = userEvent.setup(); render(<ConnectionSetup mcpOauthEnabled={false} />);
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
    const user = userEvent.setup(); render(<ConnectionSetup mcpOauthEnabled={false} />); await setup(user);
    fireEvent.change(screen.getByLabelText("Connection key name"), { target: { value: "laptop" } });
    const form = screen.getByLabelText("Connection key name").closest("form")!;
    await waitFor(() => expect(screen.getByRole("button", { name: "Create connection key" })).toBeEnabled());
    fireEvent.submit(form); fireEvent.submit(form);
    expect(issuePat).toHaveBeenCalledTimes(1);
    await act(async () => reject(new Error("Token creation unavailable")));
    expect(screen.getByRole("alert")).toHaveTextContent("We could not confirm whether the key was created");
    expect(screen.getByLabelText("Connection key name")).toHaveValue("laptop");
  });

  it("guides browser sign-in without minting a key", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup mcpOauthEnabled />); await setup(user);
    expect(screen.getByRole("radio", { name: "Sign in with AKB" })).toBeChecked();
    expect(screen.queryByLabelText("Connection key name")).toBeNull();
    expect(screen.getByText(/--transport http/)).toBeVisible();
    expect(screen.getByText(/Open Claude Code, enter \/mcp/)).toBeVisible();
    expect(issuePat).not.toHaveBeenCalled();
  });

  it("keeps one tab stop per group and separates method from key preparation", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup mcpOauthEnabled />);
    expect(screen.getByRole("radio", { name: "Claude Code" })).toHaveAttribute("tabindex", "0");
    await user.click(screen.getByRole("radio", { name: "VS Code" }));
    expect(screen.getByRole("radio", { name: "VS Code" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "Claude Code" })).toHaveAttribute("tabindex", "-1");
    await setup(user, "VS Code"); await method(user);
    expect(screen.getByRole("radio", { name: "Use a connection key" })).toHaveAttribute("tabindex", "0");
    await user.click(screen.getByRole("radio", { name: "Use an existing key" }));
    expect(screen.getByRole("radio", { name: "Use an existing key" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "Sign in with AKB" })).not.toBeChecked();
  });

  it("rejects prefixes and shell text instead of putting them in a command", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup mcpOauthEnabled={false} />); await setup(user);
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
    const user = userEvent.setup(); render(<ConnectionSetup mcpOauthEnabled />); await setup(user); await method(user);
    await user.type(screen.getByLabelText("Connection key name"), "retained draft");
    await backToTool(user); await user.click(screen.getByRole("radio", { name: "Cursor" })); await setup(user, "Cursor");
    expect(screen.getByRole("radio", { name: "Use a connection key" })).toBeChecked();
    expect(screen.getByLabelText("Connection key name")).toHaveValue("retained draft");
  });

  it("keeps a created key in the configuration across step navigation and hiding", async () => {
    vi.mocked(issuePat).mockResolvedValue(fresh);
    const user = userEvent.setup(); render(<ConnectionSetup mcpOauthEnabled={false} />); await setup(user);
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
    render(<QuickstartDialog open onOpenChange={onOpenChange} mcpOauthEnabled={false} />); await setup(user);
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
    const user = userEvent.setup(); const { rerender } = render(<ConnectionSetup mcpOauthEnabled={false} />); await setup(user);
    await user.type(screen.getByLabelText("Connection key name"), "laptop"); await user.click(screen.getByRole("button", { name: "Create connection key" }));
    await screen.findByText("Connection key created");
    rerender(<ConnectionSetup mcpOauthEnabled={false} invalidatedTokenId="other-id" />);
    expect(screen.getByText(/npx akb-mcp/)).toHaveTextContent("akb_fresh_secret");
    rerender(<ConnectionSetup mcpOauthEnabled={false} invalidatedTokenId="fresh-id" />);
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("This setup token was revoked");
    rerender(<ConnectionSetup mcpOauthEnabled={false} invalidatedTokenId="third-id" />);
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
  });

  it("clears pasted keys after revocation because their ID is unknown", async () => {
    const user = userEvent.setup(); const { rerender } = render(<ConnectionSetup mcpOauthEnabled={false} />); await setup(user);
    await user.click(screen.getByRole("radio", { name: "Use an existing key" }));
    await user.type(screen.getByLabelText("Full connection key"), "akb_saved_secret");
    rerender(<ConnectionSetup mcpOauthEnabled={false} invalidatedTokenId="revoked-id" />);
    expect(screen.getByLabelText("Full connection key")).toHaveValue("");
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your current full token again");
  });

  it("explains the provided Codex preset without claiming the client lacks OAuth", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup mcpOauthEnabled />);
    await user.click(screen.getByRole("radio", { name: "Codex" })); await setup(user, "Codex");
    expect(screen.queryByRole("radio", { name: "Sign in with AKB" })).toBeNull();
    await user.type(screen.getByLabelText("Connection key name"), "retained draft");
    await backToTool(user); await user.click(screen.getByRole("radio", { name: "Claude Code" })); await setup(user);
    expect(screen.getByRole("radio", { name: "Sign in with AKB" })).toBeChecked();
    await method(user); expect(screen.getByLabelText("Connection key name")).toHaveValue("retained draft");
    expect(issuePat).not.toHaveBeenCalled();
  });

  it("labels manual confirmation honestly and resets it when configuration changes", async () => {
    const user = userEvent.setup(); render(<ConnectionSetup mcpOauthEnabled />); await setup(user);
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
