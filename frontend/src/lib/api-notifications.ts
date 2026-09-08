import { authenticatedFetch } from "@/lib/api";

export interface NotificationItem {
  id: string;
  kind: string;
  title: string;
  message: string;
  created_at: string;
  updated_at: string;
  read: boolean;
  version: string;
  target: { uri: string; vault: string } | null;
}
export interface NotificationSummary {
  supported: true;
  unread_count: number;
  snapshot: string;
  retention_days: number;
}
export interface NotificationPage extends NotificationSummary {
  category?: NotificationCategory;
  items: NotificationItem[];
  next_cursor: string | null;
}
export type NotificationCategory = "all" | "documents" | "access";
export interface DocumentSubscription {
  resource_id: string;
  uri: string;
  title: string;
  vault: string;
}
export class NotificationsUnavailable extends Error {}
export class NotificationCategoriesUnavailable extends NotificationsUnavailable {}
export class NotificationConflict extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(`/api/v1/${path}`, init);
  if ([404, 405, 501].includes(response.status)) {
    throw new NotificationsUnavailable("Notifications are not available on this server.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const code = body?.error?.code ?? body?.detail?.code ?? body?.code;
    if (code === "notifications_disabled") throw new NotificationsUnavailable("Notifications are turned off on this server.");
    if (code === "notifications_session_required") throw new NotificationsUnavailable("Sign in with your personal account to use notifications.");
    if (response.status === 409) throw new NotificationConflict("This notification changed. We refreshed it; try again.");
    throw new Error("Could not update notifications. Try again.");
  }
  const value = await response.json();
  if (value.supported === false) throw new NotificationsUnavailable("Notifications are turned off on this server.");
  return value as T;
}

export function notificationCount(signal?: AbortSignal) {
  return request<NotificationSummary>("notifications/unread-count", { signal }).then(validateSummary);
}
function validateSummary<T extends NotificationSummary>(value: T): T {
  if (value.supported !== true || typeof value.unread_count !== "number" || typeof value.snapshot !== "string") {
    throw new NotificationsUnavailable("Notifications are not available on this server.");
  }
  return value;
}
export function notificationPage(state: "all" | "unread", cursor?: string, signal?: AbortSignal, category: NotificationCategory = "all") {
  const params = new URLSearchParams({ state, limit: "20", category });
  if (cursor) params.set("cursor", cursor);
  return request<NotificationPage>(`notifications?${params}`, { signal }).then(value => {
    validateSummary(value);
    if (category !== "all" && value.category !== category) {
      throw new NotificationCategoriesUnavailable("Category filters are not available on this server yet. You can still view all notifications.");
    }
    if (!Array.isArray(value.items)) throw new Error("Could not load notifications. Try again.");
    return value;
  });
}
export function markNotification(item: NotificationItem, read: boolean) {
  return request<unknown>(`notifications/${encodeURIComponent(item.id)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ read, version: item.version }),
  });
}
export function markNotificationSnapshot(snapshot: string) {
  return request<unknown>("notifications/mark-read", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ snapshot }),
  });
}
export function documentSubscription(uri: string, method = "GET", signal?: AbortSignal) {
  return request<{ subscribed: boolean; resource_id: string }>(
    `notification-subscriptions?${new URLSearchParams({ uri })}`, { method, signal },
  );
}
export function notificationSubscriptions(signal?: AbortSignal) {
  return request<{ items: DocumentSubscription[] }>("notification-subscriptions", { signal });
}
