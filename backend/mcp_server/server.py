"""AKB MCP Server — Primary agent interface.

Provides MCP tools for:
- akb_put/get/update/delete: Document CRUD
- akb_browse: Unified vault content view (documents, tables, files)
- akb_search/drill_down: Search and read documents
- akb_link/unlink: Create/remove cross-type relations (doc↔table↔file)
- akb_relations/graph: Query the knowledge graph
- akb_create_table/sql: Structured data tables
- akb_session_start/end: Agent work sessions
- akb_activity/diff/history: Version history
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

# Add backend to path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from app.config import settings
from app.db.postgres import get_pool, init_db, close_pool
from app.exceptions import NotFoundError
from app.services.document_service import DocumentService, EditError
from app.services.search_service import SearchService
from app.services.kg_service import get_resource_relations, get_graph, get_provenance, link_resources, unlink_resources, resolve_doc_to_uri
from app.services.uri_service import doc_uri, table_uri, file_uri
from app.services.access_service import (
    check_vault_access, grant_access, revoke_access, list_vault_members,
    list_accessible_vaults, get_vault_info, search_users, transfer_ownership,
    archive_vault,
)
from app.services.auth_service import resolve_token
from app.services.memory_service import remember, recall, forget
from app.services import publication_service, table_service
from app.services.publication_service import parse_expires_in
from app.services.session_service import SessionService
from app.services import todo_service
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.repositories.document_repo import DocumentRepository

from mcp_server.tools import TOOLS
from mcp_server.help import HELP, _resolve_help


async def _find_doc(vault_name: str, doc_ref: str) -> dict | None:
    """Find a document by any reference (UUID, d-prefix, path substring).

    Returns a full document row dict (with vault_name) or None.
    Shared helper used by MCP handlers to avoid duplicating lookup SQL.
    """
    pool = await get_pool()
    doc_repo = DocumentRepository(pool)
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault_name)
        if not vault:
            return None
        return await doc_repo.find_by_ref_with_conn(conn, vault["id"], doc_ref)

session_service = SessionService()

server = Server("akb")


class _MCPUser:
    """Resolved user from MCP request context."""
    def __init__(self, user_id: str = "00000000-0000-0000-0000-000000000000", username: str = "system"):
        self.user_id = user_id
        self.username = username

_FALLBACK_USER = _MCPUser()


async def _get_user() -> _MCPUser:
    """Get authenticated user from MCP request context.

    Uses the standard MCP SDK mechanism: server.request_context.request
    contains the original HTTP Request, from which we extract the
    Authorization header and resolve the user via PAT.
    """
    try:
        ctx = server.request_context
        request = ctx.request  # Starlette Request object
        if request:
            auth_header = request.headers.get("authorization", "")
            if auth_header:
                user = await resolve_token(auth_header)
                if user:
                    return _MCPUser(user.user_id, user.username)
    except (LookupError, AttributeError):
        pass
    return _FALLBACK_USER
doc_service = DocumentService()
search_service = SearchService()


# ── Handler Registry ────────────────────────────────────────────

_HANDLERS: dict[str, Any] = {}


def _h(name: str):
    """Register an MCP tool handler."""
    def decorator(fn):
        _HANDLERS[name] = fn
        return fn
    return decorator


@_h("akb_help")
async def _handle_help(args: dict, uid: str, user: _MCPUser) -> dict:
    topic = args.get("topic")
    return {"help": _resolve_help(topic)}


@_h("akb_list_vaults")
async def _handle_list_vaults(args: dict, uid: str, user: _MCPUser) -> dict:
    vaults = await list_accessible_vaults(uid)
    return {"vaults": vaults}


@_h("akb_create_vault")
async def _handle_create_vault(args: dict, uid: str, user: _MCPUser) -> dict:
    try:
        vault_id = await doc_service.create_vault(
            args["name"], args.get("description", ""),
            owner_id=uid, template=args.get("template"),
            public_access=args.get("public_access", "none"),
            external_git=args.get("external_git"),
        )
    except ValueError as e:
        return {"error": str(e)}
    response = {
        "vault_id": vault_id, "name": args["name"],
        "template": args.get("template"),
        "public_access": args.get("public_access", "none"),
    }
    if args.get("external_git"):
        response["external_git"] = {
            "url": args["external_git"]["url"],
            "branch": args["external_git"].get("branch") or "main",
            "read_only": True,
        }
    return response


@_h("akb_put")
async def _handle_put(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="writer")
    req = DocumentPutRequest(
        vault=args["vault"],
        collection=args["collection"],
        title=args["title"],
        content=args["content"],
        type=args.get("type", "note"),
        tags=args.get("tags", []),
        domain=args.get("domain"),
        summary=args.get("summary"),
        depends_on=args.get("depends_on", []),
        related_to=args.get("related_to", []),
    )
    try:
        result = await doc_service.put(req, agent_id=user.username)
    except ValueError as e:
        return {"error": str(e)}
    return result.model_dump()


@_h("akb_get")
async def _handle_get(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="reader")
    version = args.get("version")
    if version:
        # Read specific version from Git
        doc = await _find_doc(args["vault"], args["doc_id"])
        if not doc:
            return {"error": "Document not found"}
        from app.services.git_service import GitService
        git = GitService()
        content = git.read_file(args["vault"], doc["path"], commit=version)
        if content is None:
            return {"error": f"Version not found: {version}"}
        return {"title": doc["title"], "path": doc["path"], "version": version, "content": content}
    else:
        doc = await doc_service.get(args["vault"], args["doc_id"])
        if not doc:
            return {"error": "Document not found"}
        return doc.model_dump()


@_h("akb_update")
async def _handle_update(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="writer")
    req = DocumentUpdateRequest(
        content=args.get("content"),
        title=args.get("title"),
        status=args.get("status"),
        tags=args.get("tags"),
        summary=args.get("summary"),
        depends_on=args.get("depends_on"),
        related_to=args.get("related_to"),
        message=args.get("message"),
    )
    result = await doc_service.update(args["vault"], args["doc_id"], req, agent_id=user.username)
    if not result:
        return {"error": "Document not found"}
    return result.model_dump()


@_h("akb_edit")
async def _handle_edit(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="writer")
    try:
        result = await doc_service.edit(
            args["vault"], args["doc_id"],
            old_string=args["old_string"],
            new_string=args["new_string"],
            replace_all=args.get("replace_all", False),
            message=args.get("message"),
            agent_id=user.username,
        )
        return result.model_dump()
    except EditError as e:
        return {
            "error": "edit_failed",
            "message": str(e),
            "hint": "Use akb_get to verify current content, then retry with adjusted old_string.",
        }


@_h("akb_delete")
async def _handle_delete(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="writer")
    success = await doc_service.delete(args["vault"], args["doc_id"], agent_id=user.username)
    return {"deleted": success}


@_h("akb_browse")
async def _handle_browse(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="reader")
    result = await doc_service.browse(
        args["vault"],
        collection=args.get("collection"),
        depth=args.get("depth", 1),
        content_type=args.get("content_type", "all"),
    )
    return result.model_dump()


@_h("akb_search")
async def _handle_search(args: dict, uid: str, user: _MCPUser) -> dict:
    result = await search_service.search(
        query=args["query"],
        vault=args.get("vault"),
        collection=args.get("collection"),
        doc_type=args.get("type"),
        tags=args.get("tags"),
        limit=args.get("limit", 10),
    )
    return result.model_dump()


@_h("akb_grep")
async def _handle_grep(args: dict, uid: str, user: _MCPUser) -> dict:
    # Read access check when vault is specified
    if args.get("vault"):
        await check_vault_access(uid, args["vault"], required_role="reader")
    replace = args.get("replace")
    if replace is not None:
        # Replace requires writer access on the target vault
        if args.get("vault"):
            await check_vault_access(uid, args["vault"], required_role="writer")
        else:
            return {"error": "vault is required when using replace"}
    result = await search_service.grep(
        pattern=args["pattern"],
        vault=args.get("vault"),
        collection=args.get("collection"),
        regex=args.get("regex", False),
        case_sensitive=args.get("case_sensitive", False),
        replace=replace,
        doc_service=doc_service if replace is not None else None,
        agent_id=user.username if replace is not None else None,
        user_id=uid,
        limit=args.get("limit", 20),
    )
    return result


@_h("akb_drill_down")
async def _handle_drill_down(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="reader")
    sections = await search_service.drill_down(
        args["vault"],
        args["doc_id"],
        section=args.get("section"),
    )
    return {"doc_id": args["doc_id"], "vault": args["vault"], "sections": sections}


@_h("akb_session_start")
async def _handle_session_start(args: dict, uid: str, user: _MCPUser) -> dict:
    return await session_service.start_session(
        args["vault"], args["agent_id"], args.get("context"),
    )


@_h("akb_session_end")
async def _handle_session_end(args: dict, uid: str, user: _MCPUser) -> dict:
    return await session_service.end_session(
        args["session_id"], args.get("summary"), user_id=uid,
    )


@_h("akb_activity")
async def _handle_activity(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="reader")
    from app.services.git_service import GitService
    git = GitService()
    entries = git.vault_log(
        args["vault"],
        max_count=args.get("limit", 20),
        since=args.get("since"),
        path=args.get("collection"),  # Git-native path filter (like git log -- <path>)
    )
    # Filter by author (post-filter, Git doesn't support Korean author filter well)
    author = args.get("author")
    if author:
        entries = [e for e in entries if author.lower() in e.get("agent", "").lower() or author.lower() in e.get("author", "").lower()]
    return {"vault": args["vault"], "total": len(entries), "activity": entries}


@_h("akb_diff")
async def _handle_diff(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="reader")
    doc = await _find_doc(args["vault"], args["doc_id"])
    if not doc:
        return {"error": f"Document not found: {args['doc_id']}"}
    from app.services.git_service import GitService
    git = GitService()
    return git.file_diff(args["vault"], doc["path"], args["commit"])


@_h("akb_relations")
async def _handle_relations(args: dict, uid: str, user: _MCPUser) -> dict:
    resource_uri = args.get("resource_uri")
    if not resource_uri:
        return {"error": "resource_uri is required"}
    access = await check_vault_access(uid, args["vault"], required_role="reader")
    relations = await get_resource_relations(
        args["vault"],
        resource_uri,
        vault_id=access["vault_id"],
        direction=args.get("direction", "both"),
        relation_type=args.get("type"),
    )
    return {"resource_uri": resource_uri, "relations": relations}


@_h("akb_graph")
async def _handle_graph(args: dict, uid: str, user: _MCPUser) -> dict:
    access = await check_vault_access(uid, args["vault"], required_role="reader")
    resource_uri = args.get("resource_uri")
    return await get_graph(
        args["vault"],
        resource_uri=resource_uri,
        depth=args.get("depth", 2),
        limit=args.get("limit", 50),
        vault_id=access["vault_id"],
    )


@_h("akb_link")
async def _handle_link(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="writer")
    return await link_resources(
        args["vault"],
        args["source"], args["target"], args["relation"],
        created_by=user.username,
    )


@_h("akb_unlink")
async def _handle_unlink(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="writer")
    return await unlink_resources(
        args["source"], args["target"],
        relation_type=args.get("relation"),
    )


@_h("akb_provenance")
async def _handle_provenance(args: dict, uid: str, user: _MCPUser) -> dict:
    # Resolve the doc's vault first so we can refuse if the caller lacks
    # reader access. provenance has no vault arg in its public schema, so
    # the lookup here is the only place authority can be enforced.
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT v.name AS vault_name, v.id AS vault_id
            FROM documents d JOIN vaults v ON d.vault_id = v.id
            WHERE d.id::text = $1 OR d.metadata->>'id' = $1
            """,
            args["doc_id"],
        )
    if not row:
        return {"error": "Document not found"}
    await check_vault_access(uid, row["vault_name"], required_role="reader")
    return await get_provenance(args["doc_id"], vault_id=row["vault_id"])


