"""Move bridged revision bodies out of the git working store.

A cutover leaves two kinds of legacy-selector mapping behind.  A ``native``
mapping names a native revision and is already served entirely from
PostgreSQL.  A ``bridge`` mapping names only a git commit, so reading that
revision means reading the git working volume — which is ReadWriteOnce, which
is why the serving tier is pinned to one node and one replica.

This copies those bodies into the payload store the cutover itself writes to,
one mapping at a time.  It deliberately does not touch the revision ledger:
``native_revisions`` is immutable by trigger and its history is a recursive
``parent_revision_id`` walk, so inserting bridged revisions there would mean
rewriting lineage.  Only the body moves; the mapping keeps its identity and
its ordinal.

Three properties make this safe to run against a live vault:

* **Incremental.**  Each mapping is its own transaction and each one is
  independent.  Stopping halfway leaves a database in which some bodies are
  read from PostgreSQL and the rest from git, which is exactly the state the
  read path already handles.
* **git stays the fallback.**  Nothing is deleted here.  A mapping that has
  not moved, or whose payload cannot be found, still reads from git.
* **Rollback is one statement.**  ``UPDATE ... SET body_digest = NULL``
  returns every affected revision to the git read path.

This does not by itself free the serving tier from the volume.  A bridged
revision's *metadata* — author, time, message, changed files — is still read
from git by history, activity and diff, and the mapping table has no column
for any of it.  That is a second migration; this one moves bodies.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict, dataclass, field

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import ValidationError
from app.repositories.native_revision_migration_repo import (
    LegacyMappingAlreadyMigratedError,
    NativeRevisionMigrationRepository,
)
from app.services.git_service import GitService
from app.services.m1_pg_body_store import M1PgBodyStore

logger = logging.getLogger("akb.bridge_body_backfill")

DEFAULT_BATCH_SIZE = 200


@dataclass
class BridgeBodyBackfillReport:
    """What one invocation did, in facts an operator can act on."""

    dry_run: bool
    vault: str | None = None
    examined: int = 0
    migrated: int = 0
    bytes_stored: int = 0
    # A mapping git cannot answer for. Left pending, never invented.
    unreadable: int = 0
    # A body the payload store refuses — over the 10 MiB cap, or not UTF-8.
    rejected: int = 0
    # Taken by a concurrent pass between the claim and the update.
    raced: int = 0
    pending_before: int = 0
    pending_after: int = 0
    migrated_total: int = 0
    # True when the run looked at mappings and moved none of them, so running
    # it again with the same arguments would do the same nothing.
    stalled: bool = False
    samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class _VaultNames:
    """namespace_id -> vault name, asked once per vault rather than per row.

    The cache is what keeps this from being a pool checkout per mapping: a
    run over one vault resolves exactly one name, and a run over all of them
    resolves one per vault.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._names: dict[uuid.UUID, str | None] = {}

    async def get(self, namespace_id: uuid.UUID) -> str | None:
        if namespace_id not in self._names:
            async with self._pool.acquire() as conn:
                self._names[namespace_id] = await conn.fetchval(
                    "SELECT name FROM vaults WHERE id = $1", namespace_id
                )
        return self._names[namespace_id]


