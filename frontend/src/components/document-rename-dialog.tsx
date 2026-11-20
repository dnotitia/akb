import { useEffect, useId, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, getDocument, updateDocument } from "@/lib/api";
import { documentTitleConflictFromError, documentTitleKey, type DocumentTitleConflict } from "@/lib/document-title-conflict";
import { readWithAccessBoundary } from "@/lib/query-access-boundary";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { DocumentTitleConflictNotice } from "@/components/document-title-conflict-notice";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";

interface TitleSnapshot {
  path: string;
  title: string;
  current_commit: string;
}

/** Mount for one rename. Read the current revision before sending a title-only update. */
export function DocumentRenameDialog({ vault, path, onOpenChange, onRenamed, returnFocusRef }: {
  vault: string;
  path: string;
  onOpenChange: (open: boolean) => void;
  onRenamed?: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
}) {
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const proof = JSON.stringify([vault, path, user?.user_id, revision]);
  const initialProof = useRef(proof);
  const live = useRef(false);
  const currentAccess = useRef({ proof, checking });
  useLayoutEffect(() => {
    live.current = true;
    currentAccess.current = { proof, checking };
    return () => { live.current = false; };
  }, [proof, checking]);
  const client = useQueryClient();
  const [base, setBase] = useState<TitleSnapshot | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState<DocumentTitleConflict | null>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  const id = useId();
  const snapshotKey = ["document-rename", vault, path, user?.user_id, revision];
  const snapshot = useQuery<TitleSnapshot | null>({
    queryKey: snapshotKey,
    queryFn: ({ queryKey, signal }) => readWithAccessBoundary(client, queryKey, () => getDocument(vault, path), signal),
    enabled: !checking && proof === initialProof.current,
    retry: false,
    staleTime: 0,
  });
  useEffect(() => {
    if (checking || proof !== initialProof.current) onOpenChange(false);
  }, [checking, proof, onOpenChange]);
  useEffect(() => {
    if (base || snapshot.isFetching || !snapshot.data) return;
    const loaded = { ...snapshot.data, title: snapshot.data.title ?? "" };
    setBase(loaded);
    setDraft(loaded.title);
  }, [base, snapshot.data, snapshot.isFetching]);
  useEffect(() => {
    if (base) { titleRef.current?.focus(); titleRef.current?.select(); }
  }, [base]);

  const title = documentTitleKey(draft);
  const accessDenied = snapshot.error instanceof ApiError && [401, 403, 404].includes(snapshot.error.status);
  const changed = Boolean(base && title !== documentTitleKey(base.title));
  const canSave = changed && Boolean(title && base?.current_commit?.trim()) && !saving && !accessDenied;
  const isCurrent = () => live.current && !currentAccess.current.checking && currentAccess.current.proof === initialProof.current;

  async function save(policy: "reject" | "allow" = "reject") {
    if (!base || !canSave || !isCurrent()) return;
    setSaving(true);
    setError("");
    try {
      await updateDocument(vault, base.path, {
        title,
        expected_commit: base.current_commit,
        title_conflict_policy: policy,
      });
    } catch (nextError) {
      if (!isCurrent()) return;
      const titleConflict = documentTitleConflictFromError(nextError);
      if (titleConflict) setConflict(titleConflict);
      else setError(nextError instanceof ApiError && nextError.status === 409
        ? "This document changed while you were renaming it. Reload the current title and try again."
        : nextError instanceof Error ? nextError.message : "The title could not be saved. Try again.");
      setSaving(false);
      return;
    }
    if (!isCurrent()) return;
    // The write has committed. A failed refresh must not invite another write.
    void client.invalidateQueries({ predicate: query =>
      query.queryKey[0] === "document" && query.queryKey[1] === vault
      && (query.queryKey[2] === base.path || (query.state.data as { path?: string } | null)?.path === base.path),
    });
    void client.invalidateQueries({ queryKey: ["document-history", vault, base.path] });
    void client.invalidateQueries({ queryKey: ["document-edit-title-candidates", vault] });
    onRenamed?.();
    onOpenChange(false);
  }

  if (checking || proof !== initialProof.current) return null;
  return <Dialog open onOpenChange={open => { if (!saving) onOpenChange(open); }}>
    <DialogContent className="max-w-md gap-3 p-5"
      onCloseAutoFocus={event => {
        if (returnFocusRef.current?.isConnected) { event.preventDefault(); returnFocusRef.current.focus({ preventScroll: true }); }
      }}
      onEscapeKeyDown={event => { if (saving) event.preventDefault(); }}>
      <DialogHeader className="pr-8">
        <DialogTitle className="text-base">Rename document</DialogTitle>
        <DialogDescription>Choose a title for this document.</DialogDescription>
      </DialogHeader>
      {!base || accessDenied ? snapshot.isError || snapshot.isSuccess && !snapshot.data ? <Alert title="Could not load the current title">
        <Button type="button" variant="outline" size="sm" loading={snapshot.isFetching} onClick={() => void snapshot.refetch()}>Try again</Button>
      </Alert> : <LoadingState label="Loading current title"><Skeleton className="h-10 w-full" /></LoadingState> :
        <form noValidate onSubmit={event => { event.preventDefault(); if (!conflict) void save(); }} className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor={id}>Title</Label>
            <Input ref={titleRef} id={id} value={draft} disabled={saving}
              aria-invalid={!title || Boolean(conflict) || undefined}
              aria-describedby={!title ? `${id}-error` : undefined}
              onChange={event => { setDraft(event.target.value); setConflict(null); setError(""); }} />
            {!title && <p id={`${id}-error`} role="alert" className="text-xs text-destructive">Enter a document title.</p>}
          </div>
          {conflict && <DocumentTitleConflictNotice conflict={conflict}
            onChooseAlternative={() => { setConflict(null); titleRef.current?.focus(); titleRef.current?.select(); }}
            chooseAlternativeLabel="Choose another title" onKeepBoth={() => void save("allow")}
            keepBothLabel="Use this title" keepingBoth={saving} />}
          {error && <Alert>{error}<Button type="button" variant="outline" size="sm" className="mt-2" onClick={() => {
            setBase(null); setError(""); setConflict(null); void client.resetQueries({ queryKey: snapshotKey, exact: true });
          }}>Reload current title</Button></Alert>}
          {!base.current_commit?.trim() && <Alert>The document revision is unavailable. Reload the document before renaming it.</Alert>}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={saving} onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" loading={saving} disabled={!canSave || Boolean(conflict)}>Save title</Button>
          </DialogFooter>
        </form>}
    </DialogContent>
  </Dialog>;
}
