import { createContext, useContext, useLayoutEffect, type RefObject } from "react";

export interface PreviewEditorSession {
  requestExit: (proceed: () => void) => void;
  dismissMenu: () => boolean;
}

/** The route owns dismissal; the document owns unsaved work and editor menus. */
export const DocumentPreviewEditorContext = createContext<RefObject<PreviewEditorSession | null> | null>(null);

export function usePreviewEditorSession(session: PreviewEditorSession | null) {
  const bridge = useContext(DocumentPreviewEditorContext);
  useLayoutEffect(() => {
    if (!bridge) return;
    bridge.current = session;
    return () => { if (bridge.current === session) bridge.current = null; };
  }, [bridge, session]);
}
