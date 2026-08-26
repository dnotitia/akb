import { createContext, useContext, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";

export interface DocumentCreateDialogOptions {
  collection?: string;
}

type OpenDocumentCreateDialog = (options?: DocumentCreateDialogOptions) => void;

const DocumentCreateDialogContext = createContext<OpenDocumentCreateDialog | null>(null);

export function DocumentCreateDialogProvider({
  openCreateDocument,
  children,
}: {
  openCreateDocument: OpenDocumentCreateDialog;
  children: ReactNode;
}) {
  return (
    <DocumentCreateDialogContext.Provider value={openCreateDocument}>
      {children}
    </DocumentCreateDialogContext.Provider>
  );
}

/**
 * Opens the vault-owned document composer when it is available. The route
 * fallback keeps isolated stories and deep links useful outside VaultShell.
 */
export function useOpenDocumentCreateDialog(): OpenDocumentCreateDialog {
  const openDialog = useContext(DocumentCreateDialogContext);
  const navigate = useNavigate();
  const { name } = useParams<{ name: string }>();

  if (openDialog) return openDialog;

  return (options) => {
    if (!name) return;
    const collection = options?.collection?.trim();
    const query = collection
      ? `?collection=${encodeURIComponent(collection)}`
      : "";
    navigate(`/vault/${name}/doc/new${query}`);
  };
}
