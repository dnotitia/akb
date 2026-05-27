"""AKB MCP Server — Primary agent interface.

Provides MCP tools for:
- akb_put/get/update/delete: Document CRUD
- akb_browse: Unified vault content view (documents, tables, files)
- akb_search/drill_down: Search and read documents
- akb_link/unlink: Create/remove cross-type relations (doc↔table↔file)
- akb_relations/graph: Query the knowledge graph
- akb_create_table/sql: Structured data tables
- akb_activity/diff/history: Version history
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

# Git commit hashes only — same regex the REST layer (documents.py)
# applies to ?version=. MCP must validate too: pre-fix, an MCP caller
# could send version="HEAD~1" / "refs/heads/main" / "@~5" and have it
# resolved by GitPython, leaking historical content the REST trust
# boundary rejects (audit-v2 F-F2).
_HEX_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")

# Add backend to path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from app.db.postgres import get_pool, init_db, close_pool
from app.exceptions import ConflictError, NotFoundError
from app.services.document_service import DocumentService, EditError
from app.services.search_service import SearchService
from app.services.kg_service import get_resource_relations, get_graph, get_provenance, link_resources, unlink_resources
from app.services.uri_service import doc_uri, parse_uri, split_uri
from app.services.access_service import (
    check_vault_access, grant_access, revoke_access, list_vault_members,
    list_accessible_vaults, get_vault_info, search_users, transfer_ownership,
    archive_vault,
)
from app.services.auth_service import resolve_token
from app.services.memory_service import remember, recall, forget
from app.util.text import to_nfc
from app.services import publication_service, table_service
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.repositories.document_repo import DocumentRepository

from mcp_server.tools import TOOLS
from mcp_server.help import _resolve_help
from mcp_server.instructions import INSTRUCTIONS


async def _find_doc(vault_name: str, doc_ref: str) -> dict | None:
    """Find a document by reference (path within the vault, or the legacy
    `d-…` prefix surfaced inside `akb://…/doc/d-XXXXXXXX` URIs).

    Returns a full document row dict (with vault_name) or None.
    """
    pool = await get_pool()
    doc_repo = DocumentRepository(pool)
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault_name)
        if not vault:
            return None
        return await doc_repo.find_by_ref_with_conn(conn, vault["id"], doc_ref)



server = Server("akb", instructions=INSTRUCTIONS)


class _MCPUser:
    """Resolved user from MCP request context."""
    def __init__(
        self,
        user_id: str = "00000000-0000-0000-0000-000000000000",
        username: str = "system",
        is_admin: bool = False,
    ):
        self.user_id = user_id
        self.username = username
        self.is_admin = is_admin

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
                    return _MCPUser(user.user_id, user.username, is_admin=user.is_admin)
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


def _paginate(items_or_payload, args: dict, items_key: str = "vaults") -> dict:
    """Apply offset/limit to a list and attach total/returned.

    Accepts either a bare list (wrapped under `items_key`) or an
    already-shaped payload dict (sliced in place). Used by the
    handlers that need to fit large result sets in the agent
    client's truncate window.
    """
    if isinstance(items_or_payload, list):
        items = items_or_payload
        payload: dict = {}
    else:
        payload = items_or_payload
        items = payload.get(items_key) or []

    total = len(items)
    offset = max(0, int(args.get("offset") or 0))
    limit = args.get("limit")
    if isinstance(limit, int) and limit > 0:
        items = items[offset : offset + limit]
    elif offset:
        items = items[offset:]

    payload[items_key] = items
    payload["total"] = total
    payload["returned"] = len(items)
    if total > len(items):
        payload["truncated"] = True
        payload["hint"] = (
            f"Showing {len(items)} of {total}. "
            f"Use `filter` to narrow, or `limit`/`offset` to page."
        )
    return payload


def _filter_arg(args: dict) -> str:
    """Return the substring filter (case-insensitive, stripped).

    Accepts `filter` (canonical) or `query` (legacy alias) — list_vaults
    and browse historically used `query`, but `query` now collides with
    `akb_search.query` (a semantic retrieval string). New callers should
    pass `filter`; `query` stays accepted for one minor release.
    """
    raw = args.get("filter") or args.get("query") or ""
    return raw.strip().lower()


@_h("akb_help")
async def _handle_help(args: dict, uid: str, user: _MCPUser) -> dict:
    topic = args.get("topic")
    vault = args.get("vault")
    if topic == "vault-skill" and vault:
        from mcp_server.help import render_vault_skill_response
        async def _fetch(v, doc_id):
            try:
                resp = await doc_service.get(v, doc_id)
            except Exception:
                return None
            # DocumentResponse fields: .content (from git), .current_commit, .updated_at
            return {
                "content": resp.content or "",
                "commit": resp.current_commit,
                "updated_at": str(resp.updated_at or ""),
            }
        return {"help": await render_vault_skill_response(vault, _fetch)}
    return {"help": _resolve_help(topic)}


@_h("akb_list_vaults")
async def _handle_list_vaults(args: dict, uid: str, user: _MCPUser) -> dict:
    # Slim {name, description} only — full metadata bloats the payload
    # past the agent client's truncate cap in large tenants. REST
    # callers that need full rows use `GET /api/v1/vaults`.
    vaults = await list_accessible_vaults(uid)
    include_archived = args.get("include_archived")
    needle = _filter_arg(args)

    slim = []
    for v in vaults:
        if not include_archived and v.get("status") == "archived":
            continue
        name = v["name"]
        description = v.get("description") or ""
        if needle and needle not in name.lower() and needle not in description.lower():
            continue
        slim.append({"name": name, "description": description})

    return _paginate(slim, args, items_key="vaults")


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
    try:
        vault, collection = _resolve_parent(args, kind_name="document")
    except ValueError as e:
        return {"error": str(e)}
    await check_vault_access(uid, vault, required_role="writer")
    req = DocumentPutRequest(
        vault=vault,
        collection=collection,
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


def _resolve_parent(args: dict, *, kind_name: str) -> tuple[str, str]:
    """Decode the `parent` URI form for write tools (akb_put,
    akb_create_table, akb_put_file) and return ``(vault, collection)``.
    If `parent` is absent, fall back to the legacy ``vault`` +
    ``collection`` pair. Raises ``ValueError`` on a malformed `parent`
    URI, a leaf-resource URI (those aren't valid parents — you don't
    put a {kind_name} *inside* a doc/table/file), or when neither
    `parent` nor `vault` is supplied.

    Centralised so all three write tools agree on the rule. Mirrors
    the equivalent logic in ``_handle_browse`` (which also takes a
    URI-or-coordinate form)."""
    from app.services.uri_service import split_browse_uri
    parent = args.get("parent")
    if parent:
        try:
            vault, coll = split_browse_uri(parent)
        except ValueError as e:
            raise ValueError(
                f"Invalid `parent` URI for {kind_name}: {e}"
            ) from e
        return vault, coll or ""
    vault = args.get("vault")
    if not vault:
        raise ValueError(
            f"Either `parent` (akb:// URI) or `vault` is required to "
            f"create a {kind_name}."
        )
    return vault, args.get("collection") or ""


@_h("akb_get")
async def _handle_get(args: dict, uid: str, user: _MCPUser) -> dict:
    vault, doc_path = split_uri(args["uri"], expected_type="doc")
    doc_path = to_nfc(doc_path)
    await check_vault_access(uid, vault, required_role="reader")
    version = args.get("version")
    if version:
        if not _HEX_COMMIT_RE.fullmatch(version):
            return {
                "error": (
                    "version must be a 7-64 char lowercase hex commit hash; "
                    "symbolic refs (HEAD~N, refs/heads/main, ...) are not accepted"
                )
            }
        # Read specific version from Git. Strip frontmatter (yaml meta
        # block at top) before returning content — the un-versioned
        # akb_get path does the same via doc_service.get, and any
        # internal-id fields living in old frontmatter (legacy d-prefix)
        # must not leak out of the MCP boundary.
        import frontmatter as _fm
        doc = await _find_doc(vault, doc_path)
        if not doc:
            return {"error": "Document not found"}
        from app.services.git_service import GitService
        git = GitService()
        raw = git.read_file(vault, doc["path"], commit=version)
        if raw is None:
            return {"error": f"Version not found: {version}"}
        try:
            body = _fm.loads(raw).content
        except Exception:
            # YAML parser choked (rare — happens on historical commits
            # with malformed frontmatter). Strip a leading `---\n…\n---`
            # block by regex so the response body never leaks the yaml
            # header verbatim (which on legacy commits still carries
            # `id: d-XXXXXXXX`). If no `---` fence is present we ship
            # the raw content — it has no frontmatter to leak.
            import re as _re
            stripped = _re.sub(
                r"\A---\r?\n.*?\r?\n---\r?\n", "", raw, count=1, flags=_re.DOTALL,
            )
            body = stripped
        return {
            "title": doc["title"],
            "uri": doc_uri(vault, doc["path"]),
            "version": version,
            "content": body,
        }
    doc = await doc_service.get(vault, doc_path)
    if not doc:
        return {"error": "Document not found"}
    return doc.model_dump()


@_h("akb_update")
async def _handle_update(args: dict, uid: str, user: _MCPUser) -> dict:
    vault, doc_path = split_uri(args["uri"], expected_type="doc")
    doc_path = to_nfc(doc_path)
    await check_vault_access(uid, vault, required_role="writer")
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
    result = await doc_service.update(vault, doc_path, req, agent_id=user.username)
    if not result:
        return {"error": "Document not found"}
    return result.model_dump()


@_h("akb_edit")
async def _handle_edit(args: dict, uid: str, user: _MCPUser) -> dict:
    vault, doc_path = split_uri(args["uri"], expected_type="doc")
    doc_path = to_nfc(doc_path)
    await check_vault_access(uid, vault, required_role="writer")
    try:
        result = await doc_service.edit(
            vault, doc_path,
            old_string=args["old_string"],
            new_string=args["new_string"],
            replace_all=args.get("replace_all", False),
            message=args.get("message"),
            agent_id=user.username,
            base_commit=args.get("base_commit"),
        )
        return result.model_dump()
    except EditError as e:
        return {
            "error": "edit_failed",
            "message": str(e),
            "hint": "Use akb_get to verify current content, then retry with adjusted old_string.",
        }
    except ConflictError as e:
        # base_commit OCC mismatch: a concurrent writer moved the doc
        # between the agent's read and edit submission.
        return {
            "error": "conflict",
            "message": str(e),
            "hint": "Document was modified since base_commit. Re-read with akb_get and retry.",
        }


@_h("akb_delete")
async def _handle_delete(args: dict, uid: str, user: _MCPUser) -> dict:
    vault, doc_path = split_uri(args["uri"], expected_type="doc")
    doc_path = to_nfc(doc_path)
    await check_vault_access(uid, vault, required_role="writer")
    success = await doc_service.delete(vault, doc_path, agent_id=user.username)
    return {"deleted": success}


@_h("akb_browse")
async def _handle_browse(args: dict, uid: str, user: _MCPUser) -> dict:
    # `summary` is dropped from items by default — it's the largest
    # field on `BrowseItem` and dominates payload size on
    # collection-heavy vaults. Opt in with `include_summary=true`.
    #
    # Browse target may be specified two ways: legacy (`vault` +
    # optional `collection`) or canonical (`uri` — vault root or
    # `akb://V/coll/X`). If `uri` is given it wins and the legacy
    # params are ignored, so a caller can paste an item's `uri`
    # straight back in without re-parsing.
    from app.services.uri_service import split_browse_uri
    uri_arg = args.get("uri")
    if uri_arg:
        try:
            vault, collection = split_browse_uri(uri_arg)
        except ValueError as exc:
            return {"error": str(exc)}
    else:
        vault = args.get("vault")
        if not vault:
            return {"error": "Either `vault` or `uri` is required for akb_browse."}
        collection = args.get("collection")
    await check_vault_access(uid, vault, required_role="reader")
    result = await doc_service.browse(
        vault,
        collection=collection,
        depth=args.get("depth", 1),
        content_type=args.get("content_type", "all"),
    )
    include_summary = args.get("include_summary")
    payload = result.model_dump(
        exclude={"items": {"__all__": {"summary"}}} if not include_summary else None
    )

    needle = _filter_arg(args)
    if needle:
        payload["items"] = [
            it for it in payload.get("items") or []
            if needle in (it.get("name") or "").lower()
            or needle in (it.get("path") or "").lower()
        ]
    return _paginate(payload, args, items_key="items")


@_h("akb_search")
async def _handle_search(args: dict, uid: str, user: _MCPUser) -> dict:
    if args.get("vault"):
        await check_vault_access(uid, args["vault"], required_role="reader")
    result = await search_service.search(
        query=args["query"],
        vault=args.get("vault"),
        collection=args.get("collection"),
        doc_type=args.get("type"),
        tags=args.get("tags"),
        limit=args.get("limit", 10),
        user_id=uid,
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
        count_only=args.get("count_only", False),
        files_with_matches=args.get("files_with_matches", False),
    )
    return result


@_h("akb_drill_down")
async def _handle_drill_down(args: dict, uid: str, user: _MCPUser) -> dict:
    # Two modes:
    #   - "sections" (default): return body content for matched sections,
    #     optionally narrowed by `pattern` (substring grep on body).
    #   - "outline":            return heading paths only (no bodies).
    #     Use this to discover what sections exist in a long document
    #     before deciding which `section` to drill into.
    # On empty `sections` result, the response also includes an
    # `outline` so the agent has something to retry against.
    OUTLINE_CAP = 50
    vault, doc_path = split_uri(args["uri"], expected_type="doc")
    await check_vault_access(uid, vault, required_role="reader")
    mode = args.get("mode") or "sections"

    if mode == "outline":
        # Fetch one more than the cap so we can tell whether the
        # outline was truncated without paying for the full count.
        # When not truncated, `len(headings)` IS the total; when
        # truncated, total is omitted (we don't know the real value
        # without scanning the whole doc).
        headings = await search_service.list_section_headings(
            vault, doc_path, limit=OUTLINE_CAP + 1
        )
        truncated = len(headings) > OUTLINE_CAP
        outline = headings[:OUTLINE_CAP]
        response: dict = {
            "uri": args["uri"],
            "outline": outline,
            "returned": len(outline),
        }
        if truncated:
            response["truncated"] = True
            response["hint"] = (
                f"More than {OUTLINE_CAP} headings exist — outline is capped. "
                f"Call again with `section` set to a known prefix to narrow."
            )
        else:
            response["total"] = len(headings)
        return response

    section = args.get("section")
    pattern = (args.get("pattern") or "").strip().lower()
    sections = await search_service.drill_down(vault, doc_path, section=section)
    if pattern:
        sections = [s for s in sections if pattern in (s.get("content") or "").lower()]

    response: dict = {
        "uri": args["uri"],
        "sections": sections,
        "returned": len(sections),
    }
    if not sections:
        try:
            headings = await search_service.list_section_headings(
                vault, doc_path, limit=OUTLINE_CAP + 1
            )
        except Exception:
            headings = []
        response["outline"] = headings[:OUTLINE_CAP]
        response["truncated"] = len(headings) > OUTLINE_CAP
        response["hint"] = (
            "No section matched. Retry with one of the headings in `outline`, "
            "or call again with `mode='outline'` for the heading list only, "
            "or call `akb_get(uri=...)` to read the whole document."
        )
        return response

    # Successful match — surface where the agent can drill next.
    # `sub_sections` are the immediate children of the matched heading
    # that actually exist in this document (deduped across chunks).
    # `siblings_hint` points at a sibling lookup pattern when there
    # are no children — gives the LLM something useful to try without
    # re-fetching the full outline.
    section_paths = [s.get("section_path") or "" for s in sections]
    sub_sections = _sub_sections_of(section_paths, section)
    if sub_sections:
        response["sub_sections"] = sub_sections[:OUTLINE_CAP]
        response["hint"] = (
            "This section has children — drill further with "
            f"`section='{sub_sections[0]}'`. "
            "For all headings call again with `mode='outline'`."
        )
    elif section:
        response["hint"] = (
            "No sub-sections under this heading. To pick a sibling "
            "or another section, call again with `mode='outline'`."
        )
    else:
        # `section` was not provided — caller got the whole doc back
        # as section chunks. Quiet hint about the partial-read pattern.
        response["hint"] = (
            "Returned every section. Use `section='Heading'` or "
            "`pattern='text'` to narrow next time."
        )
    return response


def _sub_sections_of(section_paths: list[str], parent: str | None) -> list[str]:
    """Return the immediate children of ``parent`` that appear in
    ``section_paths``. The ``section`` argument to ``drill_down`` is
    an ILIKE substring (``section_path ILIKE '%' || $section || '%'``),
    not an exact match — so the returned chunks may also include the
    parent heading itself plus deeper grandchildren. We collapse
    grandchildren onto their immediate-child segment so the agent sees
    one nav level at a time.

    When ``parent`` is ``None`` (no section filter) the helper returns
    an empty list — the caller has the full outline already and a
    "sub-section" concept isn't meaningful.
    """
    if not parent:
        return []
    prefix = parent.rstrip("/") + "/"
    children: set[str] = set()
    for path in section_paths:
        if not path or not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if not rest:
            continue
        first = rest.split("/", 1)[0]
        if first:
            children.add(prefix + first)
    return sorted(children)


@_h("akb_activity")
async def _handle_activity(args: dict, uid: str, user: _MCPUser) -> dict:
    await check_vault_access(uid, args["vault"], required_role="reader")
    from app.services.git_service import GitService
    git = GitService()
    limit = args.get("limit", 20)
    # Peek one past the limit so we can flag truncation without a
    # separate `git rev-list --count` walk (which would re-traverse the
    # whole vault log just to answer "is there more?"). The trade-off:
    # `truncated` reflects what git would have produced *before* the
    # post-fetch author filter below — i.e. when truncated=True the
    # caller knows commits exist past the window, but cannot tell
    # without paging whether they would survive the author filter.
    entries = git.vault_log(
        args["vault"],
        max_count=limit + 1,
        since=args.get("since"),
        path=args.get("collection"),  # Git-native path filter (like git log -- <path>)
    )
    truncated = len(entries) > limit
    if truncated:
        entries = entries[:limit]
    # Filter by author (post-filter, Git doesn't support Korean author filter well)
    author = args.get("author")
    if author:
        entries = [e for e in entries if author.lower() in e.get("agent", "").lower() or author.lower() in e.get("author", "").lower()]
    return {
        "vault": args["vault"],
        "activity": entries,
        "returned": len(entries),
        "truncated": truncated,
    }


@_h("akb_diff")
async def _handle_diff(args: dict, uid: str, user: _MCPUser) -> dict:
    vault, doc_path = split_uri(args["uri"], expected_type="doc")
    await check_vault_access(uid, vault, required_role="reader")
    commit = args.get("commit", "")
    if not _HEX_COMMIT_RE.fullmatch(commit):
        return {
            "error": (
                "commit must be a 7-64 char lowercase hex hash; "
                "symbolic refs are not accepted"
            )
        }
    doc = await _find_doc(vault, doc_path)
    if not doc:
        return {"error": f"Document not found: {args['uri']}"}
    from app.services.git_service import GitService
    git = GitService()
    return git.file_diff(vault, doc["path"], commit)


@_h("akb_relations")
async def _handle_relations(args: dict, uid: str, user: _MCPUser) -> dict:
    uri = args["uri"]
    parsed = parse_uri(uri)
    if parsed is None:
        return {"error": f"Invalid AKB URI: '{uri}'"}
    vault = parsed.vault
    access = await check_vault_access(uid, vault, required_role="reader")
    relations = await get_resource_relations(
        vault, uri,
        vault_id=access["vault_id"],
        direction=args.get("direction", "both"),
        relation_type=args.get("type"),
    )
    return {"uri": uri, "relations": relations}


@_h("akb_graph")
async def _handle_graph(args: dict, uid: str, user: _MCPUser) -> dict:
    vault: str
    uri = args.get("uri")
    if uri:
        parsed = parse_uri(uri)
        if parsed is None:
            return {"error": f"Invalid AKB URI: '{uri}'"}
        vault = parsed.vault
    else:
        v = args.get("vault")
        if not v:
            return {"error": "Either `uri` or `vault` is required"}
        vault = v
    access = await check_vault_access(uid, vault, required_role="reader")
    return await get_graph(
        vault,
        resource_uri=uri,
        # 0.3.0 renamed the param to `hops` so it doesn't collide with
        # `akb_browse.depth` (collection-tree depth). Accept the new
        # name only — the old `depth` is not aliased here because that
        # would let half-migrated callers silently use the wrong word.
        hops=args.get("hops", 2),
        limit=args.get("limit", 50),
        vault_id=access["vault_id"],
    )


@_h("akb_link")
async def _handle_link(args: dict, uid: str, user: _MCPUser) -> dict:
    # URIs carry their own vault. Reject cross-vault links so each link
    # stays inside one access boundary.
    src_parsed = parse_uri(args["source"])
    tgt_parsed = parse_uri(args["target"])
    if src_parsed is None or tgt_parsed is None:
        return {"error": "Both source and target must be valid akb:// URIs"}
    if src_parsed.vault != tgt_parsed.vault:
        return {"error": "source and target must belong to the same vault"}
    vault = src_parsed.vault
    await check_vault_access(uid, vault, required_role="writer")
    return await link_resources(
        vault,
        args["source"], args["target"], args["relation"],
        created_by=user.username,
    )


@_h("akb_unlink")
async def _handle_unlink(args: dict, uid: str, user: _MCPUser) -> dict:
    src_parsed = parse_uri(args["source"])
    tgt_parsed = parse_uri(args["target"])
    if src_parsed is None or tgt_parsed is None:
        return {"error": "Both source and target must be valid akb:// URIs"}
    if src_parsed.vault != tgt_parsed.vault:
        return {"error": "source and target must belong to the same vault"}
    vault = src_parsed.vault
    access = await check_vault_access(uid, vault, required_role="writer")
    return await unlink_resources(
        args["source"], args["target"],
        relation_type=args.get("relation"),
        vault_id=access["vault_id"],
    )


@_h("akb_provenance")
async def _handle_provenance(args: dict, uid: str, user: _MCPUser) -> dict:
    vault, doc_path = split_uri(args["uri"], expected_type="doc")
    await check_vault_access(uid, vault, required_role="reader")
    doc = await _find_doc(vault, doc_path)
    if not doc:
        return {"error": "Document not found"}
    return await get_provenance(str(doc["id"]), vault_id=doc["vault_id"])


@_h("akb_create_table")
async def _handle_create_table(args: dict, uid: str, user: _MCPUser) -> dict:
    try:
        vault, collection = _resolve_parent(args, kind_name="table")
    except ValueError as e:
        return {"error": str(e)}
    access = await check_vault_access(uid, vault, required_role="writer")
    try:
        return await table_service.create_table(
            access["vault_id"], args["name"], args["columns"],
            actor_id=user.username,
            description=args.get("description", ""),
            collection=collection or None,
        )
    except ValueError as e:
        return {"error": str(e)}


@_h("akb_sql")
async def _handle_sql(args: dict, uid: str, user: _MCPUser) -> dict:
    sql = args["sql"].strip()
    vaults = args.get("vaults") or ([args["vault"]] if args.get("vault") else [])
    if not vaults:
        return {"error": "Must specify vault or vaults parameter"}

    # Check access on all referenced vaults — minimum reader. This is
    # the application's friendly 403 gate; if the caller has no
    # membership at all on a referenced vault, fail fast here rather
    # than letting PG return permission-denied. Per-statement read/
    # write enforcement is handled by PG ACL via the caller's role
    # memberships in akb_vault_<vid>_{reader,writer,admin}.
    for v in vaults:
        await check_vault_access(uid, v, required_role="reader")

    return await table_service.execute_sql(
        vault_names=vaults,
        user_id=uid,
        sql=sql,
        is_admin=user.is_admin,
    )


@_h("akb_drop_table")
async def _handle_drop_table(args: dict, uid: str, user: _MCPUser) -> dict:
    vault, table_name = split_uri(args["uri"], expected_type="table")
    access = await check_vault_access(uid, vault, required_role="admin")
    try:
        return await table_service.drop_table(
            access["vault_id"], table_name, actor_id=user.username,
        )
    except NotFoundError as e:
        return {"error": str(e)}


@_h("akb_alter_table")
async def _handle_alter_table(args: dict, uid: str, user: _MCPUser) -> dict:
    vault, table_name = split_uri(args["uri"], expected_type="table")
    access = await check_vault_access(uid, vault, required_role="admin")
    try:
        return await table_service.alter_table(
            access["vault_id"], table_name,
            actor_id=user.username,
            add_columns=args.get("add_columns"),
            drop_columns=args.get("drop_columns"),
            rename_columns=args.get("rename_columns"),
        )
    except NotFoundError as e:
        return {"error": str(e)}


@_h("akb_remember")
async def _handle_remember(args: dict, uid: str, user: _MCPUser) -> dict:
    return await remember(uid, args["content"], args.get("category", "general"))


@_h("akb_recall")
async def _handle_recall(args: dict, uid: str, user: _MCPUser) -> dict:
    # `recall` now returns the full envelope: {memories, returned,
    # total, truncated}. Pass it through unchanged — `total` is the
    # corpus count (was len(returned) pre-0.3.0, which lied when the
    # LIMIT cut things off).
    return await recall(uid, args.get("category"), args.get("limit", 20))


@_h("akb_forget")
async def _handle_forget(args: dict, uid: str, user: _MCPUser) -> dict:
    success = await forget(uid, args["memory_id"])
    return {"forgotten": success}


_SYSTEM_UID = "00000000-0000-0000-0000-000000000000"


@_h("akb_publish")
async def _handle_publish(args: dict, uid: str, user: _MCPUser) -> dict:
    """Publish a document, file, or table query."""
    resource_type = args.get("resource_type", "document")
    uri = args.get("uri")

    # Resolve vault + identifier from URI for doc/file, or from `vault`
    # arg for table_query (a SQL surface, not a single resource).
    doc_path: str | None = None
    file_id: str | None = None
    vault_name: str | None = None
    if resource_type == "document":
        if not uri:
            return {"error": "`uri` is required for resource_type=document"}
        vault_name, doc_path = split_uri(uri, expected_type="doc")
    elif resource_type == "file":
        if not uri:
            return {"error": "`uri` is required for resource_type=file"}
        vault_name, file_id = split_uri(uri, expected_type="file")
    elif resource_type == "table_query":
        vault_name = args.get("vault")
        if not vault_name:
            return {"error": "`vault` is required for resource_type=table_query"}
    else:
        return {"error": f"Unknown resource_type: {resource_type}"}

    await check_vault_access(uid, vault_name, required_role="writer")
    created_by = uuid.UUID(uid) if uid and uid != _SYSTEM_UID else None

    try:
        result = await publication_service.create_publication_for_vault(
            vault_name=vault_name,
            resource_type=resource_type,
            doc_id=doc_path,
            file_id=file_id,
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
        "resource_type": resource_type,
        "expires_at": result.get("expires_at"),
        "password_protected": result.get("password_protected", False),
    }


@_h("akb_unpublish")
async def _handle_unpublish(args: dict, uid: str, user: _MCPUser) -> dict:
    """Delete a publication by slug, or all publications for a given resource URI."""
    if args.get("slug"):
        # Slug-based delete: vault scoped via the publication row itself,
        # so we resolve the owning vault from the publication.
        slug = args["slug"]
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT v.name AS vault_name
                  FROM publications p JOIN vaults v ON v.id = p.vault_id
                 WHERE p.slug = $1
                """,
                slug,
            )
        if row:
            await check_vault_access(uid, row["vault_name"], required_role="writer")
        deleted = await publication_service.delete_publication(slug=slug)
        return {"published": False, "deleted": deleted}

    if args.get("uri"):
        vault, _doc_path = split_uri(args["uri"], expected_type="doc")
        await check_vault_access(uid, vault, required_role="writer")
        # Pass the URI directly — delete_publications_for_document
        # accepts canonical akb:// strings.
        count = await publication_service.delete_publications_for_document(args["uri"])
        return {"published": False, "deleted_publications": count}

    return {"error": "Either slug or uri is required"}


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
    slug = args["slug"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM publications WHERE slug = $1", slug,
        )
    if not row:
        return {"error": f"Publication not found: {slug}"}
    try:
        return await publication_service.create_snapshot(row["id"])
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


