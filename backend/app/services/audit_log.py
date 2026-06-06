"""Compliance-grade audit log — **producer only**.

AKB does not store, query, or retain audit data. It *emits* a structured,
append-only JSON-lines stream that the operator's SIEM scrapes (tail the
file) and that is handed off daily to a WORM object-storage bucket. The
operator's security org owns retention / query / correlation under its own
compliance regime. Rationale + the alternatives we rejected (in-tx outbox,
Kafka backbone, a separate audit DB, a vendor-side WORM tier) are recorded
in `backend/CHANGELOG.md` 0.8.1.

Capture model — **best-effort post-operation append.** Every MCP tool call
passes through `record_tool()` from the dispatch chokepoint *after* the
handler runs, so reads and writes are captured uniformly (the Kubernetes
audit-backend model: log at the API layer, not per-service). There is no
transactional outbox: an AKB domain write already spans PG + git + vector
store + S3 and is not globally atomic, so binding the audit line to the PG
transaction alone buys little while costing real machinery. `record()`
therefore **never raises into the caller** — audit must not break serving.

Guards that keep this "audit" and not merely "logs":
  - monotonic per-file ``seq`` + a SHA-256 ``h`` hash-chain → the SIEM can
    prove no line was dropped or altered in transit (`verify_chain`).
  - per-file manifest (line count + file digest + chain head) uploaded
    beside the data object.
  - a WORM bucket (operator enables Object Lock at bucket creation) gives
    per-day immutability once a file lands.

Local file lifecycle (this disk is a **handoff buffer**, not the record):
  * day 0   — today's file is appended to;
  * day ≥1  — a completed file is uploaded to the bucket (+ manifest);
  * day ≥N  — the local file is deleted (``audit_local_retention_days``),
              **but only after a confirmed upload** — a bucket outage
              accumulates files locally and logs, rather than losing audit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.services._backfill import BackfillRunner

logger = logging.getLogger("akb.audit")

# Schema version stamped on every line so downstream parsers can branch.
_SCHEMA_VERSION = 1

_FILE_PREFIX = "akb-audit-"
_FILE_SUFFIX = ".jsonl"
_UPLOADED_MARKER = ".uploaded-"   # sidecar: {dir}/.uploaded-{YYYY-MM-DD}
_GENESIS = "0" * 64

# Read-only tools. Used ONLY to decide whether to skip a line when
# `audit_log_reads` is off. Anything NOT listed is treated as a
# state-changing call and is ALWAYS recorded — we fail toward logging so a
# new write-tool can never silently escape the audit trail. (File tools
# `akb_get_file`/`akb_put_file` are proxy-only and never reach this
# backend dispatch, so they're intentionally absent.)
_READ_ONLY_TOOLS = frozenset({
    "akb_get", "akb_search", "akb_browse", "akb_drill_down", "akb_grep",
    "akb_graph", "akb_relations", "akb_history", "akb_diff", "akb_activity",
    "akb_provenance", "akb_list_vaults", "akb_vault_info", "akb_vault_members",
    "akb_whoami", "akb_help", "akb_search_users", "akb_publications",
    "akb_publication_snapshot",
})

# Best-effort target-ref extraction from tool args (no bodies — Metadata
# level). First match wins; value is truncated.
_TARGET_KEYS = ("id", "path", "doc", "document", "collection", "table",
                "file", "name", "query", "username", "vault")
_TARGET_MAX = 256

# ── In-process chain state (guarded by _lock) ────────────────────

_lock = threading.Lock()
_seq = 0
_prev = _GENESIS
_cur_date: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().date().isoformat()


def _log_dir() -> Path:
    return Path(settings.audit.log_dir)


def _file_for(day: str) -> Path:
    return _log_dir() / f"{_FILE_PREFIX}{day}{_FILE_SUFFIX}"


def _date_of(path: Path) -> str | None:
    name = path.name
    if name.startswith(_FILE_PREFIX) and name.endswith(_FILE_SUFFIX):
        return name[len(_FILE_PREFIX):-len(_FILE_SUFFIX)]
    return None


# ── Hash chain ───────────────────────────────────────────────────


def _canonical(core: dict) -> str:
    """Deterministic encoding of a line *without* its ``h`` field. The
    SIEM recomputes this exactly to verify the chain, so it must stay
    byte-stable: sorted keys, no spaces, unescaped UTF-8."""
    return json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chain(prev_h: str, core: dict) -> str:
    return hashlib.sha256((prev_h + _canonical(core)).encode("utf-8")).hexdigest()


def verify_chain(lines: list[str]) -> tuple[bool, int]:
    """Verify a hash-chained JSON-lines slice. Returns ``(ok, bad_seq)``
    where ``bad_seq`` is -1 when intact, otherwise the seq of the first
    line that fails (a tampered/dropped/re-ordered line). Operators and
    the SIEM use this to prove integrity of a handed-off file."""
    prev = _GENESIS
    for i, raw in enumerate(lines):
        raw = raw.strip()
        if not raw:
            continue
        obj = json.loads(raw)
        h = obj.get("h")
        core = {k: v for k, v in obj.items() if k != "h"}
        if h != _chain(prev, core):
            return False, int(obj.get("seq", i))
        prev = h
    return True, -1


# ── Seeding (continuity across process restarts) ─────────────────


def _reseed(day: str) -> None:
    """Re-establish ``_seq``/``_prev`` from the on-disk file for ``day`` so
    a restart continues the same per-file chain instead of forking it.
    Caller holds _lock."""
    global _seq, _prev, _cur_date
    _cur_date = day
    _seq = 0
    _prev = _GENESIS
    path = _file_for(day)
    if not path.exists():
        return
    try:
        last = None
        n = 0
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    n += 1
                    last = line
        _seq = n
        if last:
            _prev = json.loads(last).get("h", _GENESIS)
    except Exception as e:  # noqa: BLE001 — seeding is best-effort
        logger.warning("audit reseed of %s failed (%s); starting fresh chain", path, e)
        _seq = 0
        _prev = _GENESIS


def init() -> None:
    """Prepare the audit directory and seed the chain. Safe to call when
    disabled (no-op beyond a debug log)."""
    if not settings.audit.enabled:
        logger.info("audit disabled (audit_enabled=false)")
        return
    try:
        _log_dir().mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.error("audit log dir %s not writable: %s", _log_dir(), e)
        return
    with _lock:
        _reseed(_today())
    logger.info(
        "audit enabled: dir=%s reads=%s bucket=%s seq=%d",
        _log_dir(), settings.audit.log_reads, settings.audit.bucket or "(file-only)", _seq,
    )


# ── Record ───────────────────────────────────────────────────────


def record(
    *,
    action: str,
    actor: str | None = None,
    actor_id: str | None = None,
    vault: str | None = None,
    target: str | None = None,
    outcome: str = "ok",
    code: str | None = None,
    meta: dict | None = None,
) -> None:
    """Append one best-effort audit line. Never raises into the caller.

    ``action`` is the canonical verb (an MCP tool name like ``akb_put``,
    or ``auth.denied``). ``outcome`` is ``"ok"`` / ``"error"``. Keep
    ``meta`` tiny — this is Metadata level; bodies do not belong here.
    """
    if not settings.audit.enabled:
        return
    try:
        day = _today()
        line_json: str
        with _lock:
            global _seq, _prev, _cur_date
            if day != _cur_date:
                _reseed(day)  # rolls the file + chain at the UTC date boundary
            _seq += 1
            core = {
                "v": _SCHEMA_VERSION,
                "ts": _now().isoformat(),
                "seq": _seq,
                "action": action,
                "actor": actor,
                "actor_id": actor_id,
                "vault": vault,
                "target": target,
                "outcome": outcome,
                "code": code,
                "meta": meta or None,
            }
            h = _chain(_prev, core)
            _prev = h
            core["h"] = h
            line_json = json.dumps(core, ensure_ascii=False) + "\n"
            with _file_for(day).open("a", encoding="utf-8") as fh:
                fh.write(line_json)
    except Exception as e:  # noqa: BLE001 — audit must never break serving
        logger.error("audit record dropped (action=%s): %s", action, e)


def _target_of(args: dict) -> str | None:
    for k in _TARGET_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v:
            v = v if len(v) <= _TARGET_MAX else v[:_TARGET_MAX] + "…"
            return f"{k}={v}"
    return None


def record_tool(name: str, args: dict, user, result) -> None:
    """Audit one MCP tool call from the dispatch chokepoint. ``user`` is the
    resolved _MCPUser; ``result`` is the handler's return envelope (or the
    final error envelope) — outcome is derived from it.

    Schema note — this is deliberately NOT the `events` outbox schema
    (`events_repo.emit_event`). They sit at different altitudes: `events`
    records *domain verbs* on success only (`kind="document.put"`,
    canonical `resource_uri` built by `uri_service`) for operational Redis
    fanout; this audit stream records the *tool actually invoked* at the
    API surface (`action="akb_put"`), including reads and failures, for a
    compliance SIEM. A canonical `resource_uri` can't be formed reliably
    from raw dispatch args (e.g. `akb_search` has no resource), so we keep
    an honest, lossy `target` rather than fake a URI. The divergence is
    intentional; do not try to unify the two."""
    if not settings.audit.enabled:
        return
    # Skip reads only when the operator opted out of read logging; unknown
    # (i.e. state-changing) tools are always kept.
    if not settings.audit.log_reads and name in _READ_ONLY_TOOLS:
        return
    outcome, code = "ok", None
    if isinstance(result, dict) and (result.get("error") is not None or result.get("code")):
        outcome = "error"
        code = result.get("code")
    record(
        action=name,
        actor=getattr(user, "username", None),
        actor_id=getattr(user, "user_id", None),
        vault=(args.get("vault") if isinstance(args, dict) else None),
        target=(_target_of(args) if isinstance(args, dict) else None),
        outcome=outcome,
        code=code,
    )


# ── Uploader (daily handoff to the WORM bucket) ──────────────────


def _bucket_key(day: str, filename: str) -> str:
    return f"audit/{day}/{filename}"


def _manifest(path: Path, day: str) -> dict:
    raw = path.read_bytes()
    lines = [ln for ln in raw.decode("utf-8").splitlines() if ln.strip()]
    first_seq = json.loads(lines[0]).get("seq") if lines else None
    last = json.loads(lines[-1]) if lines else {}
    return {
        "date": day,
        "file": path.name,
        "count": len(lines),
        "first_seq": first_seq,
        "last_seq": last.get("seq"),
        "head_hash": last.get("h"),       # chain head — anchors the day
        "sha256": hashlib.sha256(raw).hexdigest(),
        "schema": _SCHEMA_VERSION,
    }


_s3 = None


def _s3_client():
    """Dedicated audit-storage client. Uses the ``audit.*`` credentials when
    set, otherwise falls back to the system S3 connection (see
    ``AuditSettings``). Built once and cached. We never need Delete on this
    client — only PutObject — so the bucket credential can be write-only.

    Note we don't reuse the `s3_adapter.put_bytes/get_bytes` primitives:
    those are bound to the single `settings.s3_bucket` on the shared
    file-store client, whereas audit must target a *different* bucket on a
    *credential-isolated* client. We share only the boto-config via
    `make_client` and issue `put_object` directly."""
    global _s3
    if _s3 is None:
        from app.services.adapters import s3_adapter  # boto3 imported lazily
        a = settings.audit
        _s3 = s3_adapter.make_client(
            a.endpoint_url or settings.s3_endpoint_url,
            a.access_key or settings.s3_access_key,
            a.secret_key or settings.s3_secret_key,
            a.region or settings.s3_region,
        )
    return _s3


def _upload(path: Path, day: str) -> None:
    """PUT the data file + manifest to the audit bucket. Raises on failure
    so the caller leaves the upload marker unset (→ retried next tick, file
    not deleted). Bucket is assumed pre-provisioned (Object Lock can only be
    set at creation), so we never auto-create it."""
    s3 = _s3_client()
    body = path.read_bytes()
    s3.put_object(
        Bucket=settings.audit.bucket,
        Key=_bucket_key(day, path.name),
        Body=body,
        ContentType="application/x-ndjson",
    )
    manifest = json.dumps(_manifest(path, day), separators=(",", ":")).encode("utf-8")
    s3.put_object(
        Bucket=settings.audit.bucket,
        Key=_bucket_key(day, path.name + ".manifest.json"),
        Body=manifest,
        ContentType="application/json",
    )


def _pending_files(today: str) -> list[tuple[Path, str]]:
    """Completed (date < today) audit files in the dir, with their date."""
    out: list[tuple[Path, str]] = []
    for p in sorted(_log_dir().glob(f"{_FILE_PREFIX}*{_FILE_SUFFIX}")):
        d = _date_of(p)
        if d and d < today:
            out.append((p, d))
    return out


def _days_old(day: str, today: str) -> int:
    a = datetime.fromisoformat(day).date()
    b = datetime.fromisoformat(today).date()
    return (b - a).days


async def _process_uploads() -> int:
    """Uploader tick (async wrapper for BackfillRunner, which `await`s its
    callback). The real work is blocking filesystem + boto3 I/O, so it runs
    in a worker thread to keep the event loop free."""
    if not settings.audit.enabled or not settings.audit.bucket:
        return 0
    return await asyncio.to_thread(_process_uploads_sync)


def _process_uploads_sync() -> int:
    """One uploader tick: upload completed files, then delete locals that
    are old enough AND confirmed uploaded. Returns work done (uploads +
    deletes) so the runner loops promptly while there's a backlog."""
    today = _today()
    done = 0
    for path, day in _pending_files(today):
        marker = _log_dir() / f"{_UPLOADED_MARKER}{day}"
        if not marker.exists():
            try:
                _upload(path, day)
                marker.write_text(_today(), encoding="utf-8")
                done += 1
                logger.info("audit: uploaded %s to bucket %s", path.name, settings.audit.bucket)
            except Exception as e:  # noqa: BLE001 — leave for retry, do NOT delete
                logger.warning("audit: upload of %s failed (kept locally): %s", path.name, e)
                continue
        # Delete only after a confirmed upload and past the local window.
        if marker.exists() and _days_old(day, today) >= settings.audit.local_retention_days:
            try:
                path.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
                done += 1
                logger.info("audit: pruned local %s (uploaded, >%dd old)",
                            path.name, settings.audit.local_retention_days)
            except Exception as e:  # noqa: BLE001
                logger.warning("audit: prune of %s failed: %s", path.name, e)
    return done


