import { useEffect, useId, useRef, useState, type ChangeEvent } from "react";
import { Camera, X } from "lucide-react";
import {
  authSessionSnapshot,
  getMe,
  isCurrentAuthSession,
  removeProfilePhoto,
  uploadProfilePhoto,
  type AuthSessionSnapshot,
  type AvatarState,
  type ProfileAvatarCapabilities,
} from "@/lib/api";
import { UserAvatar } from "@/components/user-avatar";
import { Button } from "@/components/ui/button";
import { RoleBadge } from "@/components/status-badge";
import { useFlashStatus } from "@/hooks/use-flash-status";

type PhotoUser = {
  user_id: string;
  username: string;
  display_name: string | null;
  is_admin: boolean;
  avatar?: AvatarState;
  profile_capabilities?: { avatar?: ProfileAvatarCapabilities };
};

type SelectionOwner = {
  userId: string;
  revision: string | null;
  session: AuthSessionSnapshot;
};
type PendingSelection = SelectionOwner & { file: File };
type Candidate = PendingSelection & {
  url: string;
  ready: boolean;
};

function failureCode(error: unknown): string | undefined {
  if (!error || typeof error !== "object" || !("detail" in error)) return;
  const detail = error.detail;
  return detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string"
    ? detail.code : undefined;
}

/** Keep the operation owner outside Activity: identity proof hides the view,
 * not a pending upload's receipt or its Settings busy guard. */
