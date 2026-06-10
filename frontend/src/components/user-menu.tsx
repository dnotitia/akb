import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  LogOut,
  Monitor,
  Moon,
  Settings as SettingsIcon,
  Sun,
} from "lucide-react";
import { getMe, setToken, isSsoSession, clearSsoSession, keycloakLogoutUrl } from "@/lib/api";
import { useTheme, type Theme } from "@/hooks/use-theme";

interface User {
  username?: string;
  email?: string;
  display_name?: string;
  is_admin?: boolean;
}

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
export function UserMenu() {
  const navigate = useNavigate();
  const { theme, setTheme } = useTheme();
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    getMe()
      .then(setUser)
      .catch(() => setUser(null));
  }, []);

  const label = user?.display_name || user?.username || "Account";
  const initial = (label[0] || "?").toUpperCase();

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        aria-label={`Account menu — ${label}`}
        className="inline-flex h-9 items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-2 pr-3 text-foreground hover:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background transition-token cursor-pointer"
      >
        <span
          className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-gradient-to-br from-[var(--color-teal)] to-[var(--color-teal-2)] text-white font-mono text-[10px] font-semibold"
          aria-hidden
        >
          {initial}
        </span>
        <span className="hidden sm:inline text-[13px] font-medium">
          {label}
        </span>
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
            <div title={label} className="text-sm font-medium text-foreground truncate mt-0.5">
              {label}
            </div>
            {user?.email && (
              <div title={user.email} className="font-mono text-[11px] text-foreground-muted truncate">
                {user.email}
              </div>
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

          <DropdownMenu.Item
            onSelect={() => {
              const sso = isSsoSession();
              // Build the KC logout URL (reads the id_token_hint) BEFORE
              // clearing the session, otherwise the hint is gone and
              // Keycloak falls back to a "Do you want to log out?" prompt.
              const kcLogout = sso ? keycloakLogoutUrl() : null;
              setToken(null);
              clearSsoSession();
              if (kcLogout) {
                // RP-initiated logout: end the Keycloak session too, then
                // KC redirects back to /auth. Full navigation (not SPA).
                window.location.href = kcLogout;
              } else {
                navigate("/auth");
              }
            }}
            className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm text-destructive outline-none data-[highlighted]:bg-destructive/10"
          >
            <LogOut className="h-4 w-4" aria-hidden />
            <span>Sign out</span>
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