@_h("akb_create_table")
async def _handle_create_table(args: dict, uid: str, user: _MCPUser) -> dict:
    access = await check_vault_access(uid, args["vault"], required_role="writer")
    try:
        return await table_service.create_table(
            access["vault_id"], args["name"], args["columns"],
            actor_id=user.username, description=args.get("description", ""),
        )
    except ValueError as e:
        return {"error": str(e)}


@_h("akb_sql")
async def _handle_sql(args: dict, uid: str, user: _MCPUser) -> dict:
    sql = args["sql"].strip()
    vaults = args.get("vaults") or ([args["vault"]] if args.get("vault") else [])
    if not vaults:
        return {"error": "Must specify vault or vaults parameter"}

    # Check access on all referenced vaults — minimum reader
    # Collect the lowest role across all vaults
    read_only = False
    for v in vaults:
        access = await check_vault_access(uid, v, required_role="reader")
        if access["role"] == "reader":
            read_only = True

    # PostgreSQL SET TRANSACTION READ ONLY enforces write prevention
    # at the DB level — no SQL parsing tricks can bypass it
    return await table_service.execute_sql(vaults, sql, read_only=read_only)


@_h("akb_drop_table")
async def _handle_drop_table(args: dict, uid: str, user: _MCPUser) -> dict:
    access = await check_vault_access(uid, args["vault"], required_role="admin")
    try:
        return await table_service.drop_table(
            access["vault_id"], args["table"], actor_id=user.username,
        )
    except NotFoundError as e:
        return {"error": str(e)}