// The operation owner deliberately lives outside the Activity-wrapped view.
// eslint-disable-next-line react-refresh/only-export-components
export function useProfilePhotoEditor({ user, visible, blocked, onUserUpdate }: {
  user: PhotoUser;
  visible: boolean;
  blocked: boolean;
  onUserUpdate: (patch: { avatar?: AvatarState; profile_capabilities?: PhotoUser["profile_capabilities"] }) => void;
}) {
  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const [pendingSelection, setPendingSelection] = useState<PendingSelection | null>(null);
  const [removeReview, setRemoveReview] = useState<{ revision: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const flash = useFlashStatus(4000);
  const mounted = useRef(true);
  const owner = useRef(user.user_id);
  const inFlight = useRef(false);
  const pickerOwner = useRef<SelectionOwner | null>(null);
  owner.current = user.user_id;
  const policy = user.profile_capabilities?.avatar;
  const enabled = policy?.supported === true;
  const previewUrl = candidate?.url;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl); }, [previewUrl]);
  useEffect(() => {
    const stale = (selection: SelectionOwner) => selection.userId !== user.user_id || !isCurrentAuthSession(selection.session);
    if (pickerOwner.current && stale(pickerOwner.current)) pickerOwner.current = null;
    if (pendingSelection && stale(pendingSelection)) setPendingSelection(null);
    if (candidate && stale(candidate)) {
      setCandidate(null);
      setError("");
      setConflict(false);
      setRemoveReview(null);
    }
  }, [candidate, pendingSelection, user.user_id, visible]);

  // A native chooser can return its File while window.focus is rechecking
  // identity. Retain it without a blob URL or write until that proof succeeds.
  useEffect(() => {
    if (!pendingSelection || !visible || blocked || busy) return;
    setPendingSelection(null);
    if (pendingSelection.userId !== user.user_id || !isCurrentAuthSession(pendingSelection.session)) return;
    setError(""); setRemoveReview(null);
    if (!policy?.can_upload) {
      setCandidate(null);
      setError("Photo changes are no longer available. Your saved photo has not changed.");
      return;
    }
    const { file } = pendingSelection;
    if (!policy.allowed_content_types.includes(file.type)) {
      setError("Choose a JPEG, PNG or WebP image. Animated images are not supported.");
      return;
    }
    if (file.size === 0 || file.size > policy.max_upload_bytes) {
      setError(`Choose a non-empty image up to ${Math.floor(policy.max_upload_bytes / (1024 * 1024))} MB.`);
      return;
    }
    setCandidate({ ...pendingSelection, url: URL.createObjectURL(file), ready: false });
  }, [pendingSelection, visible, blocked, busy, policy, user.user_id]);

  const owns = (id: string, session: AuthSessionSnapshot) =>
    mounted.current && owner.current === id && isCurrentAuthSession(session);

  function beginSelection() {
    if (!visible || blocked || inFlight.current || pendingSelection || !policy?.can_upload) return false;
    // Bind ownership and optimistic concurrency before leaving for native UI,
    // never to whichever account/photo happens to be current on return.
    pickerOwner.current = { userId: user.user_id, revision: user.avatar?.revision ?? null, session: authSessionSnapshot() };
    return true;
  }

  function select(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ""; // A cancelled picker is inert; the same file can be chosen again.
    const selection = pickerOwner.current;
    pickerOwner.current = null;
    if (!file || !selection || !owns(selection.userId, selection.session)) return;
    setPendingSelection({ ...selection, file });
  }

  function previewLoaded(image: HTMLImageElement) {
    if (!candidate) return;
    if (image.naturalWidth > 8192 || image.naturalHeight > 8192 || image.naturalWidth * image.naturalHeight > 12_000_000) {
      setError("This image's resolution is too high. Use at most 12 megapixels and 8,192 pixels per side.");
      setCandidate(null);
    } else setCandidate(current => current ? { ...current, ready: true } : current);
  }

  function cancel() {
    if (inFlight.current) return;
    pickerOwner.current = null; setPendingSelection(null);
    setCandidate(null); setRemoveReview(null); setError(""); setConflict(false);
  }

  function failed(caught: unknown) {
    setConflict(unresolved => unresolved || failureCode(caught) === "avatar_changed");
    setError(caught instanceof Error ? caught.message : "Could not save your photo. Try again or choose another image.");
  }

  async function save() {
    if (!candidate?.ready || pendingSelection || !visible || blocked || inFlight.current || !policy?.can_upload || conflict) return;
    if (!owns(candidate.userId, candidate.session)) { cancel(); return; }
    inFlight.current = true; setBusy(true); setError("");
    const submitted = candidate;
    const id = user.user_id;
    try {
      const result = await uploadProfilePhoto(submitted.file, submitted.revision);
      if (!owns(id, submitted.session)) return;
      onUserUpdate({ avatar: result.avatar });
      setCandidate(null);
      flash.setFlash("Photo saved");
    } catch (caught) { if (owns(id, submitted.session)) failed(caught); }
    finally { inFlight.current = false; setBusy(false); }
  }

  async function remove() {
    if (!removeReview || !visible || blocked || inFlight.current || !policy?.can_remove || conflict) return;
    const session = authSessionSnapshot();
    const id = user.user_id;
    const revision = removeReview.revision;
    inFlight.current = true; setBusy(true); setError("");
    try {
      const result = await removeProfilePhoto(revision);
      if (!owns(id, session)) return;
      onUserUpdate({ avatar: result.avatar });
      setRemoveReview(null);
      flash.setFlash("Photo removed");
    } catch (caught) { if (owns(id, session)) failed(caught); }
    finally { inFlight.current = false; setBusy(false); }
  }

  async function reload() {
    if (!visible || blocked || inFlight.current) return;
    const session = authSessionSnapshot();
    const id = user.user_id;
    inFlight.current = true; setBusy(true);
    try {
      const latest = await getMe();
      if (!owns(id, session) || latest.user_id !== id) return;
      onUserUpdate({ avatar: latest.avatar, profile_capabilities: latest.profile_capabilities });
      setCandidate(null); setRemoveReview(null); setError(""); setConflict(false);
      flash.setFlash("Latest photo loaded. Choose a photo to replace it.");
    } catch (caught) { if (owns(id, session)) failed(caught); }
    finally { inFlight.current = false; setBusy(false); }
  }

  return {
    user, policy, enabled, candidate, removeReview, busy, error, conflict,
    dirty: candidate !== null || pendingSelection !== null || removeReview !== null,
    blocked: blocked || busy || !visible || pendingSelection !== null,
    message: flash.message,
    beginSelection, select, previewLoaded, cancel, save, remove, reload,
    requestRemoval: () => { if (visible && !blocked && !inFlight.current && policy?.can_remove) { setRemoveReview({ revision: user.avatar?.revision ?? null }); setError(""); setConflict(false); } },
    dismissError: () => setError(""),
    previewFailed: () => { setCandidate(null); setError("This image could not be opened. Choose a different JPEG, PNG or WebP image."); },
  };
}

