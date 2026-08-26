import { useEffect, useRef } from "react";
import { Loader2 } from "lucide-react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useOpenDocumentCreateDialog } from "@/contexts/document-create-dialog-context";

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
    <div
      className="flex min-h-[20rem] items-center justify-center text-sm text-foreground-muted"
      role="status"
      aria-live="polite"
    >
      <Loader2 className="mr-2 h-4 w-4 animate-spin text-link" aria-hidden />
      Opening document composer…
    </div>
  );
}
