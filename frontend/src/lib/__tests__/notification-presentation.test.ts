import { describe, expect, it } from "vitest";
import { groupNotifications, notificationDay } from "@/lib/notification-presentation";
import type { NotificationItem } from "@/lib/api-notifications";

describe("notification calendar groups", () => {
  const now = new Date(2026, 8, 8, 12);
  it("uses local midnight boundaries, including month transitions", () => {
    expect(notificationDay(new Date(2026, 8, 8, 0).toISOString(), now)).toBe("Today");
    expect(notificationDay(new Date(2026, 8, 7, 23, 59).toISOString(), now)).toBe("Yesterday");
    expect(notificationDay(new Date(2026, 8, 6).toISOString(), now)).toBe("Earlier");
    expect(notificationDay(new Date(2026, 7, 31).toISOString(), new Date(2026, 8, 1))).toBe("Yesterday");
    expect(notificationDay("invalid", now)).toBe("Earlier");
  });
  it("joins adjacent pages without duplicating a date heading or changing order", () => {
    const item = (id: string, day: number) => ({ id, updated_at: new Date(2026, 8, day).toISOString() }) as NotificationItem;
    const groups = groupNotifications([item("a", 8), item("b", 8), item("c", 7), item("d", 1)], now);
    expect(groups.map(group => [group.label, group.items.map(item => item.id)])).toEqual([
      ["Today", ["a", "b"]], ["Yesterday", ["c"]], ["Earlier", ["d"]],
    ]);
    expect(groupNotifications([], now)).toEqual([]);
  });
});
