import { Archive, ArchiveRestore, ArrowRightLeft, Crown, FilePenLine, KeyRound, Trash2, UserRoundCheck, UserRoundMinus, Bell, type LucideIcon } from "lucide-react";
import type { TonalIconTone } from "@/components/ui/tonal-icon";
import type { NotificationItem } from "@/lib/api-notifications";

/** Short scan labels; never infer a person's role or recover redacted context. */
const events: Record<string, { label: string; icon: LucideIcon; tone: TonalIconTone }> = {
  "document.update": { label: "Updated", icon: FilePenLine, tone: "knowledge" },
  "document.move": { label: "Moved", icon: ArrowRightLeft, tone: "knowledge" },
  "document.archive": { label: "Archived", icon: Archive, tone: "neutral" },
  "document.restore": { label: "Restored", icon: ArchiveRestore, tone: "knowledge" },
  "document.delete": { label: "Deleted", icon: Trash2, tone: "neutral" },
  "access.granted": { label: "Access granted", icon: UserRoundCheck, tone: "people" },
  "access.changed": { label: "Access changed", icon: KeyRound, tone: "people" },
  "access.revoked": { label: "Access removed", icon: UserRoundMinus, tone: "people" },
  "access.membership_removed": { label: "Membership removed", icon: UserRoundMinus, tone: "people" },
  "access.ownership": { label: "Ownership changed", icon: Crown, tone: "people" },
};

export function notificationPresentation(kind: string) {
  return events[kind] ?? { label: null, icon: Bell, tone: "neutral" as const };
}

/** Local calendar boundaries (not 24-hour arithmetic, which fails across DST). */
export function notificationDay(iso: string, now = new Date()): "Today" | "Yesterday" | "Earlier" {
  const time = new Date(iso).getTime();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1).getTime();
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime();
  if (time >= today && time < tomorrow) return "Today";
  if (time >= yesterday && time < today) return "Yesterday";
  return "Earlier";
}

export function groupNotifications(items: NotificationItem[], now = new Date()) {
  const groups: { label: string; items: NotificationItem[] }[] = [];
  for (const item of items) {
    const label = notificationDay(item.updated_at, now);
    const last = groups.at(-1);
    if (last?.label === label) last.items.push(item);
    else groups.push({ label, items: [item] });
  }
  return groups;
}