@_h("akb_alter_table")
async def _handle_alter_table(args: dict, uid: str, user: _MCPUser) -> dict:
    access = await check_vault_access(uid, args["vault"], required_role="admin")
    try:
        return await table_service.alter_table(
            access["vault_id"], args["table"],
            actor_id=user.username,
            add_columns=args.get("add_columns"),
            drop_columns=args.get("drop_columns"),
            rename_columns=args.get("rename_columns"),
        )
    except NotFoundError as e:
        return {"error": str(e)}


@_h("akb_todo")
async def _handle_todo(args: dict, uid: str, user: _MCPUser) -> dict:
    assignee_username = args.get("assignee")
    if assignee_username:
        assignee_id = await todo_service.resolve_user_id(assignee_username)
        if not assignee_id:
            return {"error": f"User not found: {assignee_username}"}
    else:
        assignee_id = uid
        assignee_username = user.username
    result = await todo_service.create_todo(
        assignee_id=assignee_id, created_by=uid, title=args["title"],
        note=args.get("note"), vault_name=args.get("vault"),
        ref_doc=args.get("ref_doc"), priority=args.get("priority", "normal"),
        due_date=args.get("due_date"),
    )
    result["assignee"] = assignee_username
    return result


@_h("akb_todos")
async def _handle_todos(args: dict, uid: str, user: _MCPUser) -> dict:
    if args.get("assignee"):
        assignee_id = await todo_service.resolve_user_id(args["assignee"])
        if not assignee_id:
            return {"error": f"User not found: {args['assignee']}"}
    else:
        assignee_id = uid
    return await todo_service.list_todos(
        assignee_id=assignee_id, status=args.get("status", "open"),
        vault_name=args.get("vault"), limit=args.get("limit", 20),
    )