@_h("akb_create_collection")
async def _handle_create_collection(args: dict, uid: str, user: _MCPUser) -> dict:
    from app.services.collection_service import (
        CollectionService, InvalidPathError,
    )
    await check_vault_access(uid, args["vault"], required_role="writer")
    svc = CollectionService()
    try:
        return await svc.create(
            vault=args["vault"], path=args["path"],
            summary=args.get("summary"),
            agent_id=uid,
        )
    except InvalidPathError as exc:
        return {"error": "invalid_path", "message": str(exc)}


@_h("akb_delete_collection")
async def _handle_delete_collection(args: dict, uid: str, user: _MCPUser) -> dict:
    from app.services.collection_service import (
        CollectionService, CollectionNotEmptyError, InvalidPathError,
    )
    await check_vault_access(uid, args["vault"], required_role="writer")
    svc = CollectionService()
    try:
        return await svc.delete(
            vault=args["vault"], path=args["path"],
            recursive=bool(args.get("recursive", False)),
            agent_id=uid,
        )
    except InvalidPathError as exc:
        return {"error": "invalid_path", "message": str(exc)}
    except CollectionNotEmptyError as exc:
        return {
            "error": "not_empty",
            "message": str(exc),
            "doc_count": exc.doc_count,
            "file_count": exc.file_count,
            "sub_collection_count": exc.sub_collection_count,
        }


