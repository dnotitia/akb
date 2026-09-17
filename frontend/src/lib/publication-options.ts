import type { CreatePublicationRequest } from "@/lib/api";

export interface PublicationAccessOptions {
  requirePassword: boolean;
  password: string;
  expiresIn: string;
  maxViews: string;
}

export function emptyPublicationAccessOptions(): PublicationAccessOptions {
  return {
    requirePassword: false,
    password: "",
    expiresIn: "",
    maxViews: "",
  };
}

export function publicationAccessError(value: PublicationAccessOptions): string | null {
  if (value.requirePassword && value.password.trim().length < 8) {
    return "Use at least 8 characters for the publication password.";
  }
  const max = value.maxViews.trim();
  if (max && (!/^\d+$/.test(max) || !Number.isSafeInteger(Number(max)) || Number(max) < 1)) {
    return "Max views must be a positive whole number.";
  }
  return null;
}

export function publicationAccessPayload(
  value: PublicationAccessOptions,
): Pick<CreatePublicationRequest, "password" | "expires_in" | "max_views"> {
  const max = value.maxViews.trim();
  return {
    password: value.requirePassword ? value.password : undefined,
    expires_in: value.expiresIn || undefined,
    max_views: max ? Number(max) : undefined,
  };
}
