import { afterEach, describe, expect, it, vi } from "vitest";
import type { Location } from "react-router-dom";
import * as previewNavigation from "@/lib/document-preview-navigation";

const background: Location = { pathname: "/search", search: "?q=postgres", hash: "", state: null, key: "search-page" };
const preview: Location = { pathname: "/vault/alpha/doc/document", search: "", hash: "", state: null, key: "preview" };

afterEach(() => vi.restoreAllMocks());

describe("document preview Search return signal", () => {
  it("preserves an opt-in Search return token in preview navigation state", () => {
    expect(previewNavigation.documentPreviewState(background, "trigger", undefined, "session-token")).toMatchObject({
      documentPreview: true, backgroundLocation: background, globalSearchReturnToken: "session-token",
    });
  });

  it("reports whether a live Search session consumed the close signal", () => {
    const listener = vi.fn((event: Event) => event.preventDefault());
    window.addEventListener(previewNavigation.DOCUMENT_PREVIEW_CLOSED_EVENT, listener);
    try {
      const location = { ...preview, state: previewNavigation.documentPreviewState(background, "trigger", undefined, "session-token") };
      expect(previewNavigation.notifyDocumentPreviewClosed(location)).toBe(true);
      expect(listener).toHaveBeenCalledWith(expect.objectContaining({ detail: { token: "session-token" } }));
    } finally {
      window.removeEventListener(previewNavigation.DOCUMENT_PREVIEW_CLOSED_EVENT, listener);
    }
    expect(previewNavigation.notifyDocumentPreviewClosed({
      ...preview, state: previewNavigation.documentPreviewState(background, "trigger", undefined, "stale-token"),
    })).toBe(false);
  });

  it.each([
    null,
    { documentPreview: true, backgroundLocation: background },
    { documentPreview: false, backgroundLocation: background, globalSearchReturnToken: "token" },
    { documentPreview: true, backgroundLocation: background, globalSearchReturnToken: "" },
    { documentPreview: true, backgroundLocation: background, globalSearchReturnToken: 42 },
    { documentPreview: true, globalSearchReturnToken: "token" },
  ])("does not signal a return for regular or malformed preview state: %j", (state) => {
    const dispatch = vi.spyOn(window, "dispatchEvent");
    expect(previewNavigation.notifyDocumentPreviewClosed({ ...preview, state })).toBe(false);
    expect(dispatch).not.toHaveBeenCalled();
  });
});
