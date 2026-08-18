import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  LogOut,
  Monitor,
  Moon,
  Settings as SettingsIcon,
  Sun,
} from "lucide-react";
import { getMe, logoutOrdinarySession, type CurrentUser } from "@/lib/api";
import { useTheme, type Theme } from "@/hooks/use-theme";
import { TooltipText } from "@/components/ui/tooltip-text";

const THEME_ICONS: Record<Theme, React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>> = {
  light: Sun,
  dark: Moon,
  system: Monitor,
};

const THEME_LABELS: Record<Theme, string> = {
  light: "Light",
  dark: "Dark",
  system: "System",
};

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
  const initial = label[0] || "?";

  return (
    <DropdownMenu.Root
      open={open}
      onOpenChange={(next) => {
        if (!signingOut || next) setOpen(next);
      }}
    >
      <DropdownMenu.Trigger
        aria-label={`Account menu — ${label}`}
        className="inline-flex h-9 items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-2 pr-3 text-foreground hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background transition-token cursor-pointer"
      >
        <span
          className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-gradient-to-br from-[var(--color-teal)] to-[var(--color-teal-2)] text-white font-mono text-[10px] font-semibold uppercase"
          aria-hidden
        >
          {initial}
        </span>
        {/* Cap the label so long display names truncate instead of wrapping
            the header onto a second line; full name stays reachable via the
            overflow tooltip and the trigger's aria-label. */}
        <TooltipText
          as="span"
          className="hidden sm:inline max-w-[10rem] truncate text-[13px] font-medium"
        >
          {label}
        </TooltipText>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="z-50 min-w-[240px] rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          {/* Identity header */}
          <div className="px-3 py-2 border-b border-border mb-1">
            <div className="coord">Account</div>
            <TooltipText as="div" className="text-sm font-medium text-foreground truncate mt-0.5">
              {label}
            </TooltipText>
            {user?.email && (
              <TooltipText as="div" className="font-mono text-[11px] text-foreground-muted truncate">
                {user.email}
              </TooltipText>
            )}
            {user?.is_admin && (
              <div className="coord-spark mt-1">Admin</div>
            )}
          </div>

          {/* Theme — inline radio-ish row. Labelled sub-header to keep
              the dropdown's scan order predictable. */}
          <div className="px-3 pt-2 pb-1 coord">THEME</div>
          <div className="flex gap-1 px-2 pb-2">
            {(["light", "dark", "system"] as const).map((opt) => {
              const Icon = THEME_ICONS[opt];
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
                  <Icon className="h-3 w-3" aria-hidden />
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
