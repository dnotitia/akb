import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { MoreHorizontal } from "lucide-react";
import { TooltipText } from "@/components/ui/tooltip-text";
import { browseVault } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import type { ResourceLocation } from "@/contexts/resource-location-context";

const linkClass = "min-w-0 truncate rounded-[var(--radius-sm)] text-foreground-muted transition-token hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset";
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

  return (
      <nav aria-label="Resource location" className={cn("@container/resource-location min-w-0 text-sm", className)}>
        <ol className="flex min-w-0 items-center gap-1">
          <li className="flex min-w-0 max-w-[30%] items-center">
            <Link to={vaultHref} className={linkClass} title={location.vault}>{location.vault}</Link>
          </li>
          {parent && <li className={cn("flex shrink-0 items-center gap-1", !ancestors.length && "@xs/resource-location:hidden")}>
            <span aria-hidden className="text-subtle">/</span>
            <DropdownMenu.Root modal={false}>
              <DropdownMenu.Trigger aria-label="Show collection ancestry" className={controlClass}>
                <MoreHorizontal className="h-4 w-4" aria-hidden />
              </DropdownMenu.Trigger>
              <DropdownMenu.Portal>
                <DropdownMenu.Content align="start" sideOffset={4} className="z-[var(--z-popover)] max-h-[60vh] max-w-[min(24rem,calc(100vw-2rem))] overflow-y-auto rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md">
                  {collections.map(ancestor => <DropdownMenu.Item key={ancestor.path} asChild>
                    <Link to={ancestor.href} className="flex min-h-11 items-center rounded-[var(--radius-sm)] px-3 py-2 text-sm text-foreground outline-none hover:text-link data-[highlighted]:bg-surface-hover break-words">{ancestor.label}</Link>
                  </DropdownMenu.Item>)}
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>
          </li>}
          {parent && <li className="flex min-w-0 max-w-[25%] items-center gap-1 @max-xs/resource-location:hidden">
            <span aria-hidden className="text-subtle">/</span>
            <Link to={parent.href} className={linkClass}>{parent.label}</Link>
          </li>}
          <li className="flex min-w-0 flex-1 items-center gap-1">
            <span aria-hidden className="shrink-0 text-subtle">/</span>
            <TooltipText
              aria-current="page"
              tabIndex={0}
              side="bottom"
              className="min-w-0 truncate rounded-[var(--radius-sm)] font-semibold text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
            >{location.title}</TooltipText>
            <span className="shrink-0 text-xs text-foreground-muted @max-sm/resource-location:hidden">({location.kind})</span>
          </li>
        </ol>
      </nav>
  );
}
