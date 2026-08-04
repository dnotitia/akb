"""One chokepoint for deleting a `documents` row — enforced statically.

Publications lost their `document_id` FK in migration 022, so nothing
cascades when a document row goes away. Resolution re-finds the document
**by path**, and `documents` is `UNIQUE(vault_id, path)` — so a publication
that outlives its document is reached by whatever document
next occupies that path. Of the three per-row delete paths, two omitted the
publication statement — the one that had it kept it as an inline copy, so
there was no single original for the others to call.

Convergence on a helper is a convention, and a convention is exactly what
failed here. The structural version is
`DocumentRepository.delete_with_publications`: the row delete and the
publication cascade are one call, so a new delete path can only be wrong by
not deleting documents at all. This file is the lock on that door — it
enumerates every `DELETE FROM documents` in the production tree and fails on
any site not on the allowlist, so a sixth one fails in review rather than in
production.

Comments are invisible to `ast`, and docstrings are filtered out, so only
statements the database would actually execute are counted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.source_scan import BACKEND as _BACKEND, enclosing_function, python_files


_ROOTS = ("app", "mcp_server")

# `public.documents`, `ONLY documents`, `"documents"` and TRUNCATE all remove
# rows just as well as the plain form, so the scan covers them rather than
# leaving four spellings of the same bypass.
_DELETE_DOCUMENTS = re.compile(
    r"""\b(?:
          DELETE \s+ FROM \s+ (?:ONLY \s+)? (?:public\.)? "?documents"?
        | TRUNCATE \s+ (?:TABLE \s+)? (?:ONLY \s+)? (?:public\.)? "?documents"?
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

# A table name spliced in at runtime hides the phrase above from a literal
# scan: `f"DELETE FROM {t}"`, `"DELETE FROM " + t`, `"DELETE FROM {}".format(t)`.
# Any literal that ENDS mid-statement, right where the table name belongs, is
# treated as a delete site — it may be innocent (some other table), but it is
# indistinguishable from the bypass and so has to be looked at.
# TRUNCATE only counts when an interpolation actually follows it — the bare
# word is also a GRANT privilege name (`role_sync._EXPECTED_TABLE_PRIVS`) and
# flagging that would be noise, not signal.
_DANGLING_DELETE = re.compile(
    r"""\b(?:
          DELETE \s+ FROM \s* (?:\{[^}]*\})? \s*
        | TRUNCATE (?:\s+TABLE)? \s* \{[^}]*\} \s*
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

# Interpolated-table deletes that provably never name `documents`. Same
# contract as `_ALLOWLIST`: the sentence is the review.
_DYNAMIC_TABLE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("app/db/migrations/026_uri_collection_prefix.py", "_run"): (
        "`DELETE FROM {tmp}` where `tmp` is the TEMP TABLE created by the "
        "CREATE TEMP TABLE two lines above, inside the same migration."
    ),
    ("app/services/table_row_write.py", "compile_delete_rows"): (
        "User-table row delete. The name comes from "
        "`table_data_repo.pg_table_name(vault, table)`, which only ever "
        "produces a `vt_*` dynamic table — never a core table."
    ),
    ("app/services/table_row_write.py", "_compile_delete_ast"): (
        "Same compiler, akb_sql's parsed-DELETE arm; same `vt_*`-only "
        "table-name source."
    ),
}


# Every production site allowed to delete `documents` rows, with the reason
# it is allowed and how many such statements it is allowed to contain. The
# count matters: keyed on (file, function) alone, an allowlist entry would
# silently pre-approve a SECOND delete added to an already-blessed function.
# Adding or growing an entry is a deliberate act with a reviewer attached.
_ALLOWLIST: dict[tuple[str, str], tuple[int, str]] = {
    ("app/repositories/document_repo.py", "delete_with_publications"): (1, (
        "THE chokepoint. Takes FOR UPDATE on the row, runs the publication "
        "cascade, then deletes — one call, one transaction. Every ordinary "
        "document delete routes through here."
    )),
    ("app/services/document_service.py", "_rollback_vault_rows"): (1, (
        "Vault lifecycle, not an ordinary delete: purges a half-created "
        "vault, `DELETE FROM documents WHERE vault_id = $1` in bulk, and "
        "drops the `vaults` row in the same transaction — so "
        "`publications.vault_id ... ON DELETE CASCADE` (init.sql) removes "
        "the publications, and no later document can occupy the reused "
        "(vault_id, path) because the vault itself is gone. It also aborts "
        "outright if any publication exists (the foreign-write guard), so "
        "the branch that reaches the DELETE has none to clean. Routing it "
        "through the per-row chokepoint would be N round-trips to redo work "
        "one cascade already did."
    )),
    ("app/services/access_service.py", "delete_vault"): (1, (
        "Vault lifecycle, same reasoning: the `vaults` row goes in the same "
        "transaction and `publications.vault_id ... ON DELETE CASCADE` takes "
        "the publications with it. The path cannot be reoccupied — there is no "
        "vault left to hold it."
    )),
}


def _python_files() -> list[Path]:
    return python_files(_ROOTS)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of the Constant nodes that are docstrings, so prose describing a
    DELETE is not mistaken for one."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out




def _delete_sites() -> dict[tuple[str, str], list[int]]:
    """(relative path, enclosing function) → line numbers of executable
    document-delete string literals."""
    sites: dict[tuple[str, str], list[int]] = {}
    for path in _python_files():
        source = path.read_text()
        if not _DELETE_DOCUMENTS.search(source):
            continue  # cheap reject before paying for a parse
        tree = ast.parse(source)
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings or not _DELETE_DOCUMENTS.search(node.value):
                continue
            key = (path.relative_to(_BACKEND).as_posix(), enclosing_function(tree, node.lineno))
            sites.setdefault(key, []).append(node.lineno)
    return sites


def _dangling_delete_sites() -> list[tuple[str, str, int, str]]:
    """Literals that stop right where a table name would be interpolated.

    `f"DELETE FROM {table}"` renders as a JoinedStr whose literal part is
    `"DELETE FROM "` — the table never appears in any single constant, so the
    scan above cannot see it. Same for `+` concatenation and `.format()`.
    Rather than try to evaluate the expression, flag the shape.
    """
    out: list[tuple[str, str, int, str]] = []
    for path in _python_files():
        source = path.read_text()
        if not re.search(r"DELETE\s+FROM|TRUNCATE", source, re.IGNORECASE):
            continue
        tree = ast.parse(source)
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            if _DANGLING_DELETE.search(node.value.rstrip()):
                out.append((
                    path.relative_to(_BACKEND).as_posix(),
                    enclosing_function(tree, node.lineno),
                    node.lineno,
                    node.value.strip()[-60:],
                ))
    return out


def test_every_document_delete_is_allowlisted():
    """A new document delete anywhere in the production tree fails here. If
    the new site is legitimate, add it to `_ALLOWLIST` *with the reason its
    publications are handled* — that sentence is the review."""
    found = _delete_sites()
    unlisted = {k: v for k, v in found.items() if k not in _ALLOWLIST}
    assert not unlisted, (
        "unallowlisted document delete:\n"
        + "\n".join(f"  {f}:{lines} in {fn}()" for (f, fn), lines in sorted(unlisted.items()))
        + "\n\nDocument deletes must go through "
        "`DocumentRepository.delete_with_publications`, which carries the "
        "publication cascade. If this site genuinely cannot, add it to "
        "_ALLOWLIST in this file with the reason its publications are handled."
    )

    # An extra delete inside an already-blessed function would otherwise ride
    # in for free on the entry that blessed the first one.
    grew = {
        k: (len(v), _ALLOWLIST[k][0]) for k, v in found.items()
        if k in _ALLOWLIST and len(v) != _ALLOWLIST[k][0]
    }
    assert not grew, (
        "allowlisted function's delete count changed (found, allowed): "
        f"{grew}. Each statement needs its own justification — update the "
        "count in _ALLOWLIST only after reading the new one."
    )


def test_no_document_delete_hides_behind_an_interpolated_table_name():
    """`f"DELETE FROM {table}"` renders the table name at runtime, so a
    literal scan cannot see whether it says `documents`. Any literal that
    stops exactly where the table belongs is flagged — an innocent one is
    fine to allowlist, but it has to be read first."""
    dangling = [
        d for d in _dangling_delete_sites()
        if (d[0], d[1]) not in _DYNAMIC_TABLE_ALLOWLIST
    ]
    assert not dangling, (
        "delete statement with an interpolated table name — the static "
        "chokepoint gate cannot see what it deletes:\n"
        + "\n".join(f"  {f}:{ln} in {fn}(): ...{frag!r}" for f, fn, ln, frag in dangling)
    )


def test_allowlist_has_no_stale_entries():
    """The allowlist is not allowed to grow stale either: an entry whose
    site was deleted or renamed must be removed, or it silently pre-approves
    a site nobody has read."""
    found = _delete_sites()
    stale = sorted(set(_ALLOWLIST) - set(found))
    assert not stale, f"_ALLOWLIST entries with no matching site: {stale}"


def test_the_chokepoint_is_the_only_repository_delete():
    """`DocumentRepository` must expose no plainer row-delete method. The
    allowlist above stops a new inline SQL delete; this stops the other way
    in — calling a repository method that skips the cascade."""
    from app.repositories.document_repo import DocumentRepository

    assert hasattr(DocumentRepository, "delete_with_publications")
    assert not hasattr(DocumentRepository, "delete"), (
        "DocumentRepository.delete deletes the row without the publication "
        "cascade — that is the hole. Use delete_with_publications."
    )


def _chokepoint_body() -> str:
    source = (_BACKEND / "app/repositories/document_repo.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "delete_with_publications":
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # drop the docstring — it describes the SQL too
            return "\n".join(ast.unparse(stmt) for stmt in body)
    pytest.fail("delete_with_publications not found in document_repo.py")


def test_chokepoint_locks_before_it_cleans_up():
    """Ordering is the whole fix for the publish/delete TOCTOU.
    `create_publication` takes FOR SHARE on the documents row and then
    inserts; the deleter's own serialization is a pg_advisory_xact_lock over
    (vault_id, path) that the publisher never acquires, so the two lock
    namespaces do not serialize. With the cleanup running before any
    conflicting row lock, a publisher slips in between: the cleanup finds
    nothing, the publisher commits its INSERT, and the row delete — which had
    been blocked on that share lock — proceeds over a surviving publication.

    FOR UPDATE first, then the cleanup, then the row delete. The live proof
    is `tests/concurrency/test_invariants_unit.py`
    ::test_publish_delete_race_leaves_no_orphan_publication; this pins the
    ordering statically so a refactor cannot quietly invert it.
    """
    body = _chokepoint_body()
    lock = body.find("FOR UPDATE")
    # Either publication-cleanup entry point counts. The chokepoint uses the
    # by-URI form (it builds the URI from the row it just locked, so there is
    # no caller-supplied string to validate, and validating would reject a
    # legitimate brace path) — but matching both keeps this assertion about
    # the ORDER rather than about which helper name is in fashion.
    cleanup = min(
        (i for i in (
            body.find("delete_publications_by_doc_uri("),
            body.find("delete_publications_for_document("),
        ) if i != -1),
        default=-1,
    )
    row_delete = body.find("DELETE FROM documents")
    assert lock != -1, "the chokepoint must take FOR UPDATE on the resource row"
    assert cleanup != -1, "the chokepoint must run the publication cascade"
    assert row_delete != -1, "the chokepoint must delete the row"
    assert lock < cleanup < row_delete, (
        "order must be FOR UPDATE → publication cleanup → DELETE FROM documents "
        f"(got lock@{lock}, cleanup@{cleanup}, delete@{row_delete})"
    )
