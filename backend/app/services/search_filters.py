"""Small shared predicates for search scope; values always remain bound parameters."""

from typing import Literal

ArchiveScope = Literal["unarchived", "archived", "all"]


def resolve_archive_scope(scope: ArchiveScope | None, include_archived: bool) -> ArchiveScope:
    """Explicit scope wins; absent scope preserves each caller's legacy default."""
    if scope is not None:
        if scope not in ("unarchived", "archived", "all"):
            from app.exceptions import ValidationError
            raise ValidationError("Invalid archive scope")
        return scope
    return "all" if include_archived else "unarchived"


def status_matches(status: str | None, scope: ArchiveScope) -> bool:
    return scope == "all" or (status == "archived") == (scope == "archived")


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def collection_predicate(column: str, collection: str, params: list) -> str:
    """Match a collection itself or descendants, never a similarly named sibling.

    ``column`` is a repository-owned SQL expression, never request input.
    """
    path = collection.strip("/")
    params.extend([path, escape_like(path) + "/%"])
    return f"({column} = ${len(params) - 1} OR {column} LIKE ${len(params)} ESCAPE '\\')"


def metadata_matches(metadata: dict, doc_types: list[str] | None,
                     tags: list[str] | None, include_archived: bool,
                     archive_scope: ArchiveScope | None = None) -> bool:
    return (
        (not doc_types or (metadata.get("type") or "note") in doc_types)
        and (not tags or bool(set(metadata.get("tags") or []).intersection(tags)))
        and status_matches(metadata.get("status"), resolve_archive_scope(archive_scope, include_archived))
    )
