"""Attach Claude Code's prompt context to an existing stdio MCP command."""

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from uuid import UUID

META = "com.dnotitia.akb/turn"


def call_key(call_id):
    if isinstance(call_id, str) and 0 < len(call_id) <= 256:
        return hashlib.sha256(call_id.encode("utf-8")).hexdigest()
    return None


def connect():
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "akb-claude-turns"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    return sqlite3.connect(cache / "calls.sqlite3", timeout=1)


def record(event):
    if event.get("hook_event_name") != "PreToolUse":
        return
    key = call_key(event.get("tool_use_id"))
    if not key or not event.get("prompt_id") or not event.get("session_id"):
        return
    thread, turn = (str(UUID(event[field])) for field in ("session_id", "prompt_id"))
    now = int(time.time())
    with closing(connect()) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS calls (key TEXT PRIMARY KEY, thread TEXT, turn TEXT, expires INTEGER)")
        db.execute("DELETE FROM calls WHERE expires <= ?", (now,))
        # Ambiguous IDs must not join another prompt, even if two hooks race.
        db.execute("""
            INSERT INTO calls VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET thread=NULL, turn=NULL
            WHERE thread IS NOT excluded.thread OR turn IS NOT excluded.turn
        """, (key, thread, turn, now + 600))


def enrich(line):
    if b"claudecode/toolUseId" not in line:
        return line
    message = json.loads(line)
    if message.get("method") != "tools/call":
        return line
    meta = message["params"]["_meta"]
    key = call_key(meta.get("claudecode/toolUseId"))
    if not key or META in meta or "traceparent" in meta:
        return line
    with closing(connect()) as db:
        row = db.execute("SELECT thread, turn FROM calls WHERE key=? AND expires>? AND thread IS NOT NULL",
                         (key, int(time.time()))).fetchone()
    if row:
        meta[META] = {"thread_id": row[0], "turn_id": row[1]}
        return json.dumps(message, separators=(",", ":")).encode() + b"\n"
    return line


def forward(child):
    # Child stdout/stderr go directly to the host. Backpressure on the child's
    # stdin also applies to the host; each JSON-RPC frame is forwarded once.
    with os.fdopen(os.dup(0), "rb") as source, child.stdin:
        try:
            for line in source:
                try:
                    line = enrich(line)
                except (AttributeError, KeyError, TypeError, ValueError, OSError, sqlite3.Error):
                    pass  # Monitoring is optional. Preserve the original request.
                child.stdin.write(line)
                child.stdin.flush()
        except BrokenPipeError:
            pass


def main():
    if sys.argv[1:] == ["hook"]:
        os.umask(0o077)
        try:
            record(json.load(sys.stdin))
        except (AttributeError, TypeError, ValueError, OSError, sqlite3.Error):
            pass
        return 0
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        sys.exit("Usage: claude_mcp_turn.py hook | -- <stdio MCP command> [args...]")
    child = subprocess.Popen(sys.argv[2:], stdin=subprocess.PIPE)  # nosec B603: configured stdio command
    # Enforce privacy for the local cache without changing the child command's umask.
    os.umask(0o077)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda signum, _frame: child.send_signal(signum))
    threading.Thread(target=forward, args=(child,), daemon=True).start()
    status = child.wait()
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    sys.exit(main())
