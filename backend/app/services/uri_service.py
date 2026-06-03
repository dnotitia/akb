"""AKB URI scheme — location-aware unified resource identifiers.

Every resource that lives in a vault carries a canonical URI that
self-describes both *what* it is and *where* it lives in the
collection tree. There is exactly one form per resource — no
aliases, no legacy variants — because the location prefix is the
whole point: given any URI you can walk up to its parent collection
and browse siblings without an extra lookup.

Forms (canonical, the only accepted shape):

    akb://{vault}                                      → Vault root (browse target)
    akb://{vault}/coll/{coll_path}                     → Collection (browse target)
    akb://{vault}/doc/{filename}                       → Document at vault root
    akb://{vault}/coll/{coll_path}/doc/{filename}      → Document inside a collection
    akb://{vault}/table/{name}                         → Table at vault root
    akb://{vault}/coll/{coll_path}/table/{name}        → Table inside a collection
    akb://{vault}/file/{uuid}                          → File at vault root
    akb://{vault}/coll/{coll_path}/file/{uuid}         → File inside a collection

Identifier rules:

  - `{coll_path}` is a collection path — slash-separated segments,
    no leading or trailing slash (e.g. ``specs/api``).
  - `{filename}` is the document's basename (leaf filename only —
    ``api-v2.md`` not ``specs/api-v2.md``). The collection part
    lives in the ``/coll/`` segment; this avoids the "is the slash
    a collection boundary or a sub-path?" ambiguity the old single-
    segment doc URI had.
  - `{name}` is the table name (unique per vault).
  - `{uuid}` is the file's UUID primary key (unique globally).

Examples:

    akb://my-vault                                       (vault root)
    akb://my-vault/coll/specs                            (collection)
    akb://my-vault/coll/specs/api                        (nested collection)
    akb://my-vault/coll/specs/doc/api-v2.md              (doc inside collection)
    akb://my-vault/coll/specs/api/doc/v1.md              (doc inside nested)
    akb://my-vault/doc/notes.md                          (doc at vault root)
    akb://my-vault/coll/finance/table/expenses           (table inside collection)
    akb://my-vault/coll/uploads/file/550e8400-...        (file inside collection)

This module is the single source of truth for URI building and
parsing. Every other service builds URIs through the helpers below
(``doc_uri`` / ``table_uri`` / ``file_uri`` / ``coll_uri`` /
``vault_uri``) and parses incoming URIs through ``parse_uri`` /
``split_uri`` / ``split_browse_uri``.

No backward compatibility with pre-0.3.0 URIs is maintained — that
older form (``akb://V/doc/specs/api.md`` for in-collection docs,
``akb://V/table/expenses`` for in-collection tables) is intentionally
unparseable. Migration 026 rewrites every stored URI in
``edges`` / ``publications`` / ``events`` to the new canonical form.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Component fragment shared across the URI patterns. Anchored to
# disallow consecutive or trailing slashes inside the captured groups.
_VAULT = r"([^/]+)"
_COLL_PATH = r"([^/]+(?:/[^/]+)*)"
_IDENT = r"(.+)"  # broad — caller normalizes per-type
_TYPE = r"(doc|table|file)"

_URI_VAULT_ONLY_RE = re.compile(rf"^akb://{_VAULT}/?$")
_URI_COLL_ONLY_RE = re.compile(rf"^akb://{_VAULT}/coll/{_COLL_PATH}/?$")
_URI_TYPED_ROOT_RE = re.compile(rf"^akb://{_VAULT}/{_TYPE}/{_IDENT}$")
_URI_TYPED_IN_COLL_RE = re.compile(
    rf"^akb://{_VAULT}/coll/{_COLL_PATH}/{_TYPE}/{_IDENT}$"
)

# Public catalog of resource types. ``coll`` is navigation-only;
# the rest are addressable content.
RESOURCE_TYPES = ("doc", "table", "file", "coll")


class ParsedUri(NamedTuple):
    """Structured view of an AKB URI.

    Fields in surface-first order:

      - ``vault``      — vault name
      - ``kind``       — ``doc`` / ``table`` / ``file`` / ``coll`` / ``vault``
      - ``identifier`` — kind-specific id (see below)
      - ``coll_path``  — collection path prefix, ``""`` for vault root

    For docs ``identifier`` is the **full vault-relative path** (coll
    prefix + basename) so call sites that join it back to
    ``documents.path`` work without modification. ``coll_path`` is
    available separately for code that wants the collection
    explicitly. For tables ``identifier`` is the table name; for files
    the UUID; for collections both ``identifier`` and ``coll_path``
    equal the collection path; for the vault form ``identifier`` is
    ``None``.
    """
    vault: str
    kind: str
    identifier: str | None
    coll_path: str | None = None


def parse_uri(uri: str) -> ParsedUri | None:
    """Parse an AKB URI in canonical form.

    Returns ``None`` for malformed input. Strips at most one trailing
    `/` on the identifier so the canonical and slash-suffixed forms
    resolve identically (defense against hand-typed URIs in
    frontmatter `depends_on` lists).

    Also rejects URIs containing ``{`` or ``}`` — those are template
    placeholders (e.g. ``akb://{vault}/coll/{path}/doc/{filename}``
    written into a markdown body as documentation) that the bare-URI
    scanner in ``kg_service.extract_markdown_links`` would otherwise
    pick up and try to insert as edges. Real AKB URIs never contain
    braces, so this is a safe + cheap pre-flight check.

    Try-order matters: the typed-in-collection pattern must run before
    the typed-root pattern, otherwise something like
    ``akb://V/coll/X/doc/Y.md`` would mis-match the root form with
    ``type=coll`` and an identifier starting with ``X/doc/Y.md``.
    """
    if not isinstance(uri, str):
        return None
    if "{" in uri or "}" in uri:
        # Template placeholder — not a real URI. Reject before regex
        # so neither `parse_uri` nor `split_uri` ever produces a row
        # the edges table won't accept.
        return None

    m = _URI_TYPED_IN_COLL_RE.match(uri)
    if m:
        vault, coll_path, rtype, ident = m.group(1), m.group(2), m.group(3), m.group(4)
        ident = _strip_trailing_slash(ident)
        if ident is None:
            return None
        # Doc identifier carries the full vault-relative path so call
        # sites can splice it straight into ``documents.path`` queries
        # without re-joining the collection prefix.
        if rtype == "doc":
            ident = f"{coll_path}/{ident}"
        return ParsedUri(vault=vault, kind=rtype, identifier=ident, coll_path=coll_path)

    m = _URI_COLL_ONLY_RE.match(uri)
    if m:
        vault, coll_path = m.group(1), m.group(2)
        coll_path = coll_path.rstrip("/")
        if not coll_path:
            return None
        return ParsedUri(vault=vault, kind="coll", identifier=coll_path, coll_path=coll_path)

    m = _URI_TYPED_ROOT_RE.match(uri)
    if m:
        vault, rtype, ident = m.group(1), m.group(2), m.group(3)
        ident = _strip_trailing_slash(ident)
        if ident is None:
            return None
        return ParsedUri(vault=vault, kind=rtype, identifier=ident, coll_path=None)

    m = _URI_VAULT_ONLY_RE.match(uri)
    if m:
        return ParsedUri(vault=m.group(1), kind="vault", identifier=None, coll_path=None)

    return None


def _strip_trailing_slash(ident: str) -> str | None:
    """Strip a single trailing slash; reject all-slashes input."""
    if ident.endswith("/"):
        ident = ident.rstrip("/")
        if not ident:
            return None
    return ident


# ── Builders ─────────────────────────────────────────────────────────


def vault_uri(vault: str) -> str:
    """``akb://{vault}`` — the vault root, used as a browse target."""
    return f"akb://{vault}"


