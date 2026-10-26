import {
  ApiError,
  authenticatedFetch,
  authSessionSnapshot,
  isCurrentAuthSession,
  throwJsonApiError,
  type AuthSessionSnapshot,
  type AvatarState,
} from "./api";

const PHOTO_PATH = "/api/v1/auth/me/avatar";
const REVISION = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MAX_PHOTO_BYTES = 256 * 1024;

export interface ProfilePhotoMutationResult { avatar: AvatarState }

export function isProfilePhotoRevision(value: unknown): value is string {
  return typeof value === "string" && REVISION.test(value);
}

function assertSession(snapshot: AuthSessionSnapshot) {
  if (!isCurrentAuthSession(snapshot)) {
    throw new ApiError("Your session changed. Review your profile photo again.", 409, { code: "account_identity_changed" });
  }
}

function revisionParam(revision: string | null): string {
  if (revision === null) return "none";
  if (!isProfilePhotoRevision(revision)) throw new Error("Invalid profile photo revision");
  return revision;
}

function invalidResponse(): never {
  throw new ApiError("The server returned an invalid profile photo response. Refresh your profile before trying again.", 502, { code: "invalid_avatar_response" });
}

function parseAvatar(value: unknown): AvatarState {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return invalidResponse();
  const avatar = value as Record<string, unknown>;
  if (avatar.revision === null && avatar.url === null) return { revision: null, url: null };
  // Removal advances the revision as a tombstone, preventing a stale tab from
  // replacing the photo with an outdated expected_revision=none write.
  if (isProfilePhotoRevision(avatar.revision) && avatar.url === null) return { revision: avatar.revision, url: null };
  if (!isProfilePhotoRevision(avatar.revision) || typeof avatar.url !== "string") return invalidResponse();
  // Metadata never becomes a credential-bearing arbitrary image URL. Reads
  // below always construct the authenticated, same-origin endpoint themselves.
  let url: URL;
  try { url = new URL(avatar.url, window.location.origin); } catch { return invalidResponse(); }
  if (url.origin !== window.location.origin || url.pathname !== PHOTO_PATH || url.searchParams.get("v") !== avatar.revision || url.hash) return invalidResponse();
  const result: AvatarState = { revision: avatar.revision, url: avatar.url };
  for (const dimension of ["width", "height"] as const) {
    if (avatar[dimension] !== undefined) {
      if (typeof avatar[dimension] !== "number" || !Number.isInteger(avatar[dimension]) || avatar[dimension] < 1 || avatar[dimension] > 8192) return invalidResponse();
      result[dimension] = avatar[dimension];
    }
  }
  return result;
}

async function mutatePhoto(method: "PUT" | "DELETE", expectedRevision: string | null, file?: File): Promise<ProfilePhotoMutationResult> {
  const snapshot = authSessionSnapshot();
  const query = new URLSearchParams({ expected_revision: revisionParam(expectedRevision) });
  let response: Response;
  try {
    response = await authenticatedFetch(`${PHOTO_PATH}?${query}`, {
      method,
      ...(file ? { body: file, headers: { "Content-Type": file.type } } : {}),
    }, { unauthorized: "preserve-session" });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401 && isCurrentAuthSession(snapshot) && error.name !== "DeferredSessionError") {
      window.dispatchEvent(new Event("akb:revalidate-access"));
    }
    throw error;
  }
  assertSession(snapshot);
  if (!response.ok) await throwJsonApiError(response);
  const body: unknown = await response.json().catch(invalidResponse);
  assertSession(snapshot);
  if (!body || typeof body !== "object" || !("avatar" in body)) return invalidResponse();
  const avatar = parseAvatar(body.avatar);
  if ((method === "PUT" && (avatar.revision === null || avatar.url === null)) || (method === "DELETE" && avatar.url !== null)) return invalidResponse();
  return { avatar };
}

export function uploadProfilePhoto(file: File, expectedRevision: string | null): Promise<ProfilePhotoMutationResult> {
  return mutatePhoto("PUT", expectedRevision, file);
}

export function removeProfilePhoto(expectedRevision: string | null): Promise<ProfilePhotoMutationResult> {
  return mutatePhoto("DELETE", expectedRevision);
}

export async function getProfilePhotoBlob(revision: string, { signal }: { signal?: AbortSignal } = {}): Promise<Blob> {
  if (!isProfilePhotoRevision(revision)) throw new Error("Invalid profile photo revision");
  const snapshot = authSessionSnapshot();
  const query = new URLSearchParams({ v: revision });
  const response = await authenticatedFetch(`${PHOTO_PATH}?${query}`, { signal, cache: "no-store" }, { unauthorized: "preserve-session" });
  assertSession(snapshot);
  if (!response.ok) await throwJsonApiError(response);
  if (response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase() !== "image/webp") return invalidResponse();
  const blob = await response.blob();
  assertSession(snapshot);
  if (blob.size === 0 || blob.size > MAX_PHOTO_BYTES) return invalidResponse();
  return blob;
}
