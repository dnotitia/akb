"""Small shared predicates for search scope; values always remain bound parameters."""

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator, model_validator

ArchiveScope = Literal["unarchived", "archived", "all"]


class DocumentTimeRange(BaseModel):
    """Internal, half-open range on the current document's public timestamp."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    field: Literal["created_at", "updated_at"]
    gte: AwareDatetime | None = None
    lt: AwareDatetime | None = None

    @field_validator("gte", "lt")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.gte is None and self.lt is None:
            raise ValueError("A document time range requires at least one bound")
        if self.gte is not None and self.lt is not None and self.gte >= self.lt:
            raise ValueError("gte must precede lt")
        return self

    def includes(self, stamp: datetime) -> bool:
        return (self.gte is None or stamp >= self.gte) and (self.lt is None or stamp < self.lt)

    def add_predicates(self, column: str, conditions: list[str], params: list) -> None:
        """The caller supplies a repository-owned column. Only values are bound."""
        for operator, bound in ((">=", self.gte), ("<", self.lt)):
            if bound is not None:
                params.append(bound)
                conditions.append(f"{column} {operator} ${len(params)}::timestamptz")


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
