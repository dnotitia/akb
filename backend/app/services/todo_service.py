"""Todo service — personal task assignment and tracking."""

from __future__ import annotations

import uuid
from datetime import date as _date, datetime, timezone

from app.db.postgres import get_pool


async def create_todo(
    assignee_id: str,
    created_by: str,
    title: str,
    note: str | None = None,
    vault_name: str | None = None,
    ref_uri: str | None = None,
    priority: str = "normal",
    due_date: str | None = None,
) -> dict:
    """Create a todo. `ref_uri` (optional) is the canonical
    `akb://{vault}/doc/{path}` handle for a related document — the
    only resource type todos can link today. We resolve it to the
    internal `ref_doc_id` for storage; the DB column name is
    historical and stays as-is."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Resolve vault
        vault_id = None
        if vault_name:
            v = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault_name)
            if v:
                vault_id = v["id"]

        # Resolve ref doc from URI.
        ref_doc_id = None
        if ref_uri:
            from app.services.uri_service import parse_uri
            parsed = parse_uri(ref_uri)
            if parsed is not None and parsed[1] == "doc":
                ref_vault, _rtype, ref_path = parsed
                d = await conn.fetchrow(
                    """
                    SELECT d.id FROM documents d JOIN vaults v ON d.vault_id = v.id
                     WHERE v.name = $1 AND d.path = $2
                    """,
                    ref_vault, ref_path,
                )
                if d:
                    ref_doc_id = d["id"]

        # Parse due date
        due = None
        if due_date:
            due = _date.fromisoformat(due_date)

        row = await conn.fetchrow(
            """INSERT INTO todos (assignee_id, created_by, vault_id, title, note, ref_doc_id, priority, due_date)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               RETURNING id, created_at""",
            uuid.UUID(assignee_id), uuid.UUID(created_by), vault_id,
            title, note, ref_doc_id, priority, due,
        )
        return {"todo_id": str(row["id"]), "title": title, "priority": priority, "created_at": row["created_at"]}


async def list_todos(
    assignee_id: str,
    status: str = "open",
    vault_name: str | None = None,
    limit: int = 20,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        # `ref_doc_vault` + `ref_doc_path` come from the LEFT JOIN; when
        # the linked doc is in a different vault than the todo's own
        # vault_id, the JOIN still works because documents carry their
        # own vault_id. We build `ref_uri` from those two below.
        query = """
            SELECT t.id, t.title, t.note, t.priority, t.status, t.due_date,
                   t.created_at, t.completed_at,
                   u1.username as assignee, u2.username as created_by,
                   v.name as vault,
                   dv.name as ref_doc_vault,
                   d.path as ref_doc_path
            FROM todos t
            JOIN users u1 ON t.assignee_id = u1.id
            JOIN users u2 ON t.created_by = u2.id
            LEFT JOIN vaults v ON t.vault_id = v.id
            LEFT JOIN documents d ON t.ref_doc_id = d.id
            LEFT JOIN vaults dv ON d.vault_id = dv.id
            WHERE t.assignee_id = $1
        """
        params: list = [uuid.UUID(assignee_id)]
        idx = 2

        if status != "all":
            query += f" AND t.status = ${idx}"
            params.append(status)
            idx += 1

        if vault_name:
            query += f" AND v.name = ${idx}"
            params.append(vault_name)
            idx += 1

        query += f" ORDER BY CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, t.created_at DESC LIMIT ${idx}"
        params.append(limit)

        rows = await conn.fetch(query, *params)
        todos = []
        for r in rows:
            todo: dict = {
                "todo_id": str(r["id"]),
                "title": r["title"],
                "priority": r["priority"],
                "status": r["status"],
                "assignee": r["assignee"],
                "created_by": r["created_by"],
                "created_at": r["created_at"],
            }
            if r["note"]:
                todo["note"] = r["note"]
            if r["vault"]:
                todo["vault"] = r["vault"]
            if r["ref_doc_vault"] and r["ref_doc_path"]:
                from app.services.uri_service import doc_uri
                todo["ref_uri"] = doc_uri(r["ref_doc_vault"], r["ref_doc_path"])
            if r["due_date"]:
                todo["due_date"] = r["due_date"]
            if r["completed_at"]:
                todo["completed_at"] = r["completed_at"]
            todos.append(todo)

        return {"total": len(todos), "todos": todos}


async def update_todo(todo_id: str, **kwargs) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        todo = await conn.fetchrow("SELECT * FROM todos WHERE id = $1", uuid.UUID(todo_id))
        if not todo:
            return {"error": "Todo not found"}

        sets, params, idx = [], [], 1
        if "status" in kwargs:
            sets.append(f"status = ${idx}")
            params.append(kwargs["status"])
            idx += 1
            if kwargs["status"] == "done":
                sets.append(f"completed_at = ${idx}")
                params.append(datetime.now(timezone.utc))
                idx += 1
        if "title" in kwargs:
            sets.append(f"title = ${idx}")
            params.append(kwargs["title"])
            idx += 1
        if "note" in kwargs:
            sets.append(f"note = ${idx}")
            params.append(kwargs["note"])
            idx += 1
        if "priority" in kwargs:
            sets.append(f"priority = ${idx}")
            params.append(kwargs["priority"])
            idx += 1
        if "due_date" in kwargs:
            sets.append(f"due_date = ${idx}")
            params.append(_date.fromisoformat(kwargs["due_date"]))
            idx += 1
        if "assignee_id" in kwargs:
            sets.append(f"assignee_id = ${idx}")
            params.append(uuid.UUID(kwargs["assignee_id"]))
            idx += 1

        if not sets:
            return {"error": "Nothing to update"}

        params.append(uuid.UUID(todo_id))
        await conn.execute(f"UPDATE todos SET {', '.join(sets)} WHERE id = ${idx}", *params)
        return {"updated": True, "todo_id": todo_id}


async def resolve_user_id(username: str) -> str | None:
    """Resolve username to user_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE username = $1", username)
        return str(row["id"]) if row else None
