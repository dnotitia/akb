import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createPath, Router } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import { SkillSection } from "../skill-section";

vi.mock("../guide-preview", () => ({ default: () => <p>Saved guide preview</p> }));

function requestView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const replace = vi.fn();
  const navigator = {
    createHref: (to: Parameters<typeof createPath>[0] | string) => typeof to === "string" ? to : createPath(to),
    go: vi.fn(), push: vi.fn(), replace,
  };
  const view = (key: string, props: Partial<ComponentProps<typeof SkillSection>> = {}) => <QueryClientProvider client={client}>
    <Router navigator={navigator} location={{ pathname: "/vault/v/settings", search: "?edit=guide", hash: "#skill", state: null, key }}>
      <SkillSection vault="v" doc={{ content: "Saved guide body", current_commit: "original" }} canManage {...props} />
    </Router>
  </QueryClientProvider>;
  return { view, replace };
}

describe("guide edit requests", () => {
  it("consumes an edit entry once while its URL cleanup is still pending", async () => {
    const user = userEvent.setup();
    const { view, replace } = requestView();

    // History replacement can precede the router's location-context commit.
    // Hold that old context until after Cancel, rather than relying on timing.
    const { rerender } = render(view("first-edit-entry"));
    expect(await screen.findByLabelText("Vault guide body")).toHaveValue("Saved guide body");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByLabelText("Vault guide body")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit guide" })).toHaveFocus());
    expect(replace).toHaveBeenCalledTimes(1);

    // A later explicit request is a new entry and must still open the editor.
    rerender(view("second-edit-entry"));
    expect(await screen.findByLabelText("Vault guide body")).toHaveValue("Saved guide body");
    expect(replace).toHaveBeenCalledTimes(2);
  });

  it.each([{ loading: true }, { canManage: false }, { doc: undefined }])("waits for readiness before consuming %j", async props => {
    const { view, replace } = requestView();
    const { rerender } = render(view("pending-entry", props));
    expect(screen.queryByLabelText("Vault guide body")).not.toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
    rerender(view("pending-entry"));
    expect(await screen.findByLabelText("Vault guide body")).toHaveValue("Saved guide body");
    expect(replace).toHaveBeenCalledTimes(1);
  });
});
