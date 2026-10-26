import { useEffect, useRef } from "react";
import { ChevronDown } from "lucide-react";
import { useVaults } from "@/hooks/use-vaults";
import { useVaultSwitcher } from "@/contexts/vault-switcher-context";
import { Button } from "@/components/ui/button";
import { RailIdentity } from "@/components/navigation-rail-controls";
import { VaultNavigationToggle, type VaultNavigationControl } from "@/components/vault-navigation-controls";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export interface VaultNavigationHeaderProps {
  current: string;
  /** Retained for existing shell consumers; creation belongs to the switcher. */
  onCreateVault?: () => void;
  onRefetchReady?: (refetch: () => void) => void;
  /** Called only when a normal same-window navigation was accepted. */
  onNavigate?: () => void;
  navigationControl?: VaultNavigationControl;
}

/** Sidebar disclosure and Vault identity share one row. */
export function VaultNavigationHeader({ current, onRefetchReady, onNavigate, navigationControl }: VaultNavigationHeaderProps) {
  const { refetch } = useVaults();
  const switcher = useVaultSwitcher();
  const switcherTrigger = useRef<HTMLButtonElement>(null);
  const switcherOpen = Boolean(switcher?.isOpen && switcher.trigger && switcher.trigger === switcherTrigger.current);

  useEffect(() => { onRefetchReady?.(refetch); }, [onRefetchReady, refetch]);

  return <TooltipProvider delayDuration={250}>
    <div className="shrink-0 bg-navigation" data-slot="vault-navigation-header">
      <RailIdentity slot="vault-identity-header" workspace>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button ref={switcherTrigger} data-vault-switcher-trigger="" variant="ghost"
              className="h-11 min-w-0 flex-1 justify-start whitespace-normal rounded-[var(--radius-sm)] px-2 text-left font-semibold text-foreground hover:bg-surface-hover hover:text-link"
              aria-label={current ? `Switch vault: ${current}` : "Choose a vault"} aria-haspopup="dialog" disabled={!switcher}
              aria-expanded={switcherOpen} aria-controls={switcherOpen ? "vault-switcher-panel" : undefined}
              onClick={event => switcher?.openVaultSwitcher(event.currentTarget, { side: "bottom", align: "end", onNavigate })}>
              <span data-slot="vault-identity-name" className="line-clamp-2 min-w-0 flex-1 break-words text-sm leading-5 [overflow-wrap:anywhere]">{current || "Choose a vault"}</span>
              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
            </Button>
          </TooltipTrigger>
          <TooltipContent className="max-w-80 break-words [overflow-wrap:anywhere]">{current || "Choose a vault"}</TooltipContent>
        </Tooltip>
        {navigationControl && <VaultNavigationToggle id="vault-navigation-collapse" {...navigationControl} />}
      </RailIdentity>
    </div>
  </TooltipProvider>;
}
