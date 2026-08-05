"""Head-pinned direct PostgreSQL grep candidate for M1 B-grep.

The service is deliberately internal and measurement-only.  It scans the
canonical native payload representation after applying vault ACL and containment
filters, then verifies every result against the exact Head bytes in Python.
Derived chunks, embeddings, and indexes are never result authority.
"""

from __future__ import annotations

import asyncio
import io
import json
import multiprocessing
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg

from app.exceptions import AKBError, ForbiddenError, ValidationError
from app.services.document_service import _parse_markdown
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_payload_verification import (
    payload_store_for_placement,
    verify_native_head_body,
)
from app.services.native_revision_service import NativeRevisionService
from app.services.uri_service import doc_uri, file_uri


NATIVE_GREP_MAX_PATTERN_BYTES = 4096
NATIVE_GREP_MAX_REPLACEMENT_BYTES = 4096
NATIVE_GREP_MAX_SEARCH_BYTES = 128 * 1024 * 1024
NATIVE_GREP_MAX_RESOURCES = 10_000
NATIVE_GREP_REGEX_TIMEOUT_SECONDS = 5.0
NATIVE_GREP_MAX_REGEX_PROCESSES = 2
NATIVE_GREP_MAX_CHILD_RESULT_BYTES = 8 * 1024 * 1024
NATIVE_GREP_MAX_MATCHES_PER_RESOURCE = 1_000
NATIVE_GREP_MAX_TOTAL_MATCHES = 5_000
NATIVE_GREP_MAX_SNIPPET_BYTES = 4 * 1024
NATIVE_GREP_MAX_SNIPPET_BYTES_PER_RESOURCE = 256 * 1024
NATIVE_GREP_MAX_TOTAL_SNIPPET_BYTES = 1024 * 1024
_REGEX_PROCESS_STOP_SECONDS = 0.25
_REGEX_PROCESS_SLOTS = threading.BoundedSemaphore(NATIVE_GREP_MAX_REGEX_PROCESSES)


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
    selected_placement: str = M1PgBodyStore.selected_placement

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


@dataclass(frozen=True, slots=True)
class GrepScanLimits:
    matches_per_resource: int
    total_matches: int
    snippet_bytes: int
    snippet_bytes_per_resource: int
    total_snippet_bytes: int

    def public(self) -> dict[str, int]:
        return {
            "matches_per_resource": self.matches_per_resource,
            "total_matches": self.total_matches,
            "snippet_bytes": self.snippet_bytes,
            "snippet_bytes_per_resource": self.snippet_bytes_per_resource,
            "total_snippet_bytes": self.total_snippet_bytes,
        }


def _scan_limits() -> GrepScanLimits:
    # Capture limits in the parent so the spawned regex worker receives the
    # same bounded contract (including test/config monkeypatches) as literals.
    return GrepScanLimits(
        matches_per_resource=NATIVE_GREP_MAX_MATCHES_PER_RESOURCE,
        total_matches=NATIVE_GREP_MAX_TOTAL_MATCHES,
        snippet_bytes=NATIVE_GREP_MAX_SNIPPET_BYTES,
        snippet_bytes_per_resource=NATIVE_GREP_MAX_SNIPPET_BYTES_PER_RESOURCE,
        total_snippet_bytes=NATIVE_GREP_MAX_TOTAL_SNIPPET_BYTES,
    )


def _bounded_snippet(line: str, max_bytes: int) -> tuple[str, int, bool]:
    """Return stripped UTF-8 text without ever copying an unbounded snippet."""
    start = 0
    end = len(line)
    while start < end and line[start].isspace():
        start += 1
    while end > start and line[end - 1].isspace():
        end -= 1
    if max_bytes <= 0:
        return "", 0, end > start

    # A UTF-8 codepoint consumes at least one byte, so slicing at max_bytes
    # characters bounds the intermediate encoding to at most 4 * max_bytes.
    fragment = line[start:min(end, start + max_bytes)]
    encoded = fragment.encode("utf-8")
    if len(encoded) > max_bytes:
        encoded = encoded[:max_bytes]
        fragment = encoded.decode("utf-8", errors="ignore")
        encoded = fragment.encode("utf-8")
    return fragment, len(encoded), start + len(fragment) < end


