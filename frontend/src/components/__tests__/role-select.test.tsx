import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { RoleSelect } from "../role-select";
import * as api from "@/lib/api";

vi.mock("@/lib/api", () => ({ grantAccess: vi.fn() }));

afterEach(cleanup);

const baseMember = {
  username: "alice",
  display_name: "Alice",
  email: "alice@t.dev",
  role: "reader" as const,
  since: null,
};

describe("RoleSelect", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders three role options", () => {
    render(<RoleSelect vault="v" member={baseMember} onChanged={() => {}} />);
    const select = screen.getByLabelText(/change role for alice/i) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      "reader", "writer", "admin",
    ]);
    expect(select.value).toBe("reader");
  });

  it("calls grantAccess on change and reports prev/next", async () => {
    (api.grantAccess as any).mockResolvedValue({});
    const onChanged = vi.fn();
    render(<RoleSelect vault="v" member={baseMember} onChanged={onChanged} />);
    fireEvent.change(screen.getByLabelText(/change role for alice/i), {
      target: { value: "writer" },
    });
    await waitFor(() => expect(api.grantAccess).toHaveBeenCalledWith("v", "alice", "writer"));
    expect(onChanged).toHaveBeenCalledWith("reader", "writer");
  });

  it("surfaces inline error on rejection", async () => {
    (api.grantAccess as any).mockRejectedValue(new Error("boom"));
    render(<RoleSelect vault="v" member={baseMember} onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/change role for alice/i), {
      target: { value: "writer" },
    });
    expect(await screen.findByText(/boom/i)).toBeInTheDocument();
  });

  it("ignores same-value change", () => {
    render(<RoleSelect vault="v" member={baseMember} onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/change role for alice/i), {
      target: { value: "reader" },
    });
    expect(api.grantAccess).not.toHaveBeenCalled();
  });
});
