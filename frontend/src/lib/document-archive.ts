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

export class ArchiveVerificationError extends Error {
  constructor() {
    super("The change was accepted, but its current state could not be verified. Check current state before making another change.");
    this.name = "ArchiveVerificationError";
  }
}

/** Reconcile a previously accepted write without sending it again. */
export async function checkDocumentArchiveState(vault: string, ref: string) {
  const current = await getDocument(vault, ref);
  window.dispatchEvent(new CustomEvent("akb:document-status-changed", { detail: { vault, path: current.path, status: current.status } }));
  return current;
}

export async function changeDocumentArchiveState(vault: string, ref: string, status: "active" | "archived", expectedCommit?: string) {
  await updateDocument(vault, ref, {
    status,
    ...(expectedCommit ? { expected_commit: expectedCommit } : {}),
  });
  // A successful HTTP response alone cannot prove older servers applied status.
  // Do not overwrite title/body locally with an optimistic metadata snapshot.
  try {
    const current = await checkDocumentArchiveState(vault, ref);
    if (current.status !== status) throw new ArchiveVerificationError();
    return current;
  } catch {
    throw new ArchiveVerificationError();
  }
}