def _scan_bodies_sync(
    bodies: list[HeadBody],
    pattern: str,
    *,
    regex: bool,
    case_sensitive: bool,
    mode: Literal["default", "count_only", "files_with_matches"] = "default",
    resource_limit: int = 20,
    limits: GrepScanLimits | None = None,
) -> dict[str, Any]:
    """Run exact Head matching with bounded response materialization."""
    limits = limits or _scan_limits()
    matcher = M1NativeGrepService._matcher(
        pattern, regex=regex, case_sensitive=case_sensitive,
    )
    items: list[dict[str, Any]] = []
    by_resource: dict[str, int] = {}
    resources: list[dict[str, Any]] = []
    total_resources = 0
    total_matches = 0
    materialized_matches = 0
    materialized_bytes = 0
    truncation_reasons: set[str] = set()
    for body_index, body in enumerate(bodies):
        search_text = body.search_text
        lines: list[dict[str, Any]] = []
        resource_matches = 0
        resource_bytes = 0
        return_resource = mode == "default" and total_resources < resource_limit
        for number, raw_line in enumerate(io.StringIO(search_text, newline=None), start=1):
            line = raw_line.removesuffix("\n").removesuffix("\r")
            if not matcher(line):
                continue
            resource_matches += 1
            if not return_resource:
                continue

            match_limited = False
            if len(lines) >= limits.matches_per_resource:
                truncation_reasons.add("per_resource_match_limit")
                match_limited = True
            if materialized_matches >= limits.total_matches:
                truncation_reasons.add("total_match_limit")
                match_limited = True
            if match_limited:
                continue

            remaining_resource_bytes = limits.snippet_bytes_per_resource - resource_bytes
            remaining_total_bytes = limits.total_snippet_bytes - materialized_bytes
            byte_limited = False
            if remaining_resource_bytes <= 0:
                truncation_reasons.add("per_resource_snippet_byte_limit")
                byte_limited = True
            if remaining_total_bytes <= 0:
                truncation_reasons.add("total_snippet_byte_limit")
                byte_limited = True
            if byte_limited:
                continue

            allowed_bytes = min(
                limits.snippet_bytes,
                remaining_resource_bytes,
                remaining_total_bytes,
            )
            snippet, snippet_bytes, snippet_truncated = _bounded_snippet(
                line, allowed_bytes,
            )
            if snippet_truncated:
                truncation_reasons.add("snippet_byte_limit")
                if allowed_bytes == remaining_resource_bytes:
                    truncation_reasons.add("per_resource_snippet_byte_limit")
                if allowed_bytes == remaining_total_bytes:
                    truncation_reasons.add("total_snippet_byte_limit")
            lines.append({"line": number, "text": snippet})
            resource_bytes += snippet_bytes
            materialized_bytes += snippet_bytes
            materialized_matches += 1

        if resource_matches == 0:
            continue
        total_resources += 1
        total_matches += resource_matches
        if mode == "count_only":
            by_resource[body.uri] = resource_matches
        elif mode == "files_with_matches":
            resources.append({
                "uri": body.uri,
                "resource_type": body.surface,
                "revision": body.revision_id,
                "path": body.path,
            })
        elif return_resource:
            items.append({
                "uri": body.uri,
                "resource_type": body.surface,
                "vault": body.vault,
                "path": body.path,
                "title": body.title,
                "revision": body.revision_id,
                "content_hash": body.digest,
                "matches": lines,
                "_body_index": body_index,
            })

    if mode == "default" and total_resources > len(items):
        truncation_reasons.add("resource_limit")
    return {
        "items": items,
        "by_resource": by_resource,
        "resources": resources,
        "total_resources": total_resources,
        "total_matches": total_matches,
        "materialized_matches": materialized_matches,
        "truncation_reasons": sorted(truncation_reasons),
        "limits": limits.public(),
    }


def _head_bodies_from_rows(rows) -> list[HeadBody]:
    bodies: list[HeadBody] = []
    for row in rows:
        canonical = verify_native_head_body(row)
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
                selected_placement=row["selected_placement"],
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