@_h("akb_history")
async def _handle_history(args: dict, uid: str, user: _MCPUser) -> dict:
    vault, doc_path = split_uri(args["uri"], expected_type="doc")
    doc_path = to_nfc(doc_path)
    await check_vault_access(uid, vault, required_role="reader")
    doc = await _find_doc(vault, doc_path)
    if not doc:
        return {"error": f"Document not found: {args['uri']}"}
    from app.services.git_service import GitService
    git = GitService()
    # Pass the doc's created_at as a lineage boundary so commits from a
    # previous document at the same path (deleted-and-recreated) don't
    # leak into this doc's history. created_at lives on the documents
    # row; convert to Unix seconds for git's filter.
    since_epoch = None
    created_at = doc.get("created_at")
    if created_at is not None:
        since_epoch = int(created_at.timestamp())
    history = git.file_log(
        vault, doc["path"],
        max_count=args.get("limit", 20),
        since_epoch=since_epoch,
    )
    return {"uri": doc_uri(vault, doc["path"]), "history": history}


@_h("akb_set_public")
async def _handle_set_public(args: dict, uid: str, user: _MCPUser) -> dict:
    """Set `vaults.public_access`. Owner-only.

    `level` is preferred ({"none","reader","writer"}); the legacy
    `is_public` boolean is mapped to {"none","reader"} for back-compat.
    Business logic + PG-RBAC plumbing live in
    `access_service.set_public_access` — this handler is a thin
    adapter."""
    from app.services.access_service import set_public_access

    level = args.get("level")
    if level is None:
        # Legacy boolean: True → reader, False → none.
        level = "reader" if args.get("is_public", True) else "none"

    return await set_public_access(uid, args["vault"], level)


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
