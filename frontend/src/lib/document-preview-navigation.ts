import type { Location } from "react-router-dom";

export interface DocumentPreviewNavigationState {
  documentPreview: true;
  backgroundLocation: Location;
  returnFocusId?: string;
}

/**
 * Preserve the page that launched a search result so a document can open as a
 * route-backed preview without losing the query, filters, or result scroll.
 */
export function documentPreviewState(
  backgroundLocation: Location,
  returnFocusId?: string,
): DocumentPreviewNavigationState {
  return { documentPreview: true, backgroundLocation, returnFocusId };
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
