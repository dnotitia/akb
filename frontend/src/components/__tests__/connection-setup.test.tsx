import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionSetup } from "../connection-setup";
import { QuickstartDialog } from "../quickstart-dialog";
import { configureAuthTransport, setToken } from "@/lib/api";
import { issuePat, getPatCapabilities } from "@/lib/api-pat-issuance";

vi.mock("@/lib/api-pat-issuance", async () => ({ ...await vi.importActual<typeof import("@/lib/api-pat-issuance")>("@/lib/api-pat-issuance"), issuePat: vi.fn(), getPatCapabilities: vi.fn() }));

beforeEach(() => { vi.clearAllMocks(); configureAuthTransport("local"); setToken("fixture-session"); vi.mocked(getPatCapabilities).mockResolvedValue({ contract_version: 1, user_id: "00000000-0000-0000-0000-000000000001", name_max_length: 255, permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"], vault_scope_semantics: "write_restriction_sql_read_write" }); });

describe("Connection setup", () => {
  it("does not create credentials or offer a placeholder config on open", () => {
    render(<ConnectionSetup mcpOauthEnabled={false} />);
    expect(issuePat).not.toHaveBeenCalled();
    expect(screen.getByRole("group", { name: "AI tool" })).toBeVisible();
    expect(screen.getByRole("radio", { name: "Claude Code" })).toBeChecked();
    expect(screen.getByLabelText("Token name")).toBeVisible();
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    expect(screen.getByText(/This browser cannot verify/)).toBeVisible();
    expect(screen.getAllByRole("heading", { level: 3 }).map(heading => heading.textContent)).toEqual(["1. Prepare access", "2. Configure Claude Code", "3. Try it in your agent"]);
    expect(screen.queryByRole("menuitemradio")).toBeNull();
  });

  it("guards repeated submission and keeps failure recoverable", async () => {
    let reject!: (error: Error) => void;
    vi.mocked(issuePat).mockImplementation(() => new Promise((_, no) => { reject = no; }));
    render(<ConnectionSetup mcpOauthEnabled={false} />);
    fireEvent.change(screen.getByLabelText("Token name"), { target: { value: "laptop" } });
    const form = screen.getByLabelText("Token name").closest("form")!;
    await waitFor(() => expect(screen.getByRole("button", { name: "Create token" })).toBeEnabled());
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(issuePat).toHaveBeenCalledTimes(1);
    await act(async () => reject(new Error("Token creation unavailable")));
    expect(screen.getByRole("alert")).toHaveTextContent("Creation result needs checking");
    expect(screen.getByLabelText("Token name")).toHaveValue("laptop");
  });

  it("allows browser sign-in without minting a token", async () => {
    render(<ConnectionSetup mcpOauthEnabled />);
    expect(screen.getByRole("radio", { name: "Browser sign-in" })).toBeChecked();
    expect(screen.queryByLabelText("Token name")).toBeNull();
    expect(screen.getByText(/--transport http/)).toBeVisible();
    expect(screen.getByText(/This browser cannot verify/)).toBeVisible();
    expect(issuePat).not.toHaveBeenCalled();
  });

  it("keeps one tab stop per choice group as the selected option changes", async () => {
    const user = userEvent.setup();
    render(<ConnectionSetup mcpOauthEnabled />);
    expect(screen.getByRole("radio", { name: "Claude Code" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "VS Code" })).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("radio", { name: "Browser sign-in" })).toHaveAttribute("tabindex", "0");
    await user.click(screen.getByRole("radio", { name: "VS Code" }));
    expect(screen.getByRole("radio", { name: "VS Code" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "Claude Code" })).toHaveAttribute("tabindex", "-1");
    await user.click(screen.getByRole("radio", { name: "Use a saved token" }));
    expect(screen.getByRole("radio", { name: "Use a saved token" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "Browser sign-in" })).toHaveAttribute("tabindex", "-1");
  });

  it("rejects a token prefix instead of putting it into a command", async () => {
    const user = userEvent.setup();
    render(<ConnectionSetup mcpOauthEnabled={false} />);
    await user.click(screen.getByRole("radio", { name: "Use a saved token" }));
    await user.type(screen.getByLabelText("Full saved token"), "akb_prefix…");
    expect(screen.getByRole("alert")).toHaveTextContent("Enter the full token");
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    await user.clear(screen.getByLabelText("Full saved token"));
    await user.type(screen.getByLabelText("Full saved token"), "akb_$(command)");
    expect(screen.getByRole("alert")).toHaveTextContent("only letters, numbers");
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    await user.clear(screen.getByLabelText("Full saved token"));
    await user.type(screen.getByLabelText("Full saved token"), "akb_complete_saved_secret");
    expect(screen.getByText(/npx akb-mcp/)).toHaveTextContent("akb_complete_saved_secret");
    expect(screen.getByRole("heading", { name: "3. Try it in your agent" })).toBeVisible();
    expect(issuePat).not.toHaveBeenCalled();
  });

  it("preserves an explicit token choice when the client changes", async () => {
    const user = userEvent.setup();
    render(<ConnectionSetup mcpOauthEnabled />);
    await user.click(screen.getByRole("radio", { name: "Create a new token" }));
    await user.click(screen.getByRole("radio", { name: "Cursor" }));
    expect(screen.getByRole("radio", { name: "Create a new token" })).toBeChecked();
    expect(screen.getByLabelText("Token name")).toBeVisible();
    expect(screen.getByRole("heading", { name: "3. Try it in your agent" })).toBeVisible();
    expect(screen.getByText(/Your account permissions still apply/)).toBeVisible();
  });

  it("keeps a new secret available until closing is acknowledged", async () => {
    vi.mocked(issuePat).mockResolvedValue({ token: "akb_fresh_secret", token_id: "t1", name: "laptop", prefix: "akb_fresh", verified: true });
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(<QuickstartDialog open onOpenChange={onOpenChange} mcpOauthEnabled={false} />);
    await user.type(screen.getByLabelText("Token name"), "laptop");
    await user.click(screen.getByRole("button", { name: "Create token" }));
    expect(await screen.findByText("Token created — save it now")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(await screen.findByText("Have you saved your token?")).toBeVisible();
    expect(onOpenChange).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "I've saved it — close" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("invalidates only a matching freshly created token", async () => {
    vi.mocked(issuePat).mockResolvedValue({ token: "akb_fresh_secret", token_id: "fresh-id", name: "laptop", prefix: "akb_fresh", verified: true });
    const user = userEvent.setup();
    const { rerender } = render(<ConnectionSetup mcpOauthEnabled={false} />);
    await user.type(screen.getByLabelText("Token name"), "laptop");
    await user.click(screen.getByRole("button", { name: "Create token" }));
    expect(await screen.findByText("Token created — save it now")).toBeVisible();
    rerender(<ConnectionSetup mcpOauthEnabled={false} invalidatedTokenId="other-id" />);
    expect(screen.getByText(/npx akb-mcp/)).toHaveTextContent("akb_fresh_secret");
    rerender(<ConnectionSetup mcpOauthEnabled={false} invalidatedTokenId="fresh-id" />);
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    expect(screen.queryByText("akb_fresh_secret")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("This setup token was revoked");
    rerender(<ConnectionSetup mcpOauthEnabled={false} invalidatedTokenId="third-id" />);
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
  });

  it("clears a pasted token after revocation because its ID is unknown", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ConnectionSetup mcpOauthEnabled={false} />);
    await user.click(screen.getByRole("radio", { name: "Use a saved token" }));
    await user.type(screen.getByLabelText("Full saved token"), "akb_saved_secret");
    expect(screen.getByText(/npx akb-mcp/)).toBeVisible();
    rerender(<ConnectionSetup mcpOauthEnabled={false} invalidatedTokenId="revoked-id" />);
    expect(screen.getByLabelText("Full saved token")).toHaveValue("");
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your current full token again");
  });

  it("falls back to token setup for tools without OAuth support and restores browser sign-in", async () => {
    const user = userEvent.setup();
    render(<ConnectionSetup mcpOauthEnabled />);
    await user.click(screen.getByRole("radio", { name: "Codex" }));
    expect(screen.queryByRole("radio", { name: "Browser sign-in" })).toBeNull();
    expect(screen.getByLabelText("Token name")).toBeVisible();
    expect(screen.queryByText(/codex mcp add/)).toBeNull();
    await user.type(screen.getByLabelText("Token name"), "retained draft");
    await user.click(screen.getByRole("radio", { name: "Claude Code" }));
    expect(screen.getByRole("radio", { name: "Browser sign-in" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Create a new token" }));
    expect(screen.getByLabelText("Token name")).toHaveValue("retained draft");
    expect(issuePat).not.toHaveBeenCalled();
  });
});
