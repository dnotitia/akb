import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionSetup } from "../connection-setup";
import { QuickstartDialog } from "../quickstart-dialog";
import { createPAT } from "@/lib/api";

vi.mock("@/lib/api", () => ({ createPAT: vi.fn() }));

beforeEach(() => vi.clearAllMocks());

describe("Connection setup", () => {
  it("does not create credentials or offer a placeholder config on open", () => {
    render(<ConnectionSetup mcpOauthEnabled={false} />);
    expect(createPAT).not.toHaveBeenCalled();
    expect(screen.getByLabelText("AI tool")).toBeVisible();
    expect(screen.getByLabelText("Token name")).toBeVisible();
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    expect(screen.queryByText(/This browser cannot verify/)).toBeNull();
    expect(screen.queryByText("3. Try it in your agent")).toBeNull();
  });

  it("guards repeated submission and keeps failure recoverable", async () => {
    let reject!: (error: Error) => void;
    vi.mocked(createPAT).mockImplementation(() => new Promise((_, no) => { reject = no; }));
    render(<ConnectionSetup mcpOauthEnabled={false} />);
    fireEvent.change(screen.getByLabelText("Token name"), { target: { value: "laptop" } });
    const form = screen.getByLabelText("Token name").closest("form")!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(createPAT).toHaveBeenCalledTimes(1);
    await act(async () => reject(new Error("Token creation unavailable")));
    expect(screen.getByRole("alert")).toHaveTextContent("Token creation unavailable");
    expect(screen.getByLabelText("Token name")).toHaveValue("laptop");
  });

  it("allows browser sign-in without minting a token", async () => {
    render(<ConnectionSetup mcpOauthEnabled />);
    expect(screen.getByLabelText("Sign-in method")).toHaveTextContent("Browser sign-in (OAuth)");
    expect(screen.queryByLabelText("Token name")).toBeNull();
    expect(screen.getByText(/--transport http/)).toBeVisible();
    expect(screen.getByText(/This browser cannot verify/)).toBeVisible();
    expect(createPAT).not.toHaveBeenCalled();
  });

  it("rejects a token prefix instead of putting it into a command", async () => {
    const user = userEvent.setup();
    render(<ConnectionSetup mcpOauthEnabled={false} />);
    await user.click(screen.getByLabelText("Access token"));
    await user.click(screen.getByRole("menuitemradio", { name: "Use a saved token" }));
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
    expect(screen.getByText("3. Try it in your agent")).toBeVisible();
    expect(createPAT).not.toHaveBeenCalled();
  });

  it("preserves an explicit token choice when the client changes", async () => {
    const user = userEvent.setup();
    render(<ConnectionSetup mcpOauthEnabled />);
    await user.click(screen.getByLabelText("Sign-in method"));
    await user.click(screen.getByRole("menuitemradio", { name: "Access token" }));
    await user.click(screen.getByLabelText("AI tool"));
    await user.click(screen.getByRole("menuitemradio", { name: "Cursor" }));
    expect(screen.getByLabelText("Sign-in method")).toHaveTextContent("Access token");
    expect(screen.getByLabelText("Token name")).toBeVisible();
    expect(screen.queryByText("3. Try it in your agent")).toBeNull();
    expect(screen.getByText(/read or change content within your existing vault permissions/)).toBeVisible();
  });

  it("keeps a new secret available until closing is acknowledged", async () => {
    vi.mocked(createPAT).mockResolvedValue({ token: "akb_fresh_secret", token_id: "t1", name: "laptop", prefix: "akb_fresh" });
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
    vi.mocked(createPAT).mockResolvedValue({ token: "akb_fresh_secret", token_id: "fresh-id" });
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
    await user.click(screen.getByLabelText("Access token"));
    await user.click(screen.getByRole("menuitemradio", { name: "Use a saved token" }));
    await user.type(screen.getByLabelText("Full saved token"), "akb_saved_secret");
    expect(screen.getByText(/npx akb-mcp/)).toBeVisible();
    rerender(<ConnectionSetup mcpOauthEnabled={false} invalidatedTokenId="revoked-id" />);
    expect(screen.getByLabelText("Full saved token")).toHaveValue("");
    expect(screen.queryByText(/npx akb-mcp/)).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your current full token again");
  });
});
