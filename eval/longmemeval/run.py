#!/usr/bin/env python3
"""LongMemEval benchmark runner for AKB — single-file, stdlib-only CLI.

See README.md (next to this file) for the full plan, design decisions,
and known risks.

Quick start (after `docker compose up -d` in this directory):

    python eval/longmemeval/run.py \\
        --dataset ~/datasets/longmemeval/longmemeval_s.json \\
        --ndjson eval/reports/longmemeval-akb.ndjson \\
        --limit 5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_AKB_URL = "http://localhost:18000"
DEFAULT_ADAPTER = "akb-hybrid"
DEFAULT_TOP_K = 5
DEFAULT_MAX_INDEX_WAIT = 300
POLL_INTERVAL_S = 1.0


# ── HTTP ──────────────────────────────────────────────────────────────────

class HTTPError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} on {url}: {body[:200]}")
        self.status = status
        self.body = body
        self.url = url


def http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req_headers = {"Accept": "application/json"}
    if body is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise HTTPError(e.code, body_text, url) from e


# ── AKB client ────────────────────────────────────────────────────────────

class AKBClient:
    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def register(self, username: str, password: str) -> None:
        http_request(
            "POST",
            f"{self.base_url}/api/v1/auth/register",
            body={
                "username": username,
                "password": password,
                "email": f"{username}@example.invalid",
            },
        )

    def login(self, username: str, password: str) -> str:
        r = http_request(
            "POST",
            f"{self.base_url}/api/v1/auth/login",
            body={"username": username, "password": password},
        )
        self.token = r["token"]
        return self.token

    def delete_account(self) -> dict:
        return http_request(
            "DELETE", f"{self.base_url}/api/v1/my/account", headers=self._auth()
        )

    def create_vault(self, name: str, description: str = "lme") -> dict:
        qs = urllib.parse.urlencode(
            {"name": name, "description": description, "public_access": "none"}
        )
        return http_request(
            "POST", f"{self.base_url}/api/v1/vaults?{qs}", headers=self._auth()
        )

    def delete_vault(self, name: str) -> None:
        try:
            http_request(
                "DELETE", f"{self.base_url}/api/v1/vaults/{name}", headers=self._auth()
            )
        except HTTPError as e:
            if e.status != 404:
                raise

    def put_document(
        self,
        vault: str,
        collection: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
    ) -> dict:
        return http_request(
            "POST",
            f"{self.base_url}/api/v1/documents",
            headers=self._auth(),
            body={
                "vault": vault,
                "collection": collection,
                "title": title,
                "content": content,
                "type": "note",
                "tags": tags or [],
            },
        )

    def health_vault(self, vault: str) -> dict:
        return http_request(
            "GET", f"{self.base_url}/health/vault/{vault}", headers=self._auth()
        )

    def search(self, q: str, vault: str, limit: int) -> dict:
        qs = urllib.parse.urlencode({"q": q, "vault": vault, "limit": limit})
        return http_request(
            "GET", f"{self.base_url}/api/v1/search?{qs}", headers=self._auth()
        )


# ── Dataset normalization ─────────────────────────────────────────────────

@dataclass
class Session:
    session_id: str
    rendered: str


@dataclass
class Question:
    question_id: str
    question: str
    question_type: str
    sessions: list[Session]
    answer_session_ids: list[str]
    is_abstention: bool


def render_session(session_date: str | None, turns: list[dict]) -> str:
    """Plain explicit format with session date header.

    LongMemEval's `temporal-reasoning` (133/500) and `knowledge-update`
    (78/500) question types depend on per-session timestamps -- 42% of
    the benchmark.  Without the date in the document body, those
    questions can't be answered from retrieval alone.
    """
    lines = []
    if session_date:
        lines.append(f"[Session date: {session_date}]")
    for t in turns:
        role = (t.get("role") or "?").upper()
        content = t.get("content") or ""
        lines.append(f"{role}: {content}")
    return "\n\n".join(lines)


def normalize_question(raw: dict) -> Question:
    # Each question has three parallel arrays: haystack_session_ids[i]
    # is the id for haystack_sessions[i] (turns), and haystack_dates[i]
    # is the per-session timestamp (string, formatted like
    # "2023/05/30 (Tue) 23:40").
    sids = raw.get("haystack_session_ids") or []
    bodies = raw.get("haystack_sessions") or []
    dates = raw.get("haystack_dates") or [None] * len(sids)
    if len(sids) != len(bodies):
        raise ValueError(
            f"question {raw.get('question_id')}: haystack length mismatch "
            f"{len(sids)} vs {len(bodies)}"
        )
    if len(dates) != len(sids):
        # Don't fail -- pad with None so render_session just omits the
        # date header for those sessions.
        dates = list(dates) + [None] * (len(sids) - len(dates))
    # Some LongMemEval questions list the same session id twice in
    # haystack_session_ids (15/500 in the _s split).  Posting the same
    # title twice into one vault returns 409 Conflict, so keep the
    # first occurrence and drop the rest -- preserving the implicit
    # ordering the dataset chose.
    sessions: list[Session] = []
    seen: set[str] = set()
    for sid, turns, date in zip(sids, bodies, dates):
        if sid in seen:
            continue
        seen.add(sid)
        sessions.append(Session(session_id=sid, rendered=render_session(date, turns)))
    qid = raw["question_id"]
    return Question(
        question_id=qid,
        question=raw["question"],
        question_type=raw["question_type"],
        sessions=sessions,
        answer_session_ids=list(raw.get("answer_session_ids") or []),
        is_abstention=qid.endswith("_abs"),
    )


def normalize_vault_name(qid: str) -> str:
    # Vault name regex: ^[a-z0-9][a-z0-9-]*$.  LongMemEval qids are 8-char
    # hex (already legal); only the `_abs` suffix needs translation.
    return qid.replace("_", "-").lower()


def session_id_from_path(path: str) -> str:
    # Backend _slugify lowercases the title, preserves underscores, and
    # appends `.md`.  Reverse: 'chat/sharegpt_xxx_0.md' → 'sharegpt_xxx_0'.
    return path.removeprefix("chat/").removesuffix(".md")


def lowered(ids: list[str]) -> set[str]:
    return {s.lower() for s in ids}


# ── Helpers ───────────────────────────────────────────────────────────────

def sha8(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:8]


def shard_for(qid: str, total_workers: int) -> int:
    if total_workers <= 1:
        return 0
    return hashlib.sha256(qid.encode()).digest()[0] % total_workers


def load_completed(ndjson_path: Path, adapter: str) -> set[str]:
    if not ndjson_path.exists():
        return set()
    completed: set[str] = set()
    with ndjson_path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "run_meta_start":
                continue
            if rec.get("adapter") == adapter and "question_id" in rec:
                completed.add(rec["question_id"])
    return completed


def append_ndjson(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


def wait_for_indexing(
    client: AKBClient, vault: str, max_seconds: int
) -> tuple[bool, int]:
    """Poll /health/vault until backfill.upsert.pending == 0.

    Returns (success, elapsed_ms).
    """
    start = time.monotonic()
    deadline = start + max_seconds
    while time.monotonic() < deadline:
        try:
            h = client.health_vault(vault)
            # Response shape: {"vault", "metadata_backfill", "vector_store": {"backfill": {"upsert": {pending, retrying, abandoned, indexed}}}}
            upsert = (
                ((h.get("vector_store") or {}).get("backfill") or {}).get("upsert")
                or {}
            )
            if int(upsert.get("pending", 0)) == 0:
                return True, int((time.monotonic() - start) * 1000)
        except HTTPError:
            pass
        time.sleep(POLL_INTERVAL_S)
    return False, int(max_seconds * 1000)


def collect_run_meta(adapter: str, akb_url: str) -> dict:
    repo_root = Path(__file__).resolve().parent.parent.parent
    try:
        backend_sha = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=repo_root,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        backend_sha = "unknown"

    app_yaml = Path(__file__).resolve().parent / "config" / "app.yaml"
    app_yaml_sha = (
        hashlib.sha256(app_yaml.read_bytes()).hexdigest()[:12]
        if app_yaml.exists()
        else "unknown"
    )

    # Lightweight YAML scrape (avoid a pyyaml dep).  Pulls keys we care
    # about for retrieval-side reproducibility.
    snapshot: dict[str, str] = {}
    if app_yaml.exists():
        for raw_line in app_yaml.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, _, v = line.partition(":")
            snapshot[k.strip()] = v.strip().strip('"').strip("'")

    interesting = (
        "embed_model embed_dimensions embed_base_url rerank_enabled "
        "rerank_model rerank_base_url rerank_prefetch rerank_fusion_k "
        "search_prefetch "
        "vector_store_driver vector_store_sparse_shape bm25_k1 bm25_b "
        "indexing_batch_size indexing_concurrency"
    ).split()
    cfg = {k: snapshot.get(k) for k in interesting}

    return {
        "type": "run_meta_start",
        "adapter": adapter,
        "akb_url": akb_url,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backend_sha": backend_sha,
        "app_yaml_sha": app_yaml_sha,
        "config": cfg,
    }


# ── Per-question pipeline ─────────────────────────────────────────────────

def run_question(
    client: AKBClient,
    q: Question,
    worker_id: int,
    top_k: int,
    max_index_wait: int,
    adapter: str,
) -> dict:
    base = {
        "adapter": adapter,
        "question_id": q.question_id,
        "question_type": q.question_type,
        "is_abstention": q.is_abstention,
        "num_haystack": len(q.sessions),
        "ground_truth": q.answer_session_ids,
    }
    vault = f"lme-{normalize_vault_name(q.question_id)}-{worker_id}"

    client.delete_vault(vault)  # 404 OK — clear stale state from a prior crash
    client.create_vault(vault)

    # ── ingest ──
    ingest_start = time.monotonic()
    try:
        for s in q.sessions:
            client.put_document(vault, "chat", s.session_id, s.rendered, tags=["lme"])
    except HTTPError as e:
        client.delete_vault(vault)
        return {
            **base,
            "status": "ingest_error",
            "error": str(e),
            "ingest_ms": int((time.monotonic() - ingest_start) * 1000),
            "index_wait_ms": None,
            "query_ms": None,
            "retrieved": None,
            "hit_at_k": None,
        }
    ingest_ms = int((time.monotonic() - ingest_start) * 1000)

    # ── indexing wait ──
    ok, wait_ms = wait_for_indexing(client, vault, max_index_wait)
    if not ok:
        client.delete_vault(vault)
        return {
            **base,
            "status": "index_wait_timeout",
            "ingest_ms": ingest_ms,
            "index_wait_ms": wait_ms,
            "query_ms": None,
            "retrieved": None,
            "hit_at_k": None,
        }

    # ── search ──
    query_start = time.monotonic()
    try:
        resp = client.search(q.question, vault, top_k)
    except HTTPError as e:
        client.delete_vault(vault)
        return {
            **base,
            "status": "search_error",
            "error": str(e),
            "ingest_ms": ingest_ms,
            "index_wait_ms": wait_ms,
            "query_ms": int((time.monotonic() - query_start) * 1000),
            "retrieved": None,
            "hit_at_k": None,
        }
    query_ms = int((time.monotonic() - query_start) * 1000)

    retrieved = [session_id_from_path(r["path"]) for r in resp.get("results", [])]
    hit = bool(lowered(retrieved).intersection(lowered(q.answer_session_ids)))

    client.delete_vault(vault)
    return {
        **base,
        "status": "ok",
        "ingest_ms": ingest_ms,
        "index_wait_ms": wait_ms,
        "query_ms": query_ms,
        "retrieved": retrieved,
        "hit_at_k": hit,
    }


# ── main ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", type=Path, default=os.environ.get("LONGMEMEVAL_PATH"))
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    p.add_argument("--adapter", default=DEFAULT_ADAPTER)
    p.add_argument("--ndjson", type=Path, required=True)
    p.add_argument("--max-wall-seconds", type=int, default=None)
    p.add_argument("--worker-id", type=int, default=0)
    p.add_argument("--total-workers", type=int, default=1)
    p.add_argument("--limit", type=int, default=None, help="cap to first N questions (after shard/stratify)")
    p.add_argument("--stratify", type=int, default=None, help="sample N per question_type (seed=0)")
    p.add_argument("--akb-url", default=os.environ.get("AKB_URL", DEFAULT_AKB_URL))
    p.add_argument("--max-index-wait-seconds", type=int, default=DEFAULT_MAX_INDEX_WAIT)
    args = p.parse_args()
    if not args.dataset:
        p.error("--dataset (or env LONGMEMEVAL_PATH) is required")
    if not args.dataset.exists():
        p.error(f"dataset not found: {args.dataset}")
    if args.worker_id >= args.total_workers:
        p.error(f"--worker-id must be < --total-workers ({args.worker_id} >= {args.total_workers})")
    return args


def main() -> int:
    args = parse_args()

    print(f"loading dataset: {args.dataset}", file=sys.stderr)
    raw = json.loads(args.dataset.read_text())
    questions = [normalize_question(q) for q in raw]
    print(f"  loaded {len(questions)} questions", file=sys.stderr)

    # stratify before shard so each worker gets a balanced slice
    if args.stratify:
        rng = random.Random(0)
        by_type: dict[str, list[Question]] = {}
        for q in questions:
            by_type.setdefault(q.question_type, []).append(q)
        sample: list[Question] = []
        for qs in by_type.values():
            rng.shuffle(qs)
            sample.extend(qs[: args.stratify])
        questions = sample
        print(f"  stratified: {len(questions)} questions across {len(by_type)} types", file=sys.stderr)

    # shard
    questions = [q for q in questions if shard_for(q.question_id, args.total_workers) == args.worker_id]

    # resume — drop already-completed
    completed = load_completed(args.ndjson, args.adapter)
    if completed:
        before = len(questions)
        questions = [q for q in questions if q.question_id not in completed]
        print(f"  resume: skipping {before - len(questions)} already-completed", file=sys.stderr)

    # limit applies last (testing convenience)
    if args.limit:
        questions = questions[: args.limit]

    print(
        f"worker_id={args.worker_id}/{args.total_workers} todo={len(questions)} "
        f"adapter={args.adapter} akb_url={args.akb_url}",
        file=sys.stderr,
    )
    if not questions:
        print("nothing to do", file=sys.stderr)
        return 0

    # auth — ephemeral user per run
    ts = int(time.time())
    username = f"lme-eval-{args.worker_id}-{ts}"
    password = secrets.token_urlsafe(24)
    client = AKBClient(args.akb_url)
    client.register(username, password)
    client.login(username, password)
    print(f"auth: {username}", file=sys.stderr)

    # cleanup hooks — SIGINT/SIGTERM call the same path as the finally block
    _cleaned = {"done": False}

    def _cleanup() -> None:
        if _cleaned["done"]:
            return
        _cleaned["done"] = True
        print("cleanup: DELETE /my/account", file=sys.stderr)
        try:
            r = client.delete_account()
            print(f"  deleted vaults: {r.get('vaults_deleted', [])}", file=sys.stderr)
        except Exception as e:
            print(f"  cleanup failed: {e}", file=sys.stderr)

    def _sigexit(*_):
        _cleanup()
        sys.exit(130)

    signal.signal(signal.SIGINT, _sigexit)
    signal.signal(signal.SIGTERM, _sigexit)

    # run_meta header — once per file
    if not args.ndjson.exists() or args.ndjson.stat().st_size == 0:
        append_ndjson(args.ndjson, collect_run_meta(args.adapter, args.akb_url))

    wall_start = time.monotonic()
    counts = {"ok": 0, "hit": 0, "ingest_error": 0, "index_wait_timeout": 0, "search_error": 0}

    try:
        for i, q in enumerate(questions, 1):
            if args.max_wall_seconds and (time.monotonic() - wall_start) > args.max_wall_seconds:
                print(f"wall budget exhausted ({args.max_wall_seconds}s)", file=sys.stderr)
                break
            rec = run_question(
                client,
                q,
                args.worker_id,
                args.top_k,
                args.max_index_wait_seconds,
                args.adapter,
            )
            append_ndjson(args.ndjson, rec)
            status = rec["status"]
            counts[status] = counts.get(status, 0) + 1
            if status == "ok" and rec.get("hit_at_k"):
                counts["hit"] += 1
            elapsed = int((time.monotonic() - wall_start))
            print(
                f"  [{i}/{len(questions)} t+{elapsed}s] {q.question_id} "
                f"{q.question_type:24s} {status:18s} "
                f"hit={rec.get('hit_at_k')!s:5s} "
                f"ingest={rec.get('ingest_ms')}ms wait={rec.get('index_wait_ms')}ms "
                f"query={rec.get('query_ms')}ms",
                file=sys.stderr,
            )
    finally:
        _cleanup()

    n_ok = counts["ok"]
    recall = (counts["hit"] / n_ok) if n_ok else 0.0
    print(
        f"\nsummary: ok={n_ok} hits={counts['hit']} R@{args.top_k}={recall:.1%} "
        f"errors=(ingest={counts.get('ingest_error', 0)} "
        f"wait={counts.get('index_wait_timeout', 0)} "
        f"search={counts.get('search_error', 0)})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
