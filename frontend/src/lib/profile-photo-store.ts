import { ApiError, authSessionSnapshot, getProfilePhotoBlob, isCurrentAuthSession, subscribePrivateAssetReset, type AuthSessionSnapshot } from "./api";

export type ProfilePhotoSnapshot = Readonly<{ url: string | null; error: unknown | null }>;
type PhotoEntry = {
  snapshot: ProfilePhotoSnapshot;
  session: AuthSessionSnapshot;
  controller: AbortController;
  listeners: Set<(snapshot: ProfilePhotoSnapshot) => void>;
  disposed: boolean;
};
const entries = new Map<string, PhotoEntry>();
let stopResetListener: (() => void) | null = null;
const EMPTY: ProfilePhotoSnapshot = Object.freeze({ url: null, error: null });
const unauthorizedChecks = new Set<string>();
let unauthorizedGeneration: number | null = null;

function dispose(entry: PhotoEntry) {
  entry.disposed = true;
  entry.controller.abort();
  if (entry.snapshot.url) URL.revokeObjectURL(entry.snapshot.url);
  entry.snapshot = EMPTY;
}

function resetPhotos() {
  const previous = [...entries.values()];
  entries.clear();
  stopResetListener?.();
  stopResetListener = null;
  for (const entry of previous) {
    dispose(entry);
    // A readable SSO session cookie can change during render. Revocation is
    // synchronous, but React observers are notified after that render finishes.
    queueMicrotask(() => { for (const listener of entry.listeners) listener(EMPTY); });
  }
}

/** One private request and object URL across the header, menu and Profile. */
export function acquireProfilePhoto(
  userId: string,
  revision: string,
  listener: (snapshot: ProfilePhotoSnapshot) => void,
  session = authSessionSnapshot(),
): () => void {
  if (!isCurrentAuthSession(session)) { listener(EMPTY); return () => {}; }
  const key = `${session.generation}\u0000${userId}\u0000${revision}`;
  if (unauthorizedGeneration !== session.generation) {
    unauthorizedChecks.clear();
    unauthorizedGeneration = session.generation;
  }
  let entry = entries.get(key);
  if (!entry) {
    entry = { snapshot: EMPTY, session, controller: new AbortController(), listeners: new Set(), disposed: false };
    entries.set(key, entry);
    stopResetListener ??= subscribePrivateAssetReset(resetPhotos);
    const current = entry;
    void getProfilePhotoBlob(revision, { signal: current.controller.signal }).then(blob => {
      if (current.disposed || !isCurrentAuthSession(current.session)) return;
      current.snapshot = { url: URL.createObjectURL(blob), error: null };
      for (const subscriber of current.listeners) subscriber(current.snapshot);
    }).catch(error => {
      if (current.disposed || current.controller.signal.aborted || !isCurrentAuthSession(current.session)) return;
      current.snapshot = { url: null, error };
      for (const subscriber of current.listeners) subscriber(current.snapshot);
      if (error instanceof ApiError && error.status === 401 && !unauthorizedChecks.has(key)) {
        unauthorizedChecks.add(key);
        // Verify once per session/photo. If identity remains valid but this
        // endpoint is misconfigured, repeated proof refreshes must not loop.
        window.dispatchEvent(new Event("akb:revalidate-access"));
      }
    });
  }
  const ownedEntry = entry;
  ownedEntry.listeners.add(listener);
  listener(ownedEntry.snapshot);
  return () => {
    ownedEntry.listeners.delete(listener);
    if (ownedEntry.listeners.size > 0 || ownedEntry.disposed) return;
    entries.delete(key);
    dispose(ownedEntry);
    if (entries.size === 0) {
      stopResetListener?.();
      stopResetListener = null;
    }
  };
}