async def backfill_bridge_bodies(
    *,
    vault: str | None = None,
    limit: int = 1000,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    git: GitService | None = None,
    pool: asyncpg.Pool | None = None,
) -> BridgeBodyBackfillReport:
    """Copy bridged bodies into the payload store, at most ``limit`` of them.

    ``limit`` bounds how many mappings are *examined*, not how many move — a
    body git cannot answer for still costs a look, and a run that counted
    only successes could not be given a bounded amount of work.

    ``vault`` narrows the run to one namespace, which is how the first live
    run is kept small enough to inspect by hand.  ``dry_run`` reads git and
    verifies the body would be accepted, then writes nothing — so the shape
    of a vault can be measured before anything moves.
    """
    if limit <= 0:
        raise ValidationError("--limit must be positive")
    if batch_size <= 0:
        raise ValidationError("--batch-size must be positive")

    pool = pool or await get_pool()
    repo = NativeRevisionMigrationRepository(pool)
    store = M1PgBodyStore(pool)
    legacy_git = git or GitService()
    names = _VaultNames(pool)
    report = BridgeBodyBackfillReport(dry_run=dry_run, vault=vault)

    namespace_id: uuid.UUID | None = None
    if vault is not None:
        async with pool.acquire() as conn:
            namespace_id = await conn.fetchval("SELECT id FROM vaults WHERE name = $1", vault)
        if namespace_id is None:
            raise ValidationError(f"Vault not found: {vault}")

    async with pool.acquire() as conn:
        report.pending_before, report.migrated_total = await repo.count_unmigrated_bridge_bodies(
            conn, namespace_id=namespace_id
        )

    # Page by keyset, not by re-asking for the head. A row this run cannot
    # move keeps `body_digest IS NULL`, so it stays first in the claim order
    # forever; without a cursor one unreadable body would be read again in
    # every batch, and a dry run — which moves nothing — would never advance
    # at all.
    cursor: tuple[uuid.UUID, uuid.UUID, int, str] | None = None

    while report.examined < limit:
        want = min(batch_size, limit - report.examined)
        claimed = await repo.claim_unmigrated_bridge_bodies(
            limit=want, namespace_id=namespace_id, after=cursor
        )
        if not claimed:
            break

        for mapping in claimed:
            report.examined += 1
            cursor = (
                mapping.namespace_id,
                mapping.resource_id,
                mapping.lineage_ordinal,
                mapping.legacy_git_oid,
            )
            vault_name = await names.get(mapping.namespace_id)
            if vault_name is None:
                report.unreadable += 1
                _sample(report, f"{mapping.namespace_id}: vault row is gone")
                continue

            try:
                body = await asyncio.to_thread(
                    legacy_git.read_file,
                    vault_name,
                    mapping.path_at_revision,
                    mapping.legacy_git_oid,
                )
            except (FileNotFoundError, OSError) as exc:
                report.unreadable += 1
                _sample(report, f"{vault_name}@{mapping.legacy_git_oid[:8]}: {exc}")
                continue
            if body is None:
                # git no longer carries that path at that commit. The revision
                # was already unreadable before this ran; moving on is the
                # only thing that does not invent a body.
                report.unreadable += 1
                _sample(
                    report,
                    f"{vault_name}:{mapping.path_at_revision}@{mapping.legacy_git_oid[:8]}: absent in git",
                )
                continue

            encoded = body.encode("utf-8")
            if dry_run:
                report.migrated += 1
                report.bytes_stored += len(encoded)
                continue

            try:
                async with pool.acquire() as conn, conn.transaction():
                    prepared = await store.prepare_text_in_conn(
                        conn, namespace_id=mapping.namespace_id, payload=encoded
                    )
                    await repo.attach_bridge_body_digest(
                        conn,
                        namespace_id=mapping.namespace_id,
                        resource_id=mapping.resource_id,
                        legacy_git_oid=mapping.legacy_git_oid,
                        digest=prepared.digest,
                    )
            except LegacyMappingAlreadyMigratedError:
                report.raced += 1
                continue
            except ValidationError as exc:
                # Over the body cap, or bytes the store will not hold. The
                # mapping keeps reading from git, which it could already do.
                report.rejected += 1
                _sample(report, f"{vault_name}@{mapping.legacy_git_oid[:8]}: {exc}")
                continue

            report.migrated += 1
            report.bytes_stored += prepared.byte_size

    # Nothing moved although there was work to do: every mapping this run
    # reached is one it cannot move, so running it again does the same
    # nothing. An operator has to look at `samples`, not reschedule.
    report.stalled = (
        not dry_run and report.examined > 0 and report.migrated == 0 and report.raced == 0
    )

    async with pool.acquire() as conn:
        report.pending_after, report.migrated_total = await repo.count_unmigrated_bridge_bodies(
            conn, namespace_id=namespace_id
        )
    logger.info(
        "bridge body backfill: examined=%d migrated=%d unreadable=%d rejected=%d pending=%d",
        report.examined,
        report.migrated,
        report.unreadable,
        report.rejected,
        report.pending_after,
    )
    return report


def _sample(report: BridgeBodyBackfillReport, line: str) -> None:
    """Keep the first few reasons, not all of them.

    An operator needs to know what kind of thing is being skipped; a report
    that grows one line per skipped row stops being readable exactly when the
    skips matter.
    """
    if len(report.samples) < 10:
        report.samples.append(line)
