import { describe, expect, it } from "vitest";
import { Archive, ArchiveRestore, ArrowRightLeft, Bell, FilePenLine, KeyRound, Trash2, UserRoundCheck, UserRoundKey, UserRoundMinus } from "lucide-react";
import { groupNotifications, notificationDay, notificationPresentation } from "@/lib/notification-presentation";
import type { NotificationItem } from "@/lib/api-notifications";

describe("notification icon vocabulary", () => {
  it.each([
    ["document.update", FilePenLine],
    ["document.move", ArrowRightLeft],
    ["access.granted", UserRoundCheck],
    ["document.archive", Archive],
    ["document.delete", Trash2],
    ["document.restore", ArchiveRestore],
    ["access.changed", KeyRound],
    ["access.revoked", UserRoundMinus],
    ["access.membership_removed", UserRoundMinus],
    ["access.ownership", UserRoundKey],
    ["future.event", Bell],
  ] as const)("uses the approved outline glyph for %s", (kind, icon) => {
    expect(notificationPresentation(kind).icon).toBe(icon);
  });
});

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