export function ProfilePhotoEditor({ editor }: { editor: ReturnType<typeof useProfilePhotoEditor> }) {
  const input = useRef<HTMLInputElement>(null);
  const chooseButton = useRef<HTMLButtonElement>(null);
  const descriptionId = useId();
  const errorId = useId();
  const { user, policy, candidate, removeReview, blocked } = editor;
  const photoPresent = !!user.avatar?.url;
  const label = user.display_name?.trim() || user.username;
  const unavailable = ["avatar_storage_unavailable", "storage_unavailable", "storage_not_configured"].includes(policy?.reason ?? "")
    ? "Profile photos are unavailable until your administrator configures file storage."
    : !editor.enabled ? "Profile photos are not supported by this server yet."
      : !policy?.can_upload ? "Photo changes require your own active account sign-in." : null;
  function cancel() {
    editor.cancel();
    chooseButton.current?.focus();
  }

  return <div className="py-5" aria-busy={editor.busy || undefined} data-testid="profile-photo-editor">
    <div className="flex min-w-0 items-start gap-3">
      {candidate ? <img key={candidate.url} src={candidate.url} alt="Selected profile photo preview" width={48} height={48}
        className="h-12 w-12 shrink-0 rounded-full object-cover" onLoad={event => editor.previewLoaded(event.currentTarget)} onError={editor.previewFailed} />
        : <UserAvatar user={user} size="lg" />}
      <div className="min-w-0 flex-1">
        <p className="break-words text-sm font-semibold text-foreground">{label}</p>
        <div className="mt-1"><RoleBadge role={user.is_admin ? "admin" : "user"} /></div>
        <input ref={input} type="file" accept={policy?.allowed_content_types.join(",") ?? "image/jpeg,image/png,image/webp"}
          className="sr-only" tabIndex={-1} aria-label="Choose profile photo" aria-describedby={descriptionId} disabled={blocked || !policy?.can_upload} onChange={editor.select} />
        {policy?.can_upload && <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button ref={chooseButton} variant="outline" size="sm" type="button" disabled={blocked} aria-describedby={`${descriptionId}${editor.error || editor.conflict ? ` ${errorId}` : ""}`} className="min-h-11 sm:min-h-9" onClick={() => { if (editor.beginSelection()) input.current?.click(); }}>
            <Camera className="h-4 w-4" aria-hidden />{candidate ? "Choose another" : photoPresent ? "Change photo" : "Upload photo"}
          </Button>
          {candidate ? <>
            <Button size="sm" type="button" loading={editor.busy} disabled={blocked || !candidate.ready || editor.conflict} className="min-h-11 sm:min-h-9" onClick={() => void editor.save()}>Save photo</Button>
            <Button variant="ghost" size="sm" type="button" disabled={blocked} className="min-h-11 sm:min-h-9" onClick={cancel}>Cancel</Button>
          </> : photoPresent && policy.can_remove && !removeReview ? <Button variant="ghost" size="sm" type="button" disabled={blocked} className="min-h-11 sm:min-h-9" onClick={editor.requestRemoval}>Remove photo</Button> : null}
        </div>}
        {candidate ? <p id={descriptionId} className="mt-2 break-all text-xs text-foreground-muted">{candidate.file.name} · Centered square preview. Not saved yet.</p>
          : !unavailable && <p id={descriptionId} className="mt-2 text-xs leading-relaxed text-foreground-muted">JPEG, PNG or WebP · Up to {Math.floor((policy?.max_upload_bytes ?? 0) / (1024 * 1024))} MB · Visible only to you.</p>}
        {unavailable && <p id={descriptionId} className="mt-2 text-xs leading-relaxed text-foreground-muted">{unavailable}</p>}
        {removeReview && <div role="group" aria-label="Remove profile photo?" className="mt-3 space-y-2 border-t border-border pt-3">
          <p className="text-sm text-foreground">Remove your photo and use initials instead?</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="destructive" size="sm" type="button" loading={editor.busy} disabled={blocked || editor.conflict} className="min-h-11 sm:min-h-9" onClick={() => void editor.remove()}>Remove photo</Button>
            <Button variant="ghost" size="sm" type="button" disabled={blocked} className="min-h-11 sm:min-h-9" onClick={cancel}>Keep photo</Button>
          </div>
        </div>}
        {(editor.error || editor.conflict) && <div className="mt-3 rounded-[var(--radius-sm)] border border-destructive bg-destructive-soft p-3">
          <div className="flex items-start gap-2"><p id={errorId} role="alert" className="min-w-0 flex-1 text-sm text-destructive-soft-foreground">{editor.error || "Your profile photo changed. Reload the latest version before saving again."}</p>
            {editor.error && <Button variant="ghost" size="icon" type="button" aria-label="Dismiss photo error" disabled={blocked} className="h-9 w-9 shrink-0" onClick={editor.dismissError}><X className="h-4 w-4" aria-hidden /></Button>}</div>
          {editor.conflict && <><p className="mt-1 text-xs text-foreground-muted">Reloading clears your selected photo so you can review the latest saved version.</p><Button variant="outline" size="sm" type="button" disabled={blocked} className="mt-2 min-h-11 sm:min-h-9" onClick={() => void editor.reload()}>Reload photo</Button></>}
        </div>}
        {editor.message && <p role="status" className="mt-2 text-sm text-success">{editor.message}</p>}
      </div>
    </div>
  </div>;
}