@_h("akb_todo_update")
async def _handle_todo_update(args: dict, uid: str, user: _MCPUser) -> dict:
    update_args = {k: v for k, v in args.items() if k != "todo_id"}
    if "assignee" in update_args:
        aid = await todo_service.resolve_user_id(update_args.pop("assignee"))
        if not aid:
            return {"error": f"User not found"}
        update_args["assignee_id"] = aid
    return await todo_service.update_todo(args["todo_id"], **update_args)


@_h("akb_remember")
async def _handle_remember(args: dict, uid: str, user: _MCPUser) -> dict:
    return await remember(uid, args["content"], args.get("category", "general"))


@_h("akb_recall")
async def _handle_recall(args: dict, uid: str, user: _MCPUser) -> dict:
    memories = await recall(uid, args.get("category"), args.get("limit", 20))
    return {"memories": memories, "total": len(memories)}


@_h("akb_forget")
async def _handle_forget(args: dict, uid: str, user: _MCPUser) -> dict:
    success = await forget(uid, args["memory_id"])
    return {"forgotten": success}


_SYSTEM_UID = "00000000-0000-0000-0000-000000000000"


@_h("akb_publish")
async def _handle_publish(args: dict, uid: str, user: _MCPUser) -> dict:
    """Create a publication for a document, table query, or file.

    Calling with (vault, doc_id) creates a default document publication.
    For more control use resource_type, expires_in, password, etc.
    """
    await check_vault_access(uid, args["vault"], required_role="writer")
    resource_type = args.get("resource_type", "document")
    created_by = uuid.UUID(uid) if uid and uid != _SYSTEM_UID else None
    try:
        result = await publication_service.create_publication_for_vault(
            vault_name=args["vault"],
            resource_type=resource_type,
            doc_id=args.get("doc_id"),
            file_id=args.get("file_id"),
            query_sql=args.get("query_sql"),
            query_vault_names=args.get("query_vault_names"),
            query_params=args.get("query_params"),
            password=args.get("password"),
            max_views=args.get("max_views"),
            expires_in=args.get("expires_in"),
            title=args.get("title"),
            mode=args.get("mode", "live"),
            section_filter=args.get("section"),
            allow_embed=args.get("allow_embed", True),
            created_by=created_by,
        )
    except ValueError as e:
        return {"error": str(e)}

    return {
        "published": True,
        "public_url": result["public_url"],
        "public_url_full": result["public_url_full"],
        "public_base": result["public_base"],
        "slug": result["slug"],
        "publication_id": result["publication_id"],
        "resource_type": resource_type,
        "expires_at": result.get("expires_at"),
        "password_protected": result.get("password_protected", False),
    }


