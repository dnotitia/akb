import { useState } from "react";
import { useProfilePhoto, type ProfilePhotoUser } from "@/hooks/use-profile-photo";
import { cn } from "@/lib/utils";

const SIZE = { sm: "h-7 w-7 text-xs", md: "h-10 w-10 text-sm", lg: "h-12 w-12 text-lg" };

// Segment complete Unicode graphemes, so an emoji or combining mark is never
// split into a broken substitute glyph. Use code points on older browsers.
function graphemes(value: string): string[] {
  if (typeof Intl.Segmenter === "function") return Array.from(new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(value), part => part.segment);
  return Array.from(value);
}

// This tiny helper belongs to the shared avatar, not each calling surface.
// eslint-disable-next-line react-refresh/only-export-components
export function userInitials(user: Pick<ProfilePhotoUser, "username" | "display_name"> | null | undefined): string {
  const label = user?.display_name?.trim() || user?.username?.trim() || "?";
  const words = label.split(/\s+/u).filter(Boolean);
  return words.length > 1 ? `${graphemes(words[0])[0] ?? ""}${graphemes(words[words.length - 1])[0] ?? ""}` : graphemes(label).slice(0, 2).join("");
}

/** Decorative beside the visible account identity; never a broken image. */
export function UserAvatar({ user, size = "sm", className }: { user: ProfilePhotoUser | null | undefined; size?: keyof typeof SIZE; className?: string }) {
  const url = useProfilePhoto(user);
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  return (
    <span aria-hidden="true" data-slot="user-avatar" className={cn("inline-grid shrink-0 place-items-center overflow-hidden rounded-full bg-surface-selected font-display font-semibold uppercase text-surface-selected-foreground", SIZE[size], className)}>
      {url && url !== failedUrl ? <img src={url} alt="" className="h-full w-full object-cover" onError={() => setFailedUrl(url)} /> : userInitials(user)}
    </span>
  );
}
