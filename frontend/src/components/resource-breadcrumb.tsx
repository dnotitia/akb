import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { File, FileText, MoreHorizontal, Table2 } from "lucide-react";
import { TooltipText } from "@/components/ui/tooltip-text";
import { VaultBreadcrumb } from "@/components/vault-breadcrumb";
import { browseVault } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import type { ResourceLocation } from "@/contexts/resource-location-context";

const linkClass = "inline-flex min-w-0 items-center gap-1.5 rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset";
const controlClass = "inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring lg:h-8 lg:w-8";

/** Uses canonical page metadata and a single authorized directory snapshot. */
export function ResourceBreadcrumb({ location, className }: { location: ResourceLocation; className?: string }) {
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const directory = useQuery({
    queryKey: ["resource-collection-directory", user?.user_id, revision, location.vault],
    queryFn: () => browseVault(location.vault, undefined, -1),
    enabled: !!user && !checking && !!location.collectionPath,
    staleTime: 30_000,
    retry: false,
  });
  const collectionNames = new Map<string, string>();
  if (user && !checking && !directory.isError) {
    for (const item of directory.data?.items ?? []) {
      if (item.type === "collection" && typeof item.name === "string") collectionNames.set(item.path, item.name);
    }
  }
  const vaultHref = `/vault/${encodeURIComponent(location.vault)}`;
  const segments = location.collectionPath?.split("/").filter(Boolean) ?? [];
  const collections = segments.map((segment, index) => {
    const path = segments.slice(0, index + 1).join("/");
    return { label: collectionNames.get(path) || segment, path, href: `${vaultHref}?collection=${encodeURIComponent(path)}` };
  });
  const ancestors = collections.slice(0, -1);
  const parent = collections.at(-1);
  const ResourceIcon = { Document: FileText, File, Table: Table2 }[location.kind];

  return (
      <VaultBreadcrumb vault={location.vault} title={location.title} kind={location.kind}
        icon={ResourceIcon} ariaLabel="Resource location" className={className}>
          {parent && <li className={cn("flex shrink-0 items-center gap-1.5", !ancestors.length && "@xs/resource-location:hidden")}>
            <span aria-hidden className="text-subtle">/</span>
            <DropdownMenu.Root modal={false}>
              <DropdownMenu.Trigger aria-label="Show collection ancestry" className={controlClass}>
                <MoreHorizontal className="h-4 w-4" aria-hidden />
              </DropdownMenu.Trigger>
              <DropdownMenu.Portal>
                <DropdownMenu.Content align="start" sideOffset={4} className="z-[var(--z-popover)] max-h-[60vh] max-w-[min(24rem,calc(100vw-2rem))] overflow-y-auto rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md">
                  {collections.map(ancestor => <DropdownMenu.Item key={ancestor.path} asChild>
                    <Link to={ancestor.href} className="flex min-h-11 items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm text-foreground outline-none hover:text-link data-[highlighted]:bg-surface-hover break-words">
                      <span className="min-w-0">{ancestor.label}</span>
                    </Link>
                  </DropdownMenu.Item>)}
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>
          </li>}
          {parent && <li className="flex min-w-0 max-w-[25%] items-center gap-1.5 @max-xs/resource-location:hidden">
            <span aria-hidden className="shrink-0 text-subtle">/</span>
            <TooltipText asChild tip={parent.label} side="bottom">
              <Link to={parent.href} className={cn(linkClass, "block truncate")}>{parent.label}</Link>
            </TooltipText>
          </li>}
      </VaultBreadcrumb>
  );
}
