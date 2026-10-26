import type { Location } from "react-router-dom";
import { documentPreviewBackground } from "./document-preview-navigation";
import { VAULT_SKILL_PATH } from "./skill";

export function vaultGuideHref(vault: string) {
  return `/vault/${encodeURIComponent(vault)}/doc/${encodeURIComponent(VAULT_SKILL_PATH)}`;
}

export function vaultGuideEditHref(vault: string) {
  return `/vault/${encodeURIComponent(vault)}/settings?edit=guide#skill`;
}

/** Only a reading preview retaining this exact Settings entry can bypass its
 * departure guard. A full-page guide, another Vault, or promotion still leaves
 * the mounted editor and must protect its draft. */
export function retainsGuideSettings(vault: string, location: Location, href: string, state: unknown) {
  const destination = new URL(href, window.location.origin);
  if (destination.origin !== window.location.origin || destination.pathname !== vaultGuideHref(vault)) return false;
  const background = documentPreviewBackground({ ...location, state });
  return background?.key === location.key && background.pathname === location.pathname &&
    background.search === location.search && background.hash === location.hash;
}
