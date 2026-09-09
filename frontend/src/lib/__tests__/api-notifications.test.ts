import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/api";
import { notificationCount, notificationPage, NotificationsUnavailable, NotificationCategoriesUnavailable, NotificationConflict, markNotificationSnapshot, documentSubscription } from "@/lib/api-notifications";

vi.mock("@/lib/api", () => ({ authenticatedFetch: vi.fn() }));
afterEach(() => vi.clearAllMocks());

describe("notification API compatibility", () => {
  it("refreshes watching feeds only after a successful subscription mutation", async () => {
    const refresh = vi.fn();
    window.addEventListener("akb:watch-changed", refresh);
    try {
      vi.mocked(authenticatedFetch).mockImplementation(async () => new Response(JSON.stringify({ subscribed: false, resource_id: "id" })));
      await documentSubscription("akb://work/doc/id");
      expect(refresh).not.toHaveBeenCalled();
      await documentSubscription("akb://work/doc/id", "DELETE");
      expect(refresh).toHaveBeenCalledTimes(1);
      vi.mocked(authenticatedFetch).mockRejectedValueOnce(new Error("offline"));
      await expect(documentSubscription("akb://work/doc/id", "PUT")).rejects.toThrow();
      expect(refresh).toHaveBeenCalledTimes(1);
    } finally { window.removeEventListener("akb:watch-changed", refresh); }
  });
  it("sends the category with the unread predicate and cursor", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response(JSON.stringify({ supported: true, unread_count: 1, snapshot: "3", items: [], category: "documents" })));
    await notificationPage("unread", "2", undefined, "documents");
    expect(authenticatedFetch).toHaveBeenCalledWith("/api/v1/notifications?state=unread&limit=20&category=documents&cursor=2", expect.anything());
  });
  it("rejects silently ignored category filters but preserves the legacy all view", async () => {
    const legacy = { supported: true, unread_count: 1, snapshot: "3", items: [] };
    vi.mocked(authenticatedFetch).mockImplementation(async () => new Response(JSON.stringify(legacy)));
    await expect(notificationPage("all", undefined, undefined, "access")).rejects.toBeInstanceOf(NotificationCategoriesUnavailable);
    await expect(notificationPage("all")).resolves.toEqual(legacy);
  });
  it("distinguishes a disabled feature from a temporary service outage", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response(JSON.stringify({ code: "notifications_disabled" }), { status: 503 }));
    await expect(notificationCount()).rejects.toBeInstanceOf(NotificationsUnavailable);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response(JSON.stringify({ code: "service_unavailable" }), { status: 503 }));
    await expect(notificationCount()).rejects.not.toBeInstanceOf(NotificationsUnavailable);
  });
  it("does not treat an older server's unrelated 200 response as an empty inbox", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response(JSON.stringify({ items: [] })));
    await expect(notificationPage("all")).rejects.toBeInstanceOf(NotificationsUnavailable);
  });
  it("uses authenticated transport and passes the exact observed snapshot", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response("{}"));
    await markNotificationSnapshot("opaque-snapshot");
    expect(authenticatedFetch).toHaveBeenCalledWith("/api/v1/notifications/mark-read", expect.objectContaining({ method: "POST", body: '{"snapshot":"opaque-snapshot"}' }));
  });
  it("identifies stale reads for refresh without acknowledging a newer version", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response("{}", { status: 409 }));
    await expect(markNotificationSnapshot("old-snapshot")).rejects.toBeInstanceOf(NotificationConflict);
  });
});
