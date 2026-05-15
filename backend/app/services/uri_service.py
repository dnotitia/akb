"""AKB URI scheme — unified resource identifiers for cross-type references.

Format: akb://{vault}/{type}/{identifier}
  - akb://my-vault/doc/specs/api-v2.md     → Document
  - akb://my-vault/table/expenses           → Table
  - akb://my-vault/file/550e8400-...        → File

Used by the edges table to connect heterogeneous resources.
"""

from __future__ import annotations

import re

_URI_RE = re.compile(r"^akb://([^/]+)/(doc|table|file)/(.+)$")

# Valid resource types
RESOURCE_TYPES = ("doc", "table", "file")


def make_uri(vault: str, resource_type: str, identifier: str) -> str:
    """Build an AKB URI."""
    return f"akb://{vault}/{resource_type}/{identifier}"


def parse_uri(uri: str) -> tuple[str, str, str] | None:
    """Parse an AKB URI into (vault, type, identifier). Returns None if invalid."""
    m = _URI_RE.match(uri)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def doc_uri(vault: str, path: str) -> str:
    """Shorthand for document URI."""
    return make_uri(vault, "doc", path)


def table_uri(vault: str, name: str) -> str:
    """Shorthand for table URI."""
    return make_uri(vault, "table", name)


def file_uri(vault: str, file_id: str) -> str:
    """Shorthand for file URI."""
    return make_uri(vault, "file", file_id)


def split_uri(uri: str, expected_type: str | None = None) -> tuple[str, str]:
    """Parse an akb:// URI and return (vault, identifier).

    `identifier` is the path for a doc URI, the table name for a table
    URI, the file UUID for a file URI. Raises ValueError if the URI is
    malformed or its type doesn't match `expected_type`.

    Shared helper for MCP handlers and REST routes — both need the same
    parse-and-validate flow before dispatching to the service layer.
    """
    parsed = parse_uri(uri)
    if parsed is None:
        raise ValueError(
            f"Invalid AKB URI: '{uri}'. Expected akb://<vault>/<type>/<id>."
        )
    vault, rtype, ident = parsed
    if expected_type and rtype != expected_type:
        raise ValueError(
            f"Expected a {expected_type} URI; got {rtype}: '{uri}'."
        )
    return vault, ident
