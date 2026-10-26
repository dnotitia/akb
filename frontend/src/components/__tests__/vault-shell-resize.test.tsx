import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { VaultNavigationToggle } from "@/components/vault-navigation-controls";
import {
  VaultShell,
  type VaultNavigationControl,
} from "@/components/vault-shell";
vi.mock("@/components/vault-explorer", () => ({
  VaultExplorer: () => <nav aria-label="Content explorer">Collection tree</nav>,
}));
vi.mock("@/components/vault-navigation-header", () => ({
  VaultNavigationHeader: ({ navigationControl }: { navigationControl: VaultNavigationControl }) => <><VaultNavigationToggle id="vault-navigation-collapse" {...navigationControl} /><button>Switch vault</button></>,
}));
vi.mock("@/hooks/use-vaults", () => ({
  useVaults: () => ({ vaults: [], refetch: vi.fn() }),
}));
vi.mock("@/components/title-bar", () => ({
  TitleBar: ({ left }: { left: React.ReactNode }) => <header>{left}</header>,
}));
vi.mock("@/components/app-page-location", () => ({
  AppPageLocation: () => null,
}));
vi.mock("@/components/vault-create-dialog", () => ({
  VaultCreateDialog: () => null,
}));
vi.mock("@/components/document-create-dialog", () => ({
  DocumentCreateDialog: () => null,
}));
let workspaceWidth: number,
  desktop: boolean,
  mediaChange: () => void,
  resizeWorkspace: () => void,
  captured: number | null;
beforeEach(() => {
  localStorage.clear();
  workspaceWidth = 1200;
  desktop = true;
  captured = null;
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    get matches() {
      return desktop;
    },
    media: query,
    onchange: null,
    addEventListener: (
      _event: string,
      listener: EventListenerOrEventListenerObject,
    ) => {
      mediaChange = listener as () => void;
    },
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(
    function (this: HTMLElement) {
      return this.hasAttribute("data-navigation-mode") ? workspaceWidth : 0;
    },
  );
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(private callback: () => void) {}
      observe(element: HTMLElement) {
        if (element.hasAttribute("data-navigation-mode")) resizeWorkspace = this.callback;
      }
      disconnect() {}
    },
  );
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
    configurable: true,
    value: vi.fn((id: number) => {
      captured = id;
    }),
  });
  vi.spyOn(HTMLElement.prototype, "hasPointerCapture").mockImplementation(
    (id) => captured === id,
  );
  vi.spyOn(HTMLElement.prototype, "releasePointerCapture").mockImplementation(
    () => {
      captured = null;
    },
  );
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
});
function Layout() { return <Outlet context={{ minimumVaultWorkspaceWidth: 656 }} />; }

function mount() {
  return render(
    <MemoryRouter initialEntries={["/vault/demo"]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/vault/:name" element={<VaultShell />}>
            <Route index element={<p>Vault content</p>} />
          </Route>
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}
function mode() {
  return document
    .querySelector("[data-navigation-mode]")
    ?.getAttribute("data-navigation-mode");
}
function drag(handle: HTMLElement, delta: number, release = true) {
  fireEvent.pointerDown(handle, {
    pointerId: 1,
    button: 0,
    isPrimary: true,
    clientX: 200,
  });
  fireEvent.pointerMove(handle, { pointerId: 1, clientX: 200 + delta });
  if (release) fireEvent.pointerUp(handle, { pointerId: 1 });
}
describe("Focused navigation resizing", () => {
  it("resizes one context panel independently of legacy column preferences", () => {
    localStorage.setItem("akb.vaultRailWidth.v2", "310");
    localStorage.setItem("akb.treeWidth.v2", "470");
    mount();
    const handle = screen.getByRole("separator", {
      name: "Resize vault navigation",
    });
    expect(handle).toHaveAttribute("aria-valuemin", "224");
    expect(handle).toHaveAttribute("aria-valuemax", "360");
    drag(handle, 32);
    expect(handle).toHaveAttribute("aria-valuenow", "288");
    expect(document.getElementById("vault-workspace-navigation")).toHaveStyle({
      width: "288px",
    });
    expect(localStorage.getItem("akb.focusedNavigationWidth.v1")).toBe("288");
    expect(localStorage.getItem("akb.vaultRailWidth.v2")).toBe("310");
    expect(localStorage.getItem("akb.treeWidth.v2")).toBe("470");
  });
  it("retains capture until release and keeps the resulting overlay open", () => {
    workspaceWidth = 920;
    mount();
    expect(mode()).toBe("inline");
    const handle = screen.getByRole("separator", {
      name: "Resize vault navigation",
    });
    drag(handle, 80, false);
    expect(mode()).toBe("inline");
    expect(captured).toBe(1);
    expect(handle.isConnected).toBe(true);
    fireEvent.pointerUp(handle, { pointerId: 1 });
    expect(mode()).toBe("overlay");
    expect(
      screen.getByRole("separator", { name: "Resize vault navigation" }),
    ).toBe(handle);
    expect(document.body.style.cursor).toBe("");
    expect(captured).toBeNull();
  });
  it("can narrow the overlay back into inline presentation", () => {
    workspaceWidth = 920;
    localStorage.setItem("akb.focusedNavigationWidth.v1", "340");
    mount();
    expect(mode()).toBe("overlay");
    fireEvent.click(screen.getByRole("button", { name: "Open vault navigation" }));
    const handle = screen.getByRole("separator", {
      name: "Resize vault navigation",
    });
    drag(handle, -84);
    expect(handle).toHaveAttribute("aria-valuenow", "256");
    expect(mode()).toBe("inline");
  });
  it("preserves desktop width through mobile presentation", () => {
    localStorage.setItem("akb.focusedNavigationWidth.v1", "288");
    mount();
    act(() => {
      desktop = false;
      mediaChange();
    });
    expect(screen.queryByRole("separator")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Open vault navigation" }),
    );
    expect(
      screen.getByRole("button", { name: "Close vault navigation" }),
    ).toHaveAttribute("id", "vault-navigation-collapse");
    act(() => {
      desktop = true;
      workspaceWidth = 1400;
      mediaChange();
      resizeWorkspace();
    });
    expect(document.getElementById("vault-workspace-navigation")).toHaveStyle({
      width: "288px",
    });
    expect(localStorage.getItem("akb.focusedNavigationWidth.v1")).toBe("288");
  });
  it("keeps a visible focus target when responsive reflow hides navigation", () => {
    mount();
    screen.getByRole("button", { name: "Switch vault" }).focus();
    act(() => { desktop = false; mediaChange(); });
    expect(screen.getByRole("button", { name: "Open vault navigation" })).toHaveFocus();
  });

  it("cleans up capture when changing to the mobile viewport", () => {
    mount();
    const handle = screen.getByRole("separator", {
      name: "Resize vault navigation",
    });
    drag(handle, 20, false);
    expect(document.body.style.cursor).toBe("col-resize");
    act(() => {
      desktop = false;
      mediaChange();
    });
    expect(screen.queryByRole("separator")).not.toBeInTheDocument();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    expect(captured).toBeNull();
  });
  it("offers keyboard resize bounds without changing visibility preferences", () => {
    mount();
    const handle = screen.getByRole("separator", {
      name: "Resize vault navigation",
    });
    fireEvent.keyDown(handle, { key: "End" });
    expect(handle).toHaveAttribute("aria-valuenow", "360");
    fireEvent.keyDown(handle, { key: "Home" });
    expect(handle).toHaveAttribute("aria-valuenow", "224");
    expect(localStorage.getItem("akb.treeVisible")).toBeNull();
  });
});