def _regex_child(
    connection,
    operation,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    max_result_bytes: int,
) -> None:
    """One-shot spawned worker; the parent kills it if Python ``re`` stalls."""
    def encode(outcome: str, payload: Any) -> bytes:
        return json.dumps(
            [outcome, payload],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")

    try:
        result = operation(*args, **kwargs)
        message = encode("ok", result)
        if len(message) > max_result_bytes:
            message = encode(
                "validation",
                "native grep regex result exceeds bounded worker output",
            )
        connection.send_bytes(message)
    except ValidationError as exc:
        connection.send_bytes(encode("validation", exc.message))
    except re.error as exc:
        connection.send_bytes(
            encode("validation", f"Invalid regex replacement: {exc}"),
        )
    except BaseException as exc:  # noqa: BLE001 — isolate worker failures from API process
        connection.send_bytes(encode("error", type(exc).__name__))
    finally:
        connection.close()


class _RegexScanTimedOut(RuntimeError):
    pass


class _RegexStartTimedOut(_RegexScanTimedOut):
    """Spawn exceeded the request deadline; its capacity slot is reaper-owned."""


def _terminate_regex_process(process) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(_REGEX_PROCESS_STOP_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_REGEX_PROCESS_STOP_SECONDS)


def _run_regex_process(
    operation,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    timeout: float,
    *,
    delayed_cleanup=None,
):
    """Run one regex operation after the caller acquires a process slot."""
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_regex_child,
        args=(sending, operation, args, kwargs, NATIVE_GREP_MAX_CHILD_RESULT_BYTES),
        name="m1-native-regex",
        daemon=True,
    )
    deadline = time.monotonic() + timeout
    started = False
    deferred_cleanup = False
    start_finished = threading.Event()
    start_errors: list[BaseException] = []
    received: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)

    def start() -> None:
        try:
            process.start()
        except BaseException as exc:  # noqa: BLE001 — handed back to bounded caller
            start_errors.append(exc)
        finally:
            start_finished.set()

    def receive() -> None:
        try:
            received.put(receiving.recv_bytes())
        except BaseException as exc:  # noqa: BLE001 — handed back to bounded caller
            received.put(exc)

    def cleanup_after_delayed_start() -> None:
        """Reap a process whose synchronous spawn outlived the request deadline."""
        start_finished.wait()
        try:
            if not start_errors:
                _terminate_regex_process(process)
        finally:
            receiving.close()
            sending.close()
            if delayed_cleanup is not None:
                delayed_cleanup()

    try:
        threading.Thread(target=start, name="m1-regex-start", daemon=True).start()
        if not start_finished.wait(timeout=max(0.0, deadline - time.monotonic())):
            deferred_cleanup = True
            threading.Thread(
                target=cleanup_after_delayed_start,
                name="m1-regex-start-reaper",
                daemon=True,
            ).start()
            raise _RegexStartTimedOut
        if start_errors:
            raise RuntimeError("native grep regex worker failed to start") from start_errors[0]
        started = True
        sending.close()
        threading.Thread(target=receive, name="m1-regex-recv", daemon=True).start()
        try:
            message = received.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            raise _RegexScanTimedOut
        if isinstance(message, BaseException):
            raise RuntimeError("native grep regex worker exited without a result") from message
        try:
            outcome, payload = json.loads(message.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("native grep regex worker returned an invalid envelope") from exc
        if outcome == "validation":
            raise ValidationError(payload)
        if outcome != "ok":
            raise RuntimeError(f"native grep regex worker failed ({payload})")
        return payload
    finally:
        if not deferred_cleanup:
            receiving.close()
            sending.close()
            if started:
                _terminate_regex_process(process)


def _run_regex_bounded(operation, args: tuple[Any, ...], kwargs: dict[str, Any], timeout: float):
    """Run one regex operation with process-concurrency and wall-clock bounds."""
    if not _REGEX_PROCESS_SLOTS.acquire(blocking=False):
        raise AKBError("native grep regex worker capacity exhausted", status_code=429)
    release_deferred = False
    try:
        return _run_regex_process(
            operation,
            args,
            kwargs,
            timeout,
            delayed_cleanup=_REGEX_PROCESS_SLOTS.release,
        )
    except _RegexStartTimedOut:
        # The delayed-start reaper owns the slot until it can terminate the
        # eventual process. Releasing here would exceed the process bound.
        release_deferred = True
        raise
    finally:
        if not release_deferred:
            _REGEX_PROCESS_SLOTS.release()


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

        head_joins = """
              FROM native_resources rs
              JOIN vaults v ON v.id = rs.namespace_id
              JOIN native_revisions nr
                ON nr.resource_id = rs.resource_id
               AND nr.revision_id = rs.head_revision_id
              JOIN native_payload_manifests pm
                ON pm.payload_manifest_id = nr.payload_manifest_id
        """
        where = " AND ".join(conditions)
        async with self.pool.acquire() as conn:
            # Pin the aggregate guard and body fetch to one snapshot. The first
            # query never joins/selects canonical BYTEA, so an oversized corpus
            # is rejected before asyncpg materializes attacker-controlled bodies.
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                aggregate = await conn.fetchrow(
                    f"""
                    SELECT count(*) AS resource_count,
                           COALESCE(sum(pm.byte_size), 0) AS total_bytes
                      {head_joins}
                     WHERE {where}
                    """,
                    *params,
                )
                resource_count = int(aggregate["resource_count"])
                total_bytes = int(aggregate["total_bytes"])
                if resource_count > NATIVE_GREP_MAX_RESOURCES:
                    raise ValidationError(
                        f"native grep candidate resources exceed {NATIVE_GREP_MAX_RESOURCES}"
                    )
                if total_bytes > NATIVE_GREP_MAX_SEARCH_BYTES:
                    raise ValidationError(
                        f"native grep candidate bytes exceed {NATIVE_GREP_MAX_SEARCH_BYTES}"
                    )
                rows = await conn.fetch(
                    f"""
                    SELECT rs.namespace_id, v.name AS vault, rs.resource_id,
                           rs.surface, rs.current_path, rs.head_revision_id,
                           pm.digest, pm.byte_size, pm.encoding,
                           pm.selected_placement, pm.verification_profile,
                           substring(
                               p.canonical_bytes FROM 1 FOR (pm.byte_size + 1)::integer
                           ) AS canonical_bytes
                      {head_joins}
                      JOIN m1_reference_payloads p
                        ON p.payload_id = pm.private_locator
                     WHERE {where}
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
        if pattern == "":
            raise ValidationError("grep pattern must not be empty")
        if count_only and files_with_matches:
            raise ValidationError("count_only and files_with_matches are mutually exclusive")
        if replace is not None and (count_only or files_with_matches):
            raise ValidationError("replace is incompatible with count_only/files_with_matches")
        if replace is not None and include_text_files:
            raise ValidationError("native grep replace does not support File resources")
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
        mode: Literal["default", "count_only", "files_with_matches"] = "default"
        if count_only:
            mode = "count_only"
        elif files_with_matches:
            mode = "files_with_matches"
        scan_limits = _scan_limits()
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
                scanned = await asyncio.to_thread(
                    _run_regex_bounded,
                    _scan_bodies_sync,
                    (bodies, pattern),
                    {
                        "regex": True,
                        "case_sensitive": case_sensitive,
                        "mode": mode,
                        "resource_limit": limit,
                        "limits": scan_limits,
                    },
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
            scanned = await asyncio.to_thread(
                _scan_bodies_sync,
                bodies,
                pattern,
                regex=False,
                case_sensitive=case_sensitive,
                mode=mode,
                resource_limit=limit,
                limits=scan_limits,
            )
        matched = scanned["items"]
        for item in matched:
            item["_body"] = bodies[item.pop("_body_index")]

        base = {
            "pattern": pattern,
            "regex": regex,
            "searched_resources": len(bodies),
            "searched_bytes": searched_bytes,
            "total_resources": scanned["total_resources"],
            "total_matches": scanned["total_matches"],
        }
        if count_only:
            return {
                **base,
                "by_resource": scanned["by_resource"],
            }
        if files_with_matches:
            resources = scanned["resources"]
            return {**base, "n_resources": len(resources), "resources": resources}

        selected = matched
        replacements: list[dict[str, Any]] = []
        if replace is not None:
            selected_bodies = [item["_body"] for item in selected]
            # Validate every matched Head before starting the first mutation:
            # mixed placement is permitted across the scan, but an unknown
            # placement must not leave an earlier replacement committed.
            payload_stores = [
                payload_store_for_placement(
                    self.pool,
                    body.selected_placement,
                    pg_body_store=self.body_store,
                )
                for body in selected_bodies
            ]
            await self._require_write_access(
                user_id=user_id,
                namespace_ids={body.namespace_id for body in selected_bodies},
            )
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
            for item, new_search_text, payload_store in zip(
                selected,
                replacement_texts,
                payload_stores,
                strict=True,
            ):
                head_body: HeadBody = item["_body"]
                if new_search_text == head_body.search_text:
                    continue
                if head_body.surface == "document":
                    metadata, _ = _parse_markdown(head_body.text)
                    from app.services.document_service import _compose_markdown

                    new_text = _compose_markdown(metadata, new_search_text)
                else:
                    new_text = new_search_text
                result = await NativeRevisionService(
                    self.pool,
                    payload_store=payload_store,
                ).replace_text(
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
            "returned_matches": scanned["materialized_matches"],
            "truncated": bool(scanned["truncation_reasons"]),
            "results": clean,
        }
        if scanned["truncation_reasons"]:
            response["truncation"] = {
                "reasons": scanned["truncation_reasons"],
                "limits": {"resources": limit, **scanned["limits"]},
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
        if "truncation" in native:
            result["truncation"] = native["truncation"]
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
