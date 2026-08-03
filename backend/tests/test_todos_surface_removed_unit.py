"""Guards for the removal of the `todos` table and its dead service layer.

The MCP tools that drove `todos` (``akb_todo`` / ``akb_todos`` /
``akb_todo_update``) were deleted in PR #43 (`1c57350`) with the table and
``todo_service`` left behind for a "separate cleanup migration" that never
landed. Two live consequences followed, both fixed by removing the stack:

  * ``delete_user_account`` wrote ``UPDATE todos SET assignee_id = NULL`` /
    ``created_by = NULL`` against columns declared ``NOT NULL``. The block has
    no transaction wrapper, so the preceding ``vault_access`` /
    ``publications`` updates committed, the ``NotNullViolationError``
    propagated, and the closing ``DELETE FROM users`` never ran — making
    ``DELETE /api/v1/my/account`` fail permanently for a user holding a
    ``todos`` row outside their own vaults. The e2e suites call that endpoint
    only as teardown with output discarded, so CI never saw it.
  * ``todo_service`` had zero importers — four unreachable functions.

`init.sql` is checked separately from the drop migration because it runs
*before* migrations on every boot (``postgres.init_db``): leaving
``CREATE TABLE IF NOT EXISTS todos`` there would silently resurrect an empty
table after migration 050 had already been recorded in ``schema_migrations``,
and the resurrection would be permanent.
"""

from __future__ import annotations

import re
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = _BACKEND / "app" / "db" / "init.sql"

# `FROM todos`, `UPDATE todos`, `INTO todos`, `JOIN todos`, `TABLE todos`.
_TODOS_SQL = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+todos\b", re.IGNORECASE)
_TODO_SERVICE = re.compile(r"\btodo_service\b")

# The drop migration is the one place allowed to name the table — that is its
# entire job. Everything else must be free of it.
_ALLOWED_DIRS = (_BACKEND / "app" / "db" / "migrations",)


def _sql_without_comments(text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _python_sources() -> list[Path]:
    out: list[Path] = []
    for root in (_BACKEND / "app", _BACKEND / "mcp_server"):
        for path in root.rglob("*.py"):
            if any(allowed in path.parents for allowed in _ALLOWED_DIRS):
                continue
            out.append(path)
    assert out, "no backend python sources discovered — test is mis-scoped"
    return out


def test_init_sql_does_not_create_the_todos_table() -> None:
    """init.sql runs before migrations on every boot; a CREATE here undoes 050."""
    body = _sql_without_comments(_INIT_SQL.read_text())
    hits = [ln.strip() for ln in body.splitlines() if re.search(r"\btodos\b", ln, re.I)]
    assert not hits, (
        "init.sql still emits DDL for `todos`. Because init_db() runs init.sql "
        "before applying migrations, this recreates the table on the next boot "
        "while migration 050 stays recorded as applied — a permanent "
        f"resurrection. Offending lines: {hits}"
    )


def test_no_backend_code_queries_the_todos_table() -> None:
    """Nothing outside the drop migration may read or write `todos`.

    This is the regression guard for the account-deletion failure: the write
    that broke it was ``UPDATE todos SET assignee_id = NULL`` against a
    ``NOT NULL`` column.
    """
    offenders: dict[str, list[str]] = {}
    for path in _python_sources():
        for line in path.read_text().splitlines():
            if _TODOS_SQL.search(line):
                offenders.setdefault(str(path.relative_to(_BACKEND)), []).append(line.strip())

    assert not offenders, (
        "backend code still queries the dropped `todos` table: "
        + "; ".join(f"{f}: {lines}" for f, lines in sorted(offenders.items()))
    )


def test_todo_service_is_gone_and_unreferenced() -> None:
    assert not (_BACKEND / "app" / "services" / "todo_service.py").exists(), (
        "todo_service.py is back — it has no entrypoint (no MCP tool, no REST "
        "router, no UI) and its table is dropped."
    )
    offenders = [
        str(p.relative_to(_BACKEND))
        for p in _python_sources()
        if _TODO_SERVICE.search(p.read_text())
    ]
    assert not offenders, f"todo_service referenced from: {sorted(offenders)}"
