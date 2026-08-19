"""Reserved-namespace policy for the vault-skill system collection.

Two-way rule (spec: docs/design/proposal/2026-08-19-vault-skill-system-collection/):
the `overview` path namespace holds ONLY the canonical vault-skill document,
and `doc_type="skill"` exists ONLY at the canonical path (one per vault).

Guards are pure functions over already-normalized values so both document
backends (git-legacy and native-ledger) enforce identically — the native
backend creates no `collections` row for the seed, which is why the rule
binds to the path namespace and not the collections table.

`internal=True` is the bypass for the three sanctioned writers: the vault
seed, the settings reset, and the reservation backfill. It is never exposed
through any API surface.
"""

from __future__ import annotations

from app.exceptions import ForbiddenError

# Canonical location. `mcp_server/help.py` imports this constant (single
# source of truth — it previously lived there).
VAULT_SKILL_PATH = "overview/vault-skill.md"
SKILL_COLLECTION = "overview"
SKILL_DOC_TYPE = "skill"

_RESERVED_MSG = (
    "'overview' is a reserved system collection: it holds only the vault-skill "
    "document. Edit the skill with akb_update on overview/vault-skill.md or the "
    "vault settings UI; create other documents in a different collection."
)
_SKILL_TYPE_MSG = (
    "type='skill' is reserved for the canonical vault-skill document at "
    "overview/vault-skill.md; it cannot be assigned to other documents."
)


def is_reserved_collection(collection: str | None) -> bool:
    """True when a *normalized* collection path falls in the reserved namespace."""
    if not collection:
        return False
    return collection == SKILL_COLLECTION or collection.startswith(SKILL_COLLECTION + "/")


def is_reserved_path(path: str) -> bool:
    """True when a *document path* (collection/slug.md) is inside the namespace."""
    return path.startswith(SKILL_COLLECTION + "/")


def check_put(collection: str | None, doc_type: str, *, internal: bool = False) -> None:
    if internal:
        return
    if is_reserved_collection(collection):
        raise ForbiddenError(_RESERVED_MSG)
    if doc_type == SKILL_DOC_TYPE:
        raise ForbiddenError(_SKILL_TYPE_MSG)


def check_update_type(path: str, new_type: str | None) -> None:
    """Retype rules. Body/other-frontmatter updates are always allowed."""
    # Falsy = type untouched. Both backends merge with `if req.type:`
    # (document_service._update_locked, native _update_from_snapshot), so an
    # empty string is a no-op there and must not raise here.
    if not new_type:
        return
    if path == VAULT_SKILL_PATH:
        if new_type != SKILL_DOC_TYPE:
            raise ForbiddenError(
                "The vault-skill document's type is pinned to 'skill'."
            )
        return
    if new_type == SKILL_DOC_TYPE:
        raise ForbiddenError(_SKILL_TYPE_MSG)


def check_move(old_path: str, new_path: str, *, internal: bool = False) -> None:
    if internal:
        return
    if is_reserved_path(old_path) or is_reserved_path(new_path):
        raise ForbiddenError(
            "Documents cannot be moved into or out of the reserved 'overview' "
            "system collection."
        )


def check_delete(path: str, *, internal: bool = False) -> None:
    if internal:
        return
    if is_reserved_path(path):
        raise ForbiddenError(
            "The vault-skill document cannot be deleted. Use reset-to-template "
            "instead (vault settings, or overwrite the body with akb_update)."
        )


def check_collection_create(path: str) -> None:
    """Symmetric partner to `check_collection_delete`.

    Without this, `akb_create_collection(path="overview/junk")` succeeded and
    the delete guard then refused to remove it — the reservation turned a
    stray row into permanent, undeletable litter.
    """
    if is_reserved_collection(path):
        raise ForbiddenError(
            "Creating collections inside the reserved 'overview' system "
            "namespace is not allowed."
        )


def check_collection_delete(path: str) -> None:
    if is_reserved_collection(path):
        raise ForbiddenError(
            "'overview' is a reserved system collection and cannot be deleted."
        )


def check_resource_collection(collection: str | None) -> None:
    """Files and tables may not be created under the reserved namespace."""
    if is_reserved_collection(collection):
        raise ForbiddenError(
            "'overview' is a reserved system collection: files and tables "
            "cannot be created there."
        )
