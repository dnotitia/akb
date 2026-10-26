import { useEffect, useMemo, useState } from "react";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { authSessionSnapshot, isCurrentAuthSession, type AvatarState } from "@/lib/api";
import { isProfilePhotoRevision } from "@/lib/api-profile-photo";
import { acquireProfilePhoto, type ProfilePhotoSnapshot } from "@/lib/profile-photo-store";

export interface ProfilePhotoUser {
  user_id?: string;
  username?: string;
  display_name?: string | null;
  avatar?: AvatarState;
}

export function useProfilePhoto(user: ProfilePhotoUser | null | undefined): string | null {
  const currentUser = useCurrentUser();
  const verification = useAccessVerification();
  const userId = user?.user_id ?? null;
  const revision = user?.avatar?.url && isProfilePhotoRevision(user.avatar.revision) ? user.avatar.revision : null;
  const hasPhoto = Boolean(userId && revision);
  const proofKey = userId ? `${userId}\u0000${verification.revision}` : null;
  // Metadata belongs to the most recently accepted foreground proof. A changed
  // credential must wait for that proof, even if the old user's props linger.
  const acceptedSession = useMemo(() => hasPhoto && !verification.checking && proofKey ? authSessionSnapshot() : null, [hasPhoto, verification.checking, proofKey]);
  const enabled = Boolean(acceptedSession && !verification.checking && (!currentUser || currentUser.user_id === userId) && isCurrentAuthSession(acceptedSession));
  const key = enabled && acceptedSession && userId && revision ? `${acceptedSession.generation}\u0000${verification.revision}\u0000${userId}\u0000${revision}` : null;
  const [photo, setPhoto] = useState<{ key: string; snapshot: ProfilePhotoSnapshot } | null>(null);

  useEffect(() => {
    if (!key || !userId || !revision || !acceptedSession) return;
    return acquireProfilePhoto(userId, revision, snapshot => {
      setPhoto({ key, snapshot });
    }, acceptedSession);
  }, [key, userId, revision, acceptedSession]);

  return key && photo?.key === key && enabled ? photo.snapshot.url : null;
}
