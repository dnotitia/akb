import { useEffect, useRef } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useOpenDocumentCreateDialog } from "@/contexts/document-create-dialog-context";
import { InlineLoadingState } from "@/components/ui/loading-state";

/**
 * Compatibility bridge for bookmarks and older links. VaultShell owns the
 * composer, so this route opens it and returns the background to the vault
 * overview without adding a separate full-page creation state to history.
 */
export default function DocumentNewPage() {
  const { name } = useParams<{ name: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const openCreateDocument = useOpenDocumentCreateDialog();
  const openedRef = useRef(false);
  const initialCollection = searchParams.get("collection")?.trim() || undefined;

  useEffect(() => {
    if (!name || openedRef.current) return;
    openedRef.current = true;
    openCreateDocument({ collection: initialCollection });
    navigate(`/vault/${name}`, { replace: true });
  }, [initialCollection, name, navigate, openCreateDocument]);

  return (
    <div className="flex min-h-[20rem] items-center justify-center">
      <InlineLoadingState label="Opening document composer…" />
    </div>
  );
}
