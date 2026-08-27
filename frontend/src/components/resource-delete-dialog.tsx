import { ConfirmDialog } from "@/components/ui/confirm-dialog";

export type DeletableResourceKind = "document" | "file" | "table";

interface ResourceDeleteDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: DeletableResourceKind;
  name: string;
  rowCount?: number;
  onConfirm: () => void | Promise<void>;
}

const LABELS: Record<DeletableResourceKind, string> = {
  document: "document",
  file: "file",
  table: "table",
};

/** One deletion contract for every first-class Vault resource.
 *
 * Documents and files use a standard destructive confirmation. Tables add an
 * exact-name gate because the operation removes a physical database relation
 * and all rows. API errors stay inside ConfirmDialog so conflicts can be fixed
 * and retried without losing context.
 */
export function ResourceDeleteDialog({
  open,
  onOpenChange,
  kind,
  name,
  rowCount = 0,
  onConfirm,
}: ResourceDeleteDialogProps) {
  const label = LABELS[kind];
  const description =
    kind === "document"
      ? "The document, search index entries, relationships, and publication links will be removed. Git history is preserved, but this cannot be undone from the UI."
      : kind === "file"
        ? "The file record, preview, search index entries, relationships, and publication links will be removed. Stored content is cleaned up asynchronously. This cannot be undone."
        : `The physical table and all ${rowCount.toLocaleString()} ${rowCount === 1 ? "row" : "rows"}, search index entries, and relationships will be permanently removed. References from another table will block deletion.`;

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Delete ${label} “${name}”?`}
      description={description}
      confirmLabel={`Delete ${label}`}
      variant="destructive"
      confirmationText={kind === "table" ? name : undefined}
      confirmationLabel={
        kind === "table" ? "Type the table name to confirm permanent deletion" : undefined
      }
      onConfirm={onConfirm}
    />
  );
}
