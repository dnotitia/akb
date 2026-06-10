"""Guard: every migration file on disk is registered in the runtime
applier (`_apply_migrations` in app/db/postgres.py).

Migrations are run from an explicit hardcoded list, NOT by globbing the
directory — so a newly-added `0NN_*.py` file silently never runs until it
is also added to that list. This test fails loudly on that omission (which
shipped a no-op migration in 0.8.8 before this guard existed).
"""
from __future__ import annotations

from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
_MIG_DIR = _BACKEND / "app" / "db" / "migrations"
_POSTGRES = _BACKEND / "app" / "db" / "postgres.py"

# Files intentionally NOT in the runtime list (applied via init.sql on a
# fresh DB / superseded). Keep this set tiny and documented.
_INTENTIONALLY_UNREGISTERED = {
    "001_relations_to_edges.py",
    "013_nfc_normalize.py",
}


def test_every_migration_file_is_registered():
    on_disk = {p.name for p in _MIG_DIR.glob("[0-9][0-9][0-9]_*.py")}
    src = _POSTGRES.read_text(encoding="utf-8")
    missing = [
        name
        for name in sorted(on_disk - _INTENTIONALLY_UNREGISTERED)
        if f'"{name}"' not in src
    ]
    assert not missing, (
        "migration file(s) on disk but not registered in "
        f"_apply_migrations(): {missing} — add them to the list in "
        "app/db/postgres.py (or to _INTENTIONALLY_UNREGISTERED if applied "
        "elsewhere)."
    )
