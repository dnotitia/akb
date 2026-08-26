import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  LogOut,
  Settings as SettingsIcon,
} from "lucide-react";
import { getMe, logoutOrdinarySession, type CurrentUser } from "@/lib/api";
import { useTheme, type Theme } from "@/hooks/use-theme";
import { TooltipText } from "@/components/ui/tooltip-text";

const THEME_LABELS: Record<Theme, string> = {
  light: "Light",
  dark: "Dark",
  system: "System",
};

function initialsFor(label: string): string {
  const words = label.trim().split(/\s+/).filter(Boolean);
  if (words.length > 1) {
    return `${Array.from(words[0])[0] ?? ""}${Array.from(words[words.length - 1])[0] ?? ""}`;
  }
  return Array.from(words[0] ?? "?").slice(0, 2).join("");
}

/**
 * Unified user menu — avatar trigger, dropdown with identity + account actions.
 *
 * Consolidates what used to be three separate header slots (Settings link,
 * standalone ThemeToggle, Sign out button) so the header has a single
 * "this is about me" control.
 */
export function UserMenu({ initialUser }: { initialUser?: CurrentUser | null }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { theme, setTheme } = useTheme();
  const [user, setUser] = useState<CurrentUser | null>(initialUser ?? null);
  const [signOutError, setSignOutError] = useState("");
  const [signingOut, setSigningOut] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (initialUser) {
      setUser(initialUser);
      return;
    }
    getMe()
      .then(setUser)
      .catch(() => setUser(null));
  }, [initialUser]);

  async function signOut() {
    setSignOutError("");
    setSigningOut(true);
    try {
      const result = await logoutOrdinarySession();
      queryClient.clear();
      if (result.mode === "sso") {
        window.location.assign(result.logout_url);
        return;
      }
      setOpen(false);
      navigate(result.logout_url, { replace: true });
    } catch (caught) {
      setSignOutError(caught instanceof Error ? caught.message : "Sign-out failed");
    } finally {
      setSigningOut(false);
    }
  }

  const label = user?.display_name || user?.username || "Account";
  // Glyph casing is CSS-only (`uppercase` on the avatar), not a JS transform of
  // the user-supplied display name (§8).
  const initials = initialsFor(label);

  return (
    <DropdownMenu.Root
      // The account menu is a lightweight popover, not a modal task. Radix's
      // modal default locks document scrolling, removing the viewport scrollbar
      // and nudging the shared header/page horizontally on long routes.
      modal={false}
      open={open}
      onOpenChange={(next) => {
        if (!signingOut || next) setOpen(next);
      }}
    >
      <DropdownMenu.Trigger
        aria-label={`Account menu — ${label}`}
        className="group inline-flex h-9 min-w-9 max-w-28 cursor-pointer items-center gap-2 rounded-[var(--radius-md)] border border-border-strong bg-surface py-1 pl-1 pr-2.5 shadow-xs transition-token hover:border-primary hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background data-[state=open]:border-primary data-[state=open]:bg-surface-selected data-[state=open]:ring-2 data-[state=open]:ring-ring/30 sm:max-w-48"
      >
        <span
          className="inline-grid h-7 w-7 shrink-0 place-items-center rounded-[var(--radius-sm)] bg-surface-selected font-display text-xs font-bold uppercase text-surface-selected-foreground group-hover:bg-surface-active"
          aria-hidden
        >
          {initials}
        </span>
        <span className="min-w-0 truncate text-sm font-medium normal-case text-foreground">
          {label}
        </span>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="z-50 min-w-[240px] rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          {/* Identity card: the compact header trigger expands into the full
              account context here, where there is enough room to read it. */}
          <div className="mb-1 flex items-center gap-3 border-b border-border px-3 py-3">
            <span
              className="inline-grid h-10 w-10 shrink-0 place-items-center rounded-[var(--radius-md)] border border-border-strong bg-surface-selected font-display text-xs font-bold uppercase text-surface-selected-foreground"
              aria-hidden
            >
              {initials}
            </span>
            <div className="min-w-0 flex-1">
              <TooltipText as="div" className="truncate text-sm font-semibold text-foreground">
                {label}
              </TooltipText>
              {user?.email && (
                <TooltipText as="div" className="truncate text-xs text-foreground-muted">
                  {user.email}
                </TooltipText>
              )}
              {user?.is_admin && (
                <div className="mt-0.5 text-xs font-medium text-link">Administrator</div>
              )}
            </div>
          </div>

          {/* Theme — inline radio-ish row. Labelled sub-header to keep
              the dropdown's scan order predictable. */}
          <div className="px-3 pt-2 pb-1 coord">THEME</div>
          <div className="flex gap-1 px-2 pb-2">
            {(["light", "dark", "system"] as const).map((opt) => {
              const active = theme === opt;
              return (
                <button
                  key={opt}
                  type="button"
                  onClick={() => setTheme(opt)}
                  aria-pressed={active}
                  className={`flex-1 inline-flex items-center justify-center gap-1 h-7 text-xs font-medium rounded-[var(--radius-md)] border transition-colors cursor-pointer ${
                    active
                      ? "border-primary bg-surface-selected text-surface-selected-foreground"
                      : "border-border text-foreground-muted hover:text-foreground hover:bg-surface-hover"
                  }`}
                >
                  {THEME_LABELS[opt]}
                </button>
              );
            })}
          </div>

          <DropdownMenu.Separator className="h-px bg-border my-1" />

          <DropdownMenu.Item
            onSelect={() => navigate("/settings")}
            className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover"
          >
            <SettingsIcon className="h-4 w-4 text-foreground-muted" aria-hidden />
            <span>Settings</span>
          </DropdownMenu.Item>

          <DropdownMenu.Separator className="h-px bg-border my-1" />

          {signOutError && (
            <div className="px-3 py-2 text-xs text-destructive" role="alert">
              {signOutError}
            </div>
          )}

          <DropdownMenu.Item
            disabled={signingOut}
            onSelect={(event) => {
              event.preventDefault();
              void signOut();
            }}
            className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm text-destructive outline-none data-[highlighted]:bg-destructive/10"
          >
            <LogOut className="h-4 w-4" aria-hidden />
            <span>{signingOut ? "Signing out…" : "Sign out"}</span>
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