def coll_uri(vault: str, coll_path: str) -> str:
    """``akb://{vault}/coll/{path}`` — collection as a browse target."""
    if not coll_path:
        raise ValueError("coll_uri requires a non-empty collection path")
    return f"akb://{vault}/coll/{coll_path}"


def doc_uri(vault: str, path: str) -> str:
    """Build a document URI from the vault-relative ``path``.

    ``path`` is the document's full vault-relative path as stored in
    ``documents.path`` (e.g. ``"specs/api-v2.md"`` or just
    ``"notes.md"`` for a root-level document). The helper splits off
    the parent directory as the collection prefix:

        path="specs/api-v2.md"   → akb://V/coll/specs/doc/api-v2.md
        path="specs/api/v1.md"   → akb://V/coll/specs/api/doc/v1.md
        path="notes.md"          → akb://V/doc/notes.md

    Callers pass ``documents.path`` verbatim — no need to know the
    collection separately.
    """
    if "/" in path:
        coll_path, basename = path.rsplit("/", 1)
        return f"akb://{vault}/coll/{coll_path}/doc/{basename}"
    return f"akb://{vault}/doc/{path}"


def table_uri(vault: str, name: str, collection: str | None = None) -> str:
    """``akb://{vault}/coll/{collection}/table/{name}`` (or the
    root form when ``collection`` is empty/None). Callers fetch
    ``collection`` from ``vault_tables.collection_id`` → ``collections.path``
    when emitting URIs from a list query (see ``_browse_tables_by_depth``)."""
    if collection:
        return f"akb://{vault}/coll/{collection}/table/{name}"
    return f"akb://{vault}/table/{name}"


