import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { AdminResetPasswordDialog } from "../admin-reset-password-dialog";
import * as api from "@/lib/api";

vi.mock("@/lib/api", () => ({ adminResetPassword: vi.fn() }));

afterEach(cleanup);

describe("AdminResetPasswordDialog", () => {
  beforeEach(() => vi.clearAllMocks());

  it("calls adminResetPassword on Generate and surfaces temp password", async () => {
    (api.adminResetPassword as any).mockResolvedValue({
      temporary_password: "Abcd-1234-EfGh",
      username: "alice",
    });
    render(
      <AdminResetPasswordDialog userId="u1" username="alice" open onOpenChange={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /generate/i }));
    const pre = await screen.findByTestId("temp-password");
    expect(pre.textContent).toBe("Abcd-1234-EfGh");
    expect(api.adminResetPassword).toHaveBeenCalledWith("u1");
  });

  it("renders error inline when adminResetPassword rejects", async () => {
    (api.adminResetPassword as any).mockRejectedValue(new Error("boom"));
    render(
      <AdminResetPasswordDialog userId="u1" username="alice" open onOpenChange={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /generate/i }));
    expect(await screen.findByText(/boom/i)).toBeInTheDocument();
  });

  it("Copy button writes temp password to clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText }, configurable: true, writable: true,
    });
    (api.adminResetPassword as any).mockResolvedValue({
      temporary_password: "Xyz1-Wow2-Yes3", username: "bob",
    });
    render(
      <AdminResetPasswordDialog userId="u2" username="bob" open onOpenChange={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /generate/i }));
    await screen.findByTestId("temp-password");
    const btn = screen.getByRole("button", { name: /copy temporary password/i });
    btn.click();
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Xyz1-Wow2-Yes3"));
  });
});
