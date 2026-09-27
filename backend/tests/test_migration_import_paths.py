"""Repeated migration loading must not inflate spawned workers' import paths."""
from pathlib import Path
import json
import os
import subprocess
import sys

from app.db import postgres


BACKEND = Path(__file__).resolve().parents[1]
MIGRATIONS = BACKEND / 'app/db/migrations'


def test_repeated_real_migration_import_preserves_caller_path(monkeypatch):
    root = str(BACKEND)
    # The caller's root may already be later in the path. Keep that precedence,
    # list identity, and unrelated intentional duplicate instead of hoisting it.
    paths = [entry for entry in sys.path if entry != root]
    paths += ['unrelated-migration-path', 'unrelated-migration-path', root]
    monkeypatch.setattr(sys, 'path', paths)
    expected = list(paths)
    for _ in range(2):
        for file in sorted(MIGRATIONS.glob('[0-9]*.py')):
            module = postgres._load_migration(file.name)
            assert module is not None
            assert callable(module.migrate)
    assert sys.path is paths
    assert sys.path == expected


def test_real_migration_bootstrap_adds_missing_root_only_once():
    migration = MIGRATIONS / '033_users_auth_provider.py'
    code = '''
import importlib.util
import json
import sys
root, migration = sys.argv[1:]
sys.path[:] = [entry for entry in sys.path if entry not in (root, '')]
assert root not in sys.path
for index in range(2):
    spec = importlib.util.spec_from_file_location('migration_bootstrap_probe', migration)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.migrate)
print(json.dumps({'root_count': sys.path.count(root), 'root_first': sys.path[0] == root}))
'''
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    result = subprocess.run(
        [sys.executable, '-I', '-c', code, str(BACKEND), str(migration)],
        cwd=BACKEND, env=environment, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'root_count': 1, 'root_first': True}