def file_uri(vault: str, file_id: str, collection: str | None = None) -> str:
    """``akb://{vault}/coll/{collection}/file/{uuid}`` (or the root
    form when ``collection`` is empty/None). Same convention as
    ``table_uri``."""
    if collection:
        return f"akb://{vault}/coll/{collection}/file/{file_id}"
    return f"akb://{vault}/file/{file_id}"


# ── Splitters (for handlers that just want (vault, identifier)) ──────


def split_uri(uri: str, expected_type: str | None = None) -> tuple[str, str]:
    """Parse a typed AKB URI and return ``(vault, identifier)``.

    For documents the returned identifier is the full vault-relative
    path so downstream callers like ``DocumentRepository.find_by_path``
    see the same value they used pre-0.3.0. For tables and files the
    identifier is the table name or file UUID respectively. Raises
    ``ValueError`` on malformed input or type mismatch.

    ``parse_uri`` already does the doc-path reconstruction (see the
    ``ParsedUri`` docstring), so this helper is now just a thin
    expected-type wrapper.
    """
    parsed = parse_uri(uri)
    if parsed is None or parsed.kind in ("vault", "coll"):
        raise ValueError(
            f"Invalid AKB resource URI: '{uri}'. Expected a typed URI "
            f"(doc/table/file)."
        )
    if expected_type and parsed.kind != expected_type:
        raise ValueError(
            f"Expected a {expected_type} URI; got {parsed.kind}: '{uri}'."
        )
    return parsed.vault, parsed.identifier or ""


def split_browse_uri(uri: str) -> tuple[str, str | None]:
    """Parse ``uri`` as a browse target and return ``(vault, collection)``.

    - ``akb://V``                       → ``("V", None)`` — vault root browse
    - ``akb://V/coll/X``                → ``("V", "X")`` — collection-scoped
    - ``akb://V/coll/X/Y``              → ``("V", "X/Y")`` — nested

    Raises ``ValueError`` for typed URIs (those are leaf resources —
    drill into them with ``akb_get`` / ``akb_drill_down`` / ``akb_sql``
    / ``akb_get_file`` instead) and for malformed input.
    """
    parsed = parse_uri(uri)
    if parsed is None:
        raise ValueError(
            f"Invalid browse URI: '{uri}'. Expected akb://<vault> or "
            f"akb://<vault>/coll/<path>."
        )
    if parsed.kind == "vault":
        return parsed.vault, None
    if parsed.kind == "coll":
        return parsed.vault, parsed.coll_path
    raise ValueError(
        f"Browse URI must be a vault root or a coll URI; got "
        f"akb://{parsed.vault}/{parsed.kind}/... — to drill into a "
        f"{parsed.kind}, use the appropriate leaf tool instead."
    )


# No generic ``make_uri`` — every emit site uses the type-specific
# helpers above so the location prefix is built correctly. (Pre-0.3.0
# ``make_uri`` existed and produced the legacy shape; it is gone.)
