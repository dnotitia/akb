import { getDocument, updateDocument } from "@/lib/api";

/** Archive changes discovery, never document identity or access permissions. */
export function documentArchiveDisabledReason(options: {
  role: string | null;
  readOnly: boolean;
  historical: boolean;
  guide: boolean;
}): string | undefined {
  if (options.historical) return "Open the latest version to change document state.";
  if (options.readOnly) return "This Vault is read-only or its write access could not be verified.";
  if (!["writer", "admin", "owner"].includes(options.role || "")) return "Writer access or higher is required.";
  if (options.guide && options.role !== "owner") return "Only the Vault owner can change the Vault guide.";
}

export async function changeDocumentArchiveState(vault: string, ref: string, status: "active" | "archived", expectedCommit?: string) {
  await updateDocument(vault, ref, {
    status,
    ...(expectedCommit ? { expected_commit: expectedCommit } : {}),
  });
  // A successful HTTP response alone cannot prove older servers applied status.
  // Do not overwrite title/body locally with an optimistic metadata snapshot.
  const current = await getDocument(vault, ref);
  if (current.status !== status) {
    throw new Error("The document state could not be verified. Reload the document before trying again; the server may not support this action or another edit may have changed it.");
  }
  window.dispatchEvent(new CustomEvent("akb:document-status-changed", { detail: { vault, path: current.path, status } }));
  return current;
}