@_h("akb_unpublish")
async def _handle_unpublish(args: dict, uid: str, user: _MCPUser) -> dict:
    """Delete a publication by slug, or all publications for a given document."""
    await check_vault_access(uid, args["vault"], required_role="writer")

    if args.get("slug"):
        deleted = await publication_service.delete_publication(slug=args["slug"])
        return {"published": False, "deleted": deleted}

    if args.get("doc_id"):
        doc = await _find_doc(args["vault"], args["doc_id"])
        if not doc:
            return {"error": "Document not found"}
        count = await publication_service.delete_publications_for_document(doc["id"])
        return {"published": False, "deleted_publications": count}

    return {"error": "Either slug or doc_id is required"}


@_h("akb_publications")
async def _handle_publications(args: dict, uid: str, user: _MCPUser) -> dict:
    """List all publications in a vault."""
    access = await check_vault_access(uid, args["vault"], required_role="reader")
    publications = await publication_service.list_publications(
        access["vault_id"], args.get("resource_type"),
    )
    return {"publications": publications, "total": len(publications)}


@_h("akb_publication_snapshot")
async def _handle_publication_snapshot(args: dict, uid: str, user: _MCPUser) -> dict:
    """Create a snapshot of a table_query publication."""
    await check_vault_access(uid, args["vault"], required_role="writer")
    try:
        sid = uuid.UUID(args["publication_id"])
    except ValueError:
        return {"error": "Invalid publication_id format"}
    try:
        return await publication_service.create_snapshot(sid)
    except publication_service.PublicationError as e:
        return {"error": e.message}
    except Exception as e:
        return {"error": str(e)}


@_h("akb_vault_info")
async def _handle_vault_info(args: dict, uid: str, user: _MCPUser) -> dict:
    # TODO: pass user_id from auth context when MCP HTTP is used
    return await get_vault_info(uid, args["vault"])


