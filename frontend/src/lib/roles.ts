import type { ComponentType } from "react";
import { Crown, Eye, HelpCircle, Pencil, ShieldCheck } from "lucide-react";

// Role → glyph vocabulary, shared by the RoleBadge pill (status-badge.tsx) and
// the badge-less role glyph in the vault rail. Kept in a non-component module so
// both consumers stay Fast-Refresh clean.

export type Role = "owner" | "admin" | "writer" | "reader";
type IconType = ComponentType<{ className?: string; "aria-hidden"?: boolean }>;

export const ROLE_ICONS: Record<Role, IconType> = {
  owner: Crown, // owns the vault
  admin: ShieldCheck,
  writer: Pencil,
  reader: Eye,
};

// Authority ranks, mirroring the backend role hierarchy
// (access_service.py ROLE_HIERARCHY). One source for "can this role write?".
export const ROLE_RANK: Record<Role, number> = { owner: 4, admin: 3, writer: 2, reader: 1 };

/** Write authority: owner/admin/writer. Unknown roles → not editable. */
export function canEdit(role: string | undefined): boolean {
  return (ROLE_RANK[role as Role] ?? 0) >= ROLE_RANK.writer;
}

/**
 * Map a (possibly unknown) role to a glyph + accessible label. Backend can
 * introduce role levels ahead of the frontend enum, so an unrecognized role
 * falls back to a neutral HelpCircle with the raw string as its label rather
 * than rendering <undefined /> and crashing with React #130. Render the icon in
 * a NEUTRAL/foreground tint with a text label or tooltip — never color alone
 * (the glyph already carries the meaning).
 */
export function roleIcon(role: string): { Icon: IconType; label: string } {
  return { Icon: ROLE_ICONS[role as Role] ?? HelpCircle, label: role };
}
