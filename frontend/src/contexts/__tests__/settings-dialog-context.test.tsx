import { Suspense, useEffect, useState } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Link, MemoryRouter, Route, Routes, useNavigate, useSearchParams } from "react-router-dom";
import { SettingsDialogProvider, useSettingsDialog } from "../settings-dialog-context";

vi.mock("@/pages/settings/settings-dialog", () => ({
  default: function FakeSettingsDialog({ initialTab, onClose }: { initialTab: string; onClose: () => void }) {
    // Like the real controller, the initial section is seeded once per dialog.
    const [tab] = useState(initialTab);
    return <div role="dialog" aria-label="Settings"><span>Section: {tab}</span><Link to="/vault/example/doc/same">Open watched document</Link><button onClick={onClose}>Close settings</button></div>;
  },
}));

function CompatibilityRoute() {
  const { openSettings } = useSettingsDialog()!;
  const [search] = useSearchParams();
  const tab = search.get("tab") ?? "profile";
  useEffect(() => { openSettings(tab); }, [openSettings, tab]);
  return <div>Compatibility home</div>;
}
function Workspace() {
  const { openSettings } = useSettingsDialog()!;
  const navigate = useNavigate();
  return <><button onClick={() => openSettings("profile")}>Open settings</button><button onClick={() => navigate(-1)}>Go back</button><Routes><Route path="/settings" element={<CompatibilityRoute />} /><Route path="*" element={<input aria-label="Workspace draft" defaultValue="Retained" />} /></Routes></>;
}
function setup(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><SettingsDialogProvider identity="same-user"><Suspense><Workspace /></Suspense></SettingsDialogProvider></MemoryRouter>);
}
afterEach(cleanup);

describe("settings dialog routing", () => {
  it("dismisses when following a watched link to the already-open document without remounting it", async () => {
    const user = userEvent.setup();
    setup("/vault/example/doc/same");
    const draft = screen.getByLabelText("Workspace draft");
    await user.type(draft, " work");
    await user.click(screen.getByRole("button", { name: "Open settings" }));
    await screen.findByText("Section: profile");
    await user.click(screen.getByRole("link", { name: "Open watched document" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Workspace draft")).toBe(draft);
    expect(draft).toHaveValue("Retained work");
  });

  it("Back from an open modal honors the previous compatibility deep link's section", async () => {
    const user = userEvent.setup();
    setup("/settings?tab=notifications");
    await screen.findByText("Section: notifications");
    await user.click(screen.getByRole("link", { name: "Open watched document" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Open settings" }));
    await screen.findByText("Section: profile");
    await user.click(screen.getByRole("button", { name: "Go back" }));
    await screen.findByText("Section: notifications");
    expect(screen.queryByText("Section: profile")).not.toBeInTheDocument();
  });
});