@_h("akb_vault_members")
async def _handle_vault_members(args: dict, uid: str, user: _MCPUser) -> dict:
    return {"members": await list_vault_members(uid, args["vault"])}


@_h("akb_grant")
async def _handle_grant(args: dict, uid: str, user: _MCPUser) -> dict:
    return await grant_access(uid, args["vault"], args["user"], args["role"])


@_h("akb_revoke")
async def _handle_revoke(args: dict, uid: str, user: _MCPUser) -> dict:
    return await revoke_access(uid, args["vault"], args["user"])


@_h("akb_search_users")
async def _handle_search_users(args: dict, uid: str, user: _MCPUser) -> dict:
    users = await search_users(args.get("query"), args.get("limit", 20))
    return {"users": users}


@_h("akb_whoami")
async def _handle_whoami(args: dict, uid: str, user: _MCPUser) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, display_name, email, is_admin, created_at FROM users WHERE id = $1",
            uuid.UUID(uid),
        )
        if not row:
            return {"error": "User not found"}
        return {
            "user_id": str(row["id"]),
            "username": row["username"],
            "display_name": row["display_name"],
            "email": row["email"],
            "is_admin": row["is_admin"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }


@_h("akb_update_profile")
async def _handle_update_profile(args: dict, uid: str, user: _MCPUser) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        sets, params, idx = [], [], 1
        if "display_name" in args:
            sets.append(f"display_name = ${idx}")
            params.append(args["display_name"])
            idx += 1
        if "email" in args:
            sets.append(f"email = ${idx}")
            params.append(args["email"])
            idx += 1
        if not sets:
            return {"error": "Nothing to update"}
        params.append(uid)
        await conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ${idx}", *params)
        row = await conn.fetchrow("SELECT username, display_name, email FROM users WHERE id = $1", uid)
        return {"updated": True, "username": row["username"], "display_name": row["display_name"], "email": row["email"]}


@_h("akb_transfer_ownership")
async def _handle_transfer_ownership(args: dict, uid: str, user: _MCPUser) -> dict:
    return await transfer_ownership(uid, args["vault"], args["new_owner"])


@_h("akb_archive_vault")
async def _handle_archive_vault(args: dict, uid: str, user: _MCPUser) -> dict:
    return await archive_vault(uid, args["vault"])


@_h("akb_delete_vault")
async def _handle_delete_vault(args: dict, uid: str, user: _MCPUser) -> dict:
    from app.services.access_service import delete_vault
    return await delete_vault(uid, args["vault"])


@_h("akb_history")
async def _handle_history(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="reader")
    doc = await _find_doc(args["vault"], args["doc_id"])
    if not doc:
        return {"error": f"Document not found: {args['doc_id']}"}
    from app.services.git_service import GitService
    git = GitService()
    history = git.file_log(args["vault"], doc["path"], max_count=args.get("limit", 20))
    return {"doc_id": args["doc_id"], "path": doc["path"], "history": history}


@_h("akb_set_public")
async def _handle_set_public(args: dict, uid: str, user: _MCPUser) -> dict:
    from app.services.access_service import validate_public_access
    await check_vault_access(uid, args["vault"], required_role="owner")
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", args["vault"])
        # Support: "none", "reader", "writer"
        level = args.get("level")
        if level is None:
            # Legacy boolean support
            is_public = args.get("is_public", True)
            level = "reader" if is_public else "none"
        level = validate_public_access(level)
        await conn.execute("UPDATE vaults SET public_access = $1 WHERE id = $2", level, vault["id"])
    return {"vault": args["vault"], "public_access": level}


# ── Tool Handlers ────────────────────────────────────────────

@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


async def _dispatch(name: str, args: dict):
    user = await _get_user()
    uid = user.user_id

    handler = _HANDLERS.get(name)
    if not handler:
        return {"error": f"Unknown tool: {name}"}
    return await handler(args, uid, user)


# ── Entry point ──────────────────────────────────────────────

async def main():
    await init_db()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