_runner: BackfillRunner | None = None


def start_uploader() -> None:
    # We borrow BackfillRunner for its asyncio loop lifecycle (idle cadence
    # + graceful stop) ONLY — unlike the other workers this drains a
    # filesystem dir, not a PG outbox with FOR UPDATE SKIP LOCKED, so there
    # is no per-row backoff. `idle_secs` is the upload cadence (default 1h),
    # which doubles as the retry interval: a bucket outage just re-attempts
    # next tick (the file is kept, never pruned, until an upload confirms).
    global _runner
    if not (settings.audit.enabled and settings.audit.bucket):
        return
    _runner = BackfillRunner(
        "audit_uploader", _process_uploads,
        idle_secs=max(60, settings.audit.upload_interval_secs),
    )
    _runner.start()


async def stop_uploader() -> None:
    global _runner
    if _runner is not None:
        await _runner.stop()
        _runner = None


# ── Stats (for /health) ──────────────────────────────────────────


def stats() -> dict:
    if not settings.audit.enabled:
        return {"enabled": False}
    try:
        today = _today()
        pending = [p.name for p, _ in _pending_files(today)
                   if not (_log_dir() / f"{_UPLOADED_MARKER}{_date_of(p)}").exists()]
        return {
            "enabled": True,
            "dir": str(_log_dir()),
            "today_file": _file_for(today).name,
            "seq": _seq,
            "reads_logged": settings.audit.log_reads,
            "bucket": settings.audit.bucket or None,
            "pending_upload": len(pending),
        }
    except Exception as e:  # noqa: BLE001
        return {"enabled": True, "error": str(e)}
