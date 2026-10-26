import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { UserAvatar, userInitials } from "../user-avatar";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { clearPrivateAssetCache, configureAuthTransport, setToken, type CurrentUser } from "@/lib/api";

const REVISION = "22cc6a5f-6eba-4d7c-8500-d1c6518e2a8c";
const USER: CurrentUser = { user_id: "account-a", username: "alice", email: "alice@example.test", display_name: "Alice Example", is_admin: false, auth_method: "jwt", key_class: null, avatar: { revision: REVISION, url: `/api/v1/auth/me/avatar?v=${REVISION}` } };
const image = () => new Response("normalized", { headers: { "Content-Type": "image/webp" } });

describe("shared UserAvatar", () => {
  const fetchMock = vi.fn();
  const createObjectURL = vi.fn(); const revokeObjectURL = vi.fn();
  beforeEach(() => {
    localStorage.clear(); configureAuthTransport("local"); setToken("account-a-session");
    fetchMock.mockReset(); createObjectURL.mockReset().mockReturnValue("blob:avatar-a"); revokeObjectURL.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL, revokeObjectURL }));
  });
  afterEach(() => { cleanup(); clearPrivateAssetCache(); setToken(null); configureAuthTransport(null); vi.unstubAllGlobals(); });

  it.each([
    ["임근우", "임근"], ["Alice Example", "AE"], ["  A\u0301lice  Example ", "A\u0301E"], ["👩‍💻 Works", "👩‍💻W"], ["", "?"],
  ])("uses complete, consistent initials for %s", (display_name, expected) => {
    expect(userInitials({ display_name })).toBe(expected);
  });

  it("keeps legacy users and tombstones as fixed-size decorative initials without a request", () => {
    const { container } = render(<><UserAvatar user={{ username: "alice" }} size="sm" /><UserAvatar user={{ ...USER, avatar: { revision: REVISION, url: null } }} size="md" /><UserAvatar user={null} size="lg" /></>);
    const avatars = container.querySelectorAll('[data-slot="user-avatar"]');
    expect(avatars[0]).toHaveClass("h-7", "w-7"); expect(avatars[1]).toHaveClass("h-10", "w-10"); expect(avatars[2]).toHaveClass("h-12", "w-12");
    for (const avatar of avatars) expect(avatar).toHaveAttribute("aria-hidden", "true");
    expect(fetchMock).not.toHaveBeenCalled(); expect(container.querySelector("img")).toBeNull();
  });

  it("shares one private flight and URL across consumers and revokes it after the last release", async () => {
    fetchMock.mockResolvedValueOnce(image());
    const view = render(<CurrentUserProvider user={USER}><UserAvatar user={USER} /><UserAvatar user={USER} size="md" /></CurrentUserProvider>);
    await waitFor(() => expect(view.container.querySelectorAll("img")).toHaveLength(2));
    expect(fetchMock).toHaveBeenCalledOnce(); expect(createObjectURL).toHaveBeenCalledOnce();
    view.rerender(<CurrentUserProvider user={USER}><UserAvatar user={USER} /></CurrentUserProvider>);
    expect(revokeObjectURL).not.toHaveBeenCalled();
    view.unmount(); expect(revokeObjectURL).toHaveBeenCalledExactlyOnceWith("blob:avatar-a");
  });

  it("uses initials on image decoding failure without retrying or showing a broken image", async () => {
    fetchMock.mockResolvedValueOnce(image());
    const view = render(<UserAvatar user={USER} />);
    await waitFor(() => expect(view.container.querySelector("img")).not.toBeNull());
    fireEvent.error(view.container.querySelector("img")!);
    expect(view.container.querySelector("img")).toBeNull(); expect(view.container).toHaveTextContent("AE");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("does not request or expose a photo while identity proof is pending or belongs to another account", async () => {
    const view = render(<CurrentUserProvider user={USER} checking><UserAvatar user={USER} /></CurrentUserProvider>);
    expect(fetchMock).not.toHaveBeenCalled();
    view.rerender(<CurrentUserProvider user={{ ...USER, user_id: "account-b" }}><UserAvatar user={USER} /></CurrentUserProvider>);
    expect(fetchMock).not.toHaveBeenCalled(); expect(view.container.querySelector("img")).toBeNull();
    fetchMock.mockResolvedValueOnce(image());
    view.rerender(<CurrentUserProvider user={USER} revision={1}><UserAvatar user={USER} /></CurrentUserProvider>);
    await waitFor(() => expect(view.container.querySelector("img")).not.toBeNull());
    view.rerender(<CurrentUserProvider user={USER} revision={1} checking><UserAvatar user={USER} /></CurrentUserProvider>);
    expect(view.container.querySelector("img")).toBeNull(); expect(revokeObjectURL).toHaveBeenCalledWith("blob:avatar-a");
  });

  it("revokes the old URL immediately on credential changes and waits for fresh proof", async () => {
    fetchMock.mockResolvedValueOnce(image());
    const view = render(<CurrentUserProvider user={USER}><UserAvatar user={USER} /></CurrentUserProvider>);
    await waitFor(() => expect(view.container.querySelector("img")).not.toBeNull());
    await act(async () => { setToken("new-account-session"); });
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:avatar-a"); expect(view.container.querySelector("img")).toBeNull();
    expect(fetchMock).toHaveBeenCalledOnce();
    fetchMock.mockResolvedValueOnce(image());
    view.rerender(<CurrentUserProvider user={USER} revision={1}><UserAvatar user={USER} /></CurrentUserProvider>);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("aborts a released flight and never publishes its late response", async () => {
    let resolve!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(done => { resolve = done; }));
    const view = render(<UserAvatar user={USER} />);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    view.unmount(); expect(init.signal?.aborted).toBe(true);
    await act(async () => { resolve(image()); });
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("checks identity once for a shared 401 and avoids a revalidation loop", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(new Response(null, { status: 401 })));
    const proof = vi.fn(); window.addEventListener("akb:revalidate-access", proof);
    try {
      const view = render(<CurrentUserProvider user={USER}><UserAvatar user={USER} /><UserAvatar user={USER} size="md" /></CurrentUserProvider>);
      await waitFor(() => expect(proof).toHaveBeenCalledOnce());
      await act(async () => { clearPrivateAssetCache(); });
      view.rerender(<CurrentUserProvider user={USER} revision={1}><UserAvatar user={USER} /><UserAvatar user={USER} size="md" /></CurrentUserProvider>);
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      expect(proof).toHaveBeenCalledOnce(); expect(view.container.querySelector("img")).toBeNull();
    } finally { window.removeEventListener("akb:revalidate-access", proof); }
  });
});
