import { render, screen, cleanup } from "@testing-library/react";
import { describe, it, expect, afterEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import AuthForgotPage from "../auth-forgot";

afterEach(cleanup);

describe("AuthForgotPage", () => {
  it("renders heading + admin-contact guidance + back-to-login link", () => {
    render(
      <MemoryRouter>
        <AuthForgotPage />
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: /forgot your password/i })).toBeInTheDocument();
    expect(screen.getByText(/contact your administrator/i)).toBeInTheDocument();
    const back = screen.getByRole("link", { name: /back to login/i });
    expect(back).toHaveAttribute("href", "/auth");
  });
});
