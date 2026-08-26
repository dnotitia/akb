import { createContext, useContext, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

const VaultCreateDialogContext = createContext<(() => void) | null>(null);

export function VaultCreateDialogProvider({
  openCreateVault,
  children,
}: {
  openCreateVault: () => void;
  children: ReactNode;
}) {
  return (
    <VaultCreateDialogContext.Provider value={openCreateVault}>
      {children}
    </VaultCreateDialogContext.Provider>
  );
}

export function useOpenVaultCreateDialog() {
  const openDialog = useContext(VaultCreateDialogContext);
  const navigate = useNavigate();
  return openDialog || (() => navigate("/vault/new"));
}
