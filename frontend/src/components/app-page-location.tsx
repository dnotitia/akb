import { Link, matchRoutes, useLocation } from "react-router-dom";
import { appRouteContract } from "@/app-route-contract";
import { settingsSection } from "@/lib/settings-sections";
import { Bell, BookOpen, Boxes, CircleHelp, Compass, File, FilePlus2, FileText, GitCommitHorizontal, GitGraph, House, Plus, Search, Settings, Share2, Table2, Users, type LucideIcon } from "lucide-react";

const PAGE_IDENTITY: Record<string, [string, LucideIcon]> = {
  HomePage: ["Home", House],
  SearchPage: ["Search", Search],
  SettingsPage: ["Settings", Settings],
  NotificationsPage: ["Notifications", Bell],
  VaultNewPage: ["New vault", Plus],
  VaultIndexPage: ["Vaults", Boxes],
  VaultPage: ["Overview", Compass],
  DocumentNewPage: ["New document", FilePlus2],
  DocumentPage: ["Document", FileText],
  TablePage: ["Table", Table2],
  FilePage: ["File", File],
  GraphPage: ["Graph", GitGraph],
  PublicationsPage: ["Publish", Share2],
  VaultMembersPage: ["Members", Users],
  VaultSettingsPage: ["Settings", Settings],
  VaultActivityPage: ["Activity", GitCommitHorizontal],
  SkillRedirect: ["Vault guide", BookOpen],
};

/** Location chrome, not a second page heading or a resource filename. */
export function AppPageLocation({ isAdmin = false }: { isAdmin?: boolean }) {
  const { pathname, search } = useLocation();
  const match = matchRoutes([...appRouteContract], pathname)?.at(-1);
  const section = pathname === "/settings" ? settingsSection(new URLSearchParams(search).get("tab"), isAdmin) : null;
  const [label, Icon] = section ? [section.label, section.icon] : PAGE_IDENTITY[match?.route.component ?? ""] ?? ["Page not found", CircleHelp];
  const vault = match?.route.boundary === "vault-shell" ? match.params.name : undefined;

  return (
    <nav aria-label="Current page" className="hidden min-w-0 flex-1 items-center gap-2 overflow-hidden pr-4 text-sm lg:flex">
      {vault && <>
        <Link to={`/vault/${encodeURIComponent(vault)}`} title={vault} className="min-w-0 truncate rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset">{vault}</Link>
        <span aria-hidden className="shrink-0 text-subtle">/</span>
      </>}
      <span aria-current="page" className="inline-flex shrink-0 items-center gap-2 font-semibold text-foreground">
        <Icon className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden="true" />
        {label}
      </span>
    </nav>
  );
}
