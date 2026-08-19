"""One-shot, idempotent reservation backfill (spec: 2026-08-19 design item).

Service-layer only — doc moves must keep git + PG + aliases + indexes
consistent, so this NEVER touches SQL for mutations. Raw SQL is used only
to SCAN for violations. Mirror (external-git) vaults are excluded entirely.

Violation classes → treatment:
  move_out      ordinary doc under overview/  → move() to path minus prefix
                (resource_aliases keep old URIs resolving)
  retype        skill-typed doc elsewhere     → update(type="note")
  restore_type  canonical doc retyped         → update(type="skill")
  reseed        vault missing the canonical   → put(seed, skill_internal=True)

Prefix-stripping target (the spec's open implementation question): a
collection-less, vault-ROOT path IS valid — `normalize_collection_path("")`
returns "" (== vault root) and `doc_path("", slug)` yields a bare
``{slug}.md``, a shape both backends create and browse today. So
``overview/x.md`` lands at ``x.md`` and no fallback collection is needed;
only ``overview/a/b.md`` keeps a collection (``a``).

Per-item errors are collected, not fatal; rerunning after a partial failure
is safe (every treatment is a no-op once its violation is gone):

  move_out      the row's path no longer matches `path LIKE 'overview/%'`,
                so the scan does not return it again at all.
  retype        `doc_type` is now 'note', so neither scan predicate matches.
  restore_type  the canonical row is still scanned (its path is under
                overview/) but `classify_violation` returns None for it.
  reseed        the missing-canonical scan's NOT EXISTS no longer holds, so
                the vault drops out; and even against a STALE plan the seed
                pins slug `vault-skill`, and a pinned slug never
                collision-suffixes — put() raises ConflictError rather than
                creating a duplicate.

A doc that is BOTH under overview/ and skill-typed classifies as move_out
(the path rule wins); the move alone would leave a stray skill type behind,
so the move-out treatment retypes it to 'note' at its new path in the same
pass.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.services import skill_policy

logger = logging.getLogger("akb.skill_backfill")

_ACTOR = "akb-skill-backfill"

_CLASSES = ("move_out", "retype", "restore_type", "reseed")

# Scan only. Every mutation below goes through DocumentService.
_SCAN_VIOLATIONS_SQL = """
SELECT d.id, d.path, d.doc_type, v.name AS vault_name
  FROM documents d
  JOIN vaults v ON v.id = d.vault_id
 WHERE v.id NOT IN (SELECT vault_id FROM vault_external_git)
   AND (d.path LIKE 'overview/%' OR d.doc_type = 'skill')
 ORDER BY v.name, d.path
"""

_SCAN_MISSING_SQL = """
SELECT v.name AS vault_name
  FROM vaults v
 WHERE v.id NOT IN (SELECT vault_id FROM vault_external_git)
   AND NOT EXISTS (
       SELECT 1 FROM documents d
        WHERE d.vault_id = v.id AND d.path = $1
   )
 ORDER BY v.name
