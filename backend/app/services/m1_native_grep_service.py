"""Head-pinned direct PostgreSQL grep candidate for M1 B-grep.

The service is deliberately internal and measurement-only.  It scans the
canonical PG BodyStore representation after applying vault ACL and containment
filters, then verifies every result against the exact Head bytes in Python.
Derived chunks, embeddings, and indexes are never result authority.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.exceptions import AKBError, ForbiddenError, ValidationError
from app.services.document_service import _parse_markdown
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_service import NativeRevisionService
from app.services.uri_service import doc_uri, file_uri


NATIVE_GREP_MAX_PATTERN_BYTES = 4096
NATIVE_GREP_MAX_REPLACEMENT_BYTES = 4096
NATIVE_GREP_MAX_SEARCH_BYTES = 128 * 1024 * 1024
NATIVE_GREP_REGEX_TIMEOUT_SECONDS = 5.0
_REGEX_PROCESS_STOP_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class HeadBody:
    namespace_id: uuid.UUID
    vault: str
    resource_id: uuid.UUID
    surface: str
    path: str
    revision_id: str
    digest: str
    byte_size: int
    canonical_bytes: bytes

    @property
    def text(self) -> str:
        return self.canonical_bytes.decode("utf-8", errors="strict")

    @property
    def search_text(self) -> str:
        if self.surface == "document":
            return _parse_markdown(self.text)[1]
        return self.text

    @property
    def title(self) -> str:
        if self.surface == "document":
            metadata, _ = _parse_markdown(self.text)
            return str(metadata.get("title") or self.path.rsplit("/", 1)[-1])
        return self.path.rsplit("/", 1)[-1]

    @property
    def uri(self) -> str:
        if self.surface == "document":
            return doc_uri(self.vault, self.path)
        collection = self.path.rsplit("/", 1)[0] if "/" in self.path else None
        return file_uri(self.vault, str(self.resource_id), collection=collection)


def _scan_bodies_sync(
    bodies: list[HeadBody],
    pattern: str,
    *,
    regex: bool,
    case_sensitive: bool,
) -> list[dict[str, Any]]:
    """Run exact Head matching outside the serving event loop."""
    matcher = M1NativeGrepService._matcher(
        pattern, regex=regex, case_sensitive=case_sensitive,
    )
    matched: list[dict[str, Any]] = []
    for body_index, body in enumerate(bodies):
        search_text = body.search_text
        lines = [
            {"line": number, "text": line.strip()}
            for number, line in enumerate(search_text.splitlines(), start=1)
            if matcher(line)
        ]
        if not lines:
            continue
        item: dict[str, Any] = {
            "uri": body.uri,
            "resource_type": body.surface,
            "vault": body.vault,
            "path": body.path,
            "title": body.title,
            "revision": body.revision_id,
            "content_hash": body.digest,
            "matches": lines,
            "_body_index": body_index,
        }
        matched.append(item)
    return matched


def _head_bodies_from_rows(rows) -> list[HeadBody]:
    bodies: list[HeadBody] = []
    for row in rows:
        canonical = M1PgBodyStore._verify_row(row)
        bodies.append(
            HeadBody(
                namespace_id=row["namespace_id"],
                vault=row["vault"],
                resource_id=row["resource_id"],
                surface=row["surface"],
                path=row["current_path"],
                revision_id=row["head_revision_id"],
                digest=row["digest"],
                byte_size=row["byte_size"],
                canonical_bytes=canonical,
            )
        )
    return bodies


def _replace_bodies_sync(
    bodies: list[HeadBody],
    pattern: str,
    replace: str,
    *,
    regex: bool,
    case_sensitive: bool,
) -> list[str]:
    replacements = []
    for body in bodies:
        search_text = body.search_text
        if regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            replacements.append(re.sub(pattern, replace, search_text, flags=flags))
        elif case_sensitive:
            replacements.append(search_text.replace(pattern, replace))
        else:
            replacements.append(
                re.sub(re.escape(pattern), replace, search_text, flags=re.IGNORECASE)
            )
    return replacements


def _regex_child(connection, operation, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """One-shot spawned worker; the parent kills it if Python ``re`` stalls."""
    try:
        result = operation(*args, **kwargs)
        connection.send(("ok", result))
    except ValidationError as exc:
        connection.send(("validation", exc.message))
    except re.error as exc:
        connection.send(("validation", f"Invalid regex replacement: {exc}"))
    except BaseException as exc:  # noqa: BLE001 — isolate worker failures from API process
        connection.send(("error", type(exc).__name__))
    finally:
        connection.close()


class _RegexScanTimedOut(RuntimeError):
    pass


def _terminate_regex_process(process) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(_REGEX_PROCESS_STOP_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_REGEX_PROCESS_STOP_SECONDS)


def _run_regex_bounded(operation, args: tuple[Any, ...], kwargs: dict[str, Any], timeout: float):
    """Run one regex operation in a disposable process with a hard wall-clock bound."""
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_regex_child,
        args=(sending, operation, args, kwargs),
        name="m1-native-regex",
        daemon=True,
    )
    deadline = time.monotonic() + timeout
    started = False
    try:
        process.start()
        started = True
        sending.close()
        if not receiving.poll(max(0.0, deadline - time.monotonic())):
            raise _RegexScanTimedOut
        try:
            outcome, payload = receiving.recv()
        except EOFError as exc:
            raise RuntimeError("native grep regex worker exited without a result") from exc
        if outcome == "validation":
            raise ValidationError(payload)
        if outcome != "ok":
            raise RuntimeError(f"native grep regex worker failed ({payload})")
        return payload
    finally:
        receiving.close()
        sending.close()
        if started:
            _terminate_regex_process(process)


class M1NativeGrepService:
    """Direct scan/verify executor over current native Head bodies."""

    def __init__(self, pool: asyncpg.Pool, *, body_store: M1PgBodyStore | None = None):
        self.pool = pool
        self.body_store = body_store or M1PgBodyStore(pool)

    async def _head_bodies(
        self,
        *,
        user_id: uuid.UUID,
        vaults: list[str] | None,
        collection: str | None,
        resource_id: uuid.UUID | None,
        surfaces: tuple[str, ...],
    ) -> list[HeadBody]:
        conditions = [
            "rs.lifecycle = 'live'",
            "rs.content_profile = 'text'",
            "pm.selected_placement = 'pg-bodystore-v1'",
            "(v.owner_id = $1 OR EXISTS ("
            "SELECT 1 FROM vault_access va WHERE va.vault_id = v.id AND va.user_id = $1"
            ") OR EXISTS (SELECT 1 FROM users u WHERE u.id = $1 AND u.is_admin = TRUE) "
            "OR v.public_access IN ('reader', 'writer'))",
        ]
        params: list[Any] = [user_id]
        params.append(list(surfaces))
        conditions.append(f"rs.surface = ANY(${len(params)})")
        if vaults:
            params.append(vaults)
            conditions.append(f"v.name = ANY(${len(params)})")
        if collection:
            prefix = collection.strip("/")
            escaped_prefix = (
                prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            params.extend([prefix, f"{escaped_prefix}/%"])
            conditions.append(
                f"(rs.current_path = ${len(params) - 1} OR "
                f"rs.current_path LIKE ${len(params)} ESCAPE '\\')"
            )
        if resource_id is not None:
            params.append(resource_id)
            conditions.append(f"rs.resource_id = ${len(params)}")

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT rs.namespace_id, v.name AS vault, rs.resource_id,
                       rs.surface, rs.current_path, rs.head_revision_id,
                       pm.digest, pm.byte_size, pm.encoding,
                       pm.selected_placement, pm.verification_profile,
                       p.canonical_bytes
                  FROM native_resources rs
                  JOIN vaults v ON v.id = rs.namespace_id
                  JOIN native_revisions nr
                    ON nr.resource_id = rs.resource_id
                   AND nr.revision_id = rs.head_revision_id
                  JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = nr.payload_manifest_id
                  JOIN m1_reference_payloads p
                    ON p.payload_id = pm.private_locator
                 WHERE {' AND '.join(conditions)}
                 ORDER BY v.name, rs.current_path, rs.resource_id
                """,
                *params,
            )

        return await asyncio.to_thread(_head_bodies_from_rows, rows)

    async def _require_write_access(
        self,
        *,
        user_id: uuid.UUID,
        namespace_ids: set[uuid.UUID],
    ) -> None:
        if not namespace_ids:
            return
        async with self.pool.acquire() as conn:
            writable_rows = await conn.fetch(
                """
                SELECT v.id
                 FROM vaults v
                 WHERE v.id = ANY($1::uuid[])
                   AND v.status <> 'archived'
                   AND (
                       v.owner_id = $2
                       OR EXISTS (
                           SELECT 1 FROM users u
                            WHERE u.id = $2 AND u.is_admin = TRUE
                       )
                       OR EXISTS (
                           SELECT 1 FROM vault_access va
                            WHERE va.vault_id = v.id
                              AND va.user_id = $2
                              AND va.role IN ('writer', 'admin', 'owner')
                       )
                       OR v.public_access = 'writer'
                   )
                """,
                list(namespace_ids),
                user_id,
            )
        writable_ids = {row["id"] for row in writable_rows}
        if writable_ids != namespace_ids:
            raise ForbiddenError("grep replace requires writer access to every matched vault")

    @staticmethod
    def _matcher(pattern: str, *, regex: bool, case_sensitive: bool):
        if regex:
            try:
                compiled = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
            except re.error as exc:
                raise ValidationError(f"Invalid regex pattern: {exc}") from exc
            return lambda line: compiled.search(line) is not None
        if case_sensitive:
            return lambda line: pattern in line
        folded = pattern.casefold()
        return lambda line: folded in line.casefold()

    @staticmethod
    def _selected_surfaces(*, include_text_files: bool) -> tuple[str, ...]:
        return ("document", "file") if include_text_files else ("document",)

    async def grep(
        self,
        pattern: str,
        *,
        user_id: uuid.UUID,
        vaults: list[str] | None = None,
        collection: str | None = None,
        resource_id: uuid.UUID | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        count_only: bool = False,
        files_with_matches: bool = False,
        limit: int = 20,
        replace: str | None = None,
        actor: str | None = None,
        include_text_files: bool = False,
    ) -> dict[str, Any]:
        if count_only and files_with_matches:
            raise ValidationError("count_only and files_with_matches are mutually exclusive")
        if replace is not None and (count_only or files_with_matches):
            raise ValidationError("replace is incompatible with count_only/files_with_matches")
        if replace is not None and not actor:
            raise ValidationError("actor is required for native grep replacement")
        if limit < 1 or limit > 1000:
            raise ValidationError("limit must be between 1 and 1000")
        if len(pattern.encode("utf-8")) > NATIVE_GREP_MAX_PATTERN_BYTES:
            raise ValidationError(
                f"grep pattern exceeds {NATIVE_GREP_MAX_PATTERN_BYTES} UTF-8 bytes"
            )
        if replace is not None and len(replace.encode("utf-8")) > NATIVE_GREP_MAX_REPLACEMENT_BYTES:
            raise ValidationError(
                f"grep replacement exceeds {NATIVE_GREP_MAX_REPLACEMENT_BYTES} UTF-8 bytes"
            )

        # Compilation is bounded by the pattern limit and preserves the old
        # fail-fast invalid-pattern response before the candidate query.
        if regex:
            self._matcher(pattern, regex=True, case_sensitive=case_sensitive)
        bodies = await self._head_bodies(
            user_id=user_id,
            vaults=vaults,
            collection=collection,
            resource_id=resource_id,
            surfaces=self._selected_surfaces(include_text_files=include_text_files),
        )
        searched_bytes = sum(body.byte_size for body in bodies)
        if searched_bytes > NATIVE_GREP_MAX_SEARCH_BYTES:
            raise ValidationError(
                f"native grep candidate bytes exceed {NATIVE_GREP_MAX_SEARCH_BYTES}"
            )
        if regex:
            try:
                matched = await asyncio.to_thread(
                    _run_regex_bounded,
                    _scan_bodies_sync,
                    (bodies, pattern),
                    {"regex": True, "case_sensitive": case_sensitive},
                    NATIVE_GREP_REGEX_TIMEOUT_SECONDS,
                )
            except _RegexScanTimedOut as exc:
                raise AKBError(
                    "native grep regex execution timed out",
                    status_code=408,
                ) from exc
        else:
            # Literal matching is linear but may still traverse the complete
            # bounded corpus, so keep it off the request event loop too.
            matched = await asyncio.to_thread(
                _scan_bodies_sync,
                bodies,
                pattern,
                regex=False,
                case_sensitive=case_sensitive,
            )
        for item in matched:
            item["_body"] = bodies[item.pop("_body_index")]

        total_matches = sum(len(item["matches"]) for item in matched)
        base = {
            "pattern": pattern,
            "regex": regex,
            "searched_resources": len(bodies),
            "searched_bytes": searched_bytes,
            "total_resources": len(matched),
            "total_matches": total_matches,
        }
        if count_only:
            return {
                **base,
                "by_resource": {item["uri"]: len(item["matches"]) for item in matched},
            }
        if files_with_matches:
            resources = [
                {k: item[k] for k in ("uri", "resource_type", "revision", "path")}
                for item in matched
            ]
            return {**base, "n_resources": len(resources), "resources": resources}

        selected = matched[:limit]
        replacements: list[dict[str, Any]] = []
        if replace is not None:
            await self._require_write_access(
                user_id=user_id,
                namespace_ids={item["_body"].namespace_id for item in selected},
            )
            native = NativeRevisionService(self.pool, payload_store=self.body_store)
            selected_bodies = [item["_body"] for item in selected]
            if regex:
                try:
                    replacement_texts = await asyncio.to_thread(
                        _run_regex_bounded,
                        _replace_bodies_sync,
                        (selected_bodies, pattern, replace),
                        {"regex": True, "case_sensitive": case_sensitive},
                        NATIVE_GREP_REGEX_TIMEOUT_SECONDS,
                    )
                except _RegexScanTimedOut as exc:
                    raise AKBError(
                        "native grep regex replacement timed out",
                        status_code=408,
                    ) from exc
            else:
                replacement_texts = await asyncio.to_thread(
                    _replace_bodies_sync,
                    selected_bodies,
                    pattern,
                    replace,
                    regex=False,
                    case_sensitive=case_sensitive,
                )
            for item, new_search_text in zip(selected, replacement_texts, strict=True):
                head_body: HeadBody = item["_body"]
                if new_search_text == head_body.search_text:
                    continue
                if head_body.surface == "document":
                    metadata, _ = _parse_markdown(head_body.text)
                    from app.services.document_service import _compose_markdown

                    new_text = _compose_markdown(metadata, new_search_text)
                else:
                    new_text = new_search_text
                result = await native.replace_text(
                    namespace_id=head_body.namespace_id,
                    surface=head_body.surface,
                    path=head_body.path,
                    payload=new_text,
                    actor=actor or "",
                    mutation_id=uuid.uuid4(),
                    expected_revision_id=head_body.revision_id,
                    expected_resource_id=head_body.resource_id,
                    message=f"grep replace: {pattern!r}",
                )
                replacements.append({"uri": head_body.uri, "revision": result.revision_id})

        clean = [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in selected
        ]
        response: dict[str, Any] = {
            **base,
            "returned_resources": len(clean),
            "returned_matches": sum(len(item["matches"]) for item in clean),
            "truncated": len(matched) > len(clean),
            "results": clean,
        }
        if replace is not None:
            response.update(
                {"replace": replace, "replaced_resources": len(replacements), "replacements": replacements}
            )
        return response

    @staticmethod
    def _public_response(*, pattern: str, regex: bool, native: dict[str, Any]) -> dict[str, Any]:
        """Translate internal resource-neutral facts to the frozen Document grep shape."""
        if "by_resource" in native:
            return {
                "pattern": pattern,
                "regex": regex,
                "total_matches": native["total_matches"],
                "total_docs": native["total_resources"],
                "by_doc": native["by_resource"],
            }
        if "resources" in native:
            files = [row["uri"] for row in native["resources"]]
            return {
                "pattern": pattern,
                "regex": regex,
                "n_files": len(files),
                "files": files,
            }
        clean = []
        for row in native.get("results", []):
            public = {
                "uri": row["uri"],
                "vault": row["vault"],
                "path": row["path"],
                "title": row["title"],
                "matches": [
                    {"section": None, "text": match["text"]}
                    for match in row["matches"]
                ],
            }
            if row.get("resource_type") == "file":
                public.update(
                    {
                        "resource_type": "file",
                        "revision": row["revision"],
                        "content_hash": row["content_hash"],
                    }
                )
            clean.append(public)
        result: dict[str, Any] = {
            "pattern": pattern,
            "regex": regex,
            "returned_docs": native.get("returned_resources", len(clean)),
            "returned_matches": native.get("returned_matches", 0),
            "total_docs": native.get("total_resources", 0),
            "total_matches": native.get("total_matches", 0),
            "truncated": native.get("truncated", False),
            "results": clean,
        }
        if "replace" in native:
            replacements_by_uri = {row["uri"]: row for row in native.get("replacements", [])}
            result.update(
                {
                    "replace": native["replace"],
                    "replaced_docs": native.get("replaced_resources", 0),
                    "replacements": [
                        {
                            "uri": row["uri"],
                            "path": row["path"],
                            "title": row["title"],
                            "commit": replacements_by_uri[row["uri"]]["revision"],
                        }
                        for row in native.get("results", [])
                        if row["uri"] in replacements_by_uri
                    ],
                }
            )
        return result

    async def grep_public(self, pattern: str, **kwargs) -> dict[str, Any]:
        native = await self.grep(pattern, **kwargs)
        return self._public_response(pattern=pattern, regex=bool(kwargs.get("regex")), native=native)
