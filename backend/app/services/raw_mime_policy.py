"""Which stored media types are safe to hand a browser without a sandbox.

A File's media type is declared by whoever uploaded it. Any path that serves
those bytes to a browser has to decide the same question — may this render as
a document in our origin? — and it has to decide it the same way, or the same
File is inert on one route and active on another.

The rule is fail-closed: a type is inert only if it is named here. Everything
else, including types that do not exist yet, is treated as an active document.
"""

from __future__ import annotations

# Rendered inline by the browser as a picture or a viewer, never as a document.
RAW_INLINE_IMAGE_PREFIX = "image/"
# `image/svg+xml` is an image by MIME and an ACTIVE document in fact: it can
# carry <script> that runs in the embedding origin on direct navigation. It
# must not ride the generic `image/` exemption.
RAW_ACTIVE_IMAGE_MIMES = frozenset({"image/svg+xml", "image/svg"})
# Provably inert: raster images (above), PDF, and plain text forms.
RAW_INERT_MIMES = frozenset({
    "application/pdf",
    "text/plain",
    "text/csv",
    "text/markdown",
})


def is_inert_raw_mime(mime: str) -> bool:
    """True when these bytes can be served without a CSP sandbox.

    SVG is excluded explicitly, so it falls through to the sandboxed default
    even though it starts with `image/`."""
    if mime in RAW_ACTIVE_IMAGE_MIMES:
        return False
    if mime.startswith(RAW_INLINE_IMAGE_PREFIX):
        return True
    return mime in RAW_INERT_MIMES
