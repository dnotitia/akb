import type { Location } from "react-router-dom";

export interface DocumentPreviewNavigationState {
  documentPreview: true;
  backgroundLocation: Location;
  returnFocusId?: string;
  returnFocusFallbackId?: string;
  /** Opt-in handoff to a still-mounted global Search session. */
  globalSearchReturnToken?: string;
}

export const DOCUMENT_PREVIEW_CLOSED_EVENT = "akb:document-preview-closed";

/**
 * Preserve the launching page so a document can open as a route-backed preview
 * without losing search or inbox filters, scroll, or the initiating control.
 */
export function documentPreviewState(
  backgroundLocation: Location,
  returnFocusId?: string,
  returnFocusFallbackId?: string,
  globalSearchReturnToken?: string,
): DocumentPreviewNavigationState {
  return { documentPreview: true, backgroundLocation, returnFocusId, returnFocusFallbackId, globalSearchReturnToken };
}

/**
 * Call after a confirmed dismissal has unmounted the preview, including Back.
 * Never call for full-page promotion. True means Search restored its own focus;
 * false leaves the caller's ordinary return-focus behavior in charge.
 */
export function notifyDocumentPreviewClosed(location: Location): boolean {
  const state = location.state as Partial<DocumentPreviewNavigationState> | null;
  if (!documentPreviewBackground(location) || typeof state?.globalSearchReturnToken !== "string" || !state.globalSearchReturnToken) return false;
  return !window.dispatchEvent(new CustomEvent(DOCUMENT_PREVIEW_CLOSED_EVENT, {
    cancelable: true,
    detail: { token: state.globalSearchReturnToken },
  }));
}

/**
 * Browser history state is user-controlled input. Validate the small subset we
 * need before handing it to React Router as an alternate render location.
 */
export function documentPreviewBackground(
  location: Location,
): Location | null {
  const state = location.state as Partial<DocumentPreviewNavigationState> | null;
  const background = state?.backgroundLocation;

  if (
    state?.documentPreview !== true ||
    !background ||
    typeof background.pathname !== "string" ||
    typeof background.search !== "string" ||
    typeof background.hash !== "string"
  ) {
    return null;
  }

  return background;
}

export function documentPreviewReturnFocusId(location: Location) {
  const state = location.state as Partial<DocumentPreviewNavigationState> | null;
  return state?.documentPreview === true && typeof state.returnFocusId === "string"
    ? state.returnFocusId
    : null;
}

export function documentPreviewReturnFocusFallbackId(location: Location) {
  const state = location.state as Partial<DocumentPreviewNavigationState> | null;
  return state?.documentPreview === true && typeof state.returnFocusFallbackId === "string"
    ? state.returnFocusFallbackId : null;
}
