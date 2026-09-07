"""Small shared predicates for search scope; values always remain bound parameters."""


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
                     tags: list[str] | None, include_archived: bool) -> bool:
    return (
        (not doc_types or (metadata.get("type") or "note") in doc_types)
        and (not tags or bool(set(metadata.get("tags") or []).intersection(tags)))
        and (include_archived or metadata.get("status", "draft") != "archived")
    )
