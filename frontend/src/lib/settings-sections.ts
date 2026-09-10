import { Bell, KeyRound, Palette, ShieldCheck, UserRound } from "lucide-react";

export const SETTINGS_SECTIONS = [
  { id: "profile", label: "Profile", icon: UserRound },
  { id: "preferences", label: "Appearance", icon: Palette },
  { id: "notifications", label: "Watched documents", icon: Bell },
  { id: "tokens", label: "Agent connections", icon: KeyRound },
  { id: "admin", label: "Administration", icon: ShieldCheck },
] as const;

export function settingsSection(tab: string | null, isAdmin: boolean) {
  return SETTINGS_SECTIONS.find(section => section.id === tab && (section.id !== "admin" || isAdmin)) ?? SETTINGS_SECTIONS[0];
}