"""


def classify_violation(row: dict) -> str | None:
    path, doc_type = row["path"], row["doc_type"]
    if path == skill_policy.VAULT_SKILL_PATH:
        return None if doc_type == skill_policy.SKILL_DOC_TYPE else "restore_type"
    if skill_policy.is_reserved_path(path):
        return "move_out"
    if doc_type == skill_policy.SKILL_DOC_TYPE:
        return "retype"
    return None


def _move_target(path: str) -> tuple[str | None, str]:
    """overview/a/b.md → ('a', 'b'); overview/x.md → (None, 'x')."""
    rest = path[len(skill_policy.SKILL_COLLECTION) + 1:]
    rest = rest.removesuffix(".md")
    if "/" in rest:
        coll, slug = rest.rsplit("/", 1)
        return coll, slug
    return None, rest


async def _scan() -> tuple[list[dict], list[str]]:
    """Read-only violation scan. Returns (violation rows, vaults missing seed)."""
    from app.db.postgres import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SCAN_VIOLATIONS_SQL)
        missing = await conn.fetch(
            _SCAN_MISSING_SQL, skill_policy.VAULT_SKILL_PATH
        )
    return [dict(r) for r in rows], [r["vault_name"] for r in missing]


def _retype_request(new_type: str, what: str):
    from app.models.document import DocumentUpdateRequest

    return DocumentUpdateRequest(
        type=new_type, message=f"skill-reservation backfill: {what}"
    )


async def _apply_move_out(doc_service, row: dict) -> None:
    """Move a doc out of the reserved namespace, keeping its identity.

    An explicit slug makes `move` REJECT a collision instead of silently
    suffixing, so a taken target surfaces as ConflictError; retry ONCE under a
    disambiguated slug. A second collision is left to the caller's error list —
    the doc stays where it is and an operator resolves it by hand.
    """
    from app.exceptions import ConflictError

    vault, path = row["vault_name"], row["path"]
    coll, slug = _move_target(path)
    kwargs: dict[str, Any] = {
        "collection": coll or "",
        "agent_id": _ACTOR,
        "skill_internal": True,
        "message": "skill-reservation backfill: move out of reserved namespace",
    }
    try:
        res = await doc_service.move(vault, path, slug=slug, **kwargs)
    except ConflictError as e:
        if "already exists" not in str(e):
            raise
        res = await doc_service.move(
            vault, path, slug=f"{slug}-from-overview", **kwargs
        )
    # The path rule classified this row, but a skill-typed doc that merely
    # left overview/ is still a `retype` violation — close it in the same pass
    # so a clean run really means clean.
    if row["doc_type"] == skill_policy.SKILL_DOC_TYPE:
        await doc_service.update(
            vault, res.path, _retype_request("note", "retype"), agent_id=_ACTOR
        )


async def _apply_retype(doc_service, row: dict) -> None:
    await doc_service.update(
        row["vault_name"], row["path"],
        _retype_request("note", "retype"), agent_id=_ACTOR,
    )


async def _apply_restore_type(doc_service, row: dict) -> None:
    await doc_service.update(
        row["vault_name"], row["path"],
        _retype_request(skill_policy.SKILL_DOC_TYPE, "restore"), agent_id=_ACTOR,
    )


async def _apply_reseed(doc_service, vault: str) -> None:
    from app.services.document_service import build_vault_skill_seed_request

    await doc_service.put(
        build_vault_skill_seed_request(vault),
        agent_id=_ACTOR,
        skill_internal=True,
    )


async def run(doc_service, *, execute: bool = False) -> dict:
    """Scan, then (with `execute`) repair. Default is a counts-only dry run."""
    rows, missing = await _scan()

    plan: dict[str, list] = {name: [] for name in _CLASSES}
    plan["reseed"] = list(missing)
    for row in rows:
        violation = classify_violation(row)
        if violation:
            plan[violation].append(row)

    if not execute:
        return {"dry_run": True, **{name: len(plan[name]) for name in _CLASSES}}

    done = {name: 0 for name in _CLASSES}
    errors: list[dict] = []
    # `reseed` items are vault names, the rest are scan rows — so the handlers
    # are deliberately typed loosely rather than joined into one signature.
    handlers: dict[str, Callable[[Any, Any], Awaitable[None]]] = {
        "move_out": _apply_move_out,
        "retype": _apply_retype,
        "restore_type": _apply_restore_type,
        "reseed": _apply_reseed,
    }
    for name in _CLASSES:
        for item in plan[name]:
            target = item if name == "reseed" else f"{item['vault_name']}:{item['path']}"
            try:
                await handlers[name](doc_service, item)
                done[name] += 1
            except Exception as e:  # noqa: BLE001 — one bad doc must not abort the batch
                logger.error("%s failed for %s: %s", name, target, e)
                errors.append({"path": target, "error": str(e)})
    return {"dry_run": False, "done": done, "errors": errors}
