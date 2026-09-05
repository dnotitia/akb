from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass

from app import cli
from app.db import postgres
import app.services.git_service as git_service
import app.services.native_revision_backfill as backfill_module
import app.services.native_revision_cutover as cutover_module
import app.services.external_git_retirement as retirement_module


def test_native_revision_cutover_cli_routes_plan(monkeypatch, capsys) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_execute(phase: str, **kwargs):
        calls.append((phase, kwargs))
        return {"status": "planned", "cutover_id": "00000000-0000-0000-0000-000000000001"}

    monkeypatch.setattr(cli, "_execute_native_revision_cutover", fake_execute)

    code = cli.main(
        [
            "migrate-revision-backend",
            "plan",
            "--coverage-version",
            "fixture-v1",
        ]
    )

    assert code == 0
    assert calls == [("plan", {"coverage_version": "fixture-v1", "cutover_id": None})]
    assert json.loads(capsys.readouterr().out)["status"] == "planned"


def test_native_revision_cutover_cli_plan_includes_archived_vaults(monkeypatch) -> None:
    active_id = uuid.uuid4()
    archived_id = uuid.uuid4()
    refs = {"active": "a" * 40, "archived": "b" * 40}
    planned: list[tuple[object, str]] = []

    class _Connection:
        async def fetch(self, sql: str):
            assert "status <> 'deleted'" in sql
            return [
                {"id": active_id, "name": "active"},
                {"id": archived_id, "name": "archived"},
            ]

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    class _Git:
        def current_commit(self, name: str) -> str:
            return refs[name]

    @dataclass
    class _Receipt:
        status: str

    class _Cutover:
        def __init__(self, pool, *, backfill, verifier) -> None:
            assert isinstance(pool, _Pool)
            assert backfill is not None
            assert verifier is not None

        async def plan(self, *, vaults, coverage_version: str):
            planned.extend((item.namespace_id, item.fixed_ref) for item in vaults)
            assert coverage_version == "fixture-v1"
            return _Receipt(status="planned")

    async def _noop() -> None:
        return None

    async def _pool() -> _Pool:
        return _Pool()

    monkeypatch.setattr(postgres, "init_db", _noop)
    monkeypatch.setattr(postgres, "get_pool", _pool)
    monkeypatch.setattr(postgres, "close_pool", _noop)
    monkeypatch.setattr(git_service, "GitService", _Git)
    monkeypatch.setattr(backfill_module, "NativeRevisionBackfill", lambda pool, *, git: object())
    monkeypatch.setattr(cutover_module, "NativeRevisionCutoverVerifier", lambda pool, *, git: object())
    monkeypatch.setattr(cutover_module, "NativeRevisionCutover", _Cutover)

    report = asyncio.run(
        cli._execute_native_revision_cutover(
            "plan",
            coverage_version="fixture-v1",
            cutover_id=None,
        )
    )

    assert report == {"status": "planned"}
    assert planned == [(active_id, refs["active"]), (archived_id, refs["archived"])]


def test_native_revision_cutover_cli_routes_abort(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_execute(phase: str, **kwargs):
        calls.append((phase, kwargs))
        return {"status": "aborted"}

    monkeypatch.setattr(cli, "_execute_native_revision_cutover", fake_execute)
    cutover_id = "00000000-0000-0000-0000-000000000002"

    assert cli.main(["migrate-revision-backend", "abort", "--cutover-id", cutover_id]) == 0
    assert calls == [("abort", {"coverage_version": None, "cutover_id": cutover_id})]


def test_native_revision_cutover_cli_routes_exact_orphan_supersession(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_execute(phase: str, **kwargs):
        calls.append((phase, kwargs))
        return {"status": "superseded"}

    monkeypatch.setattr(cli, "_execute_native_revision_cutover", fake_execute)
    migration_run_id = "00000000-0000-0000-0000-000000000003"

    assert (
        cli.main(
            [
                "migrate-revision-backend",
                "supersede-orphan",
                "--migration-run-id",
                migration_run_id,
            ]
        )
        == 0
    )
    assert calls == [
        (
            "supersede-orphan",
            {
                "coverage_version": None,
                "cutover_id": None,
                "migration_run_id": migration_run_id,
            },
        )
    ]


def test_native_revision_cutover_cli_rejects_incomplete_arguments(capsys) -> None:
    assert cli.main(["migrate-revision-backend", "apply"]) == 2
    assert "migrate-revision-backend" in capsys.readouterr().err


def test_external_git_retirement_cli_routes_only_with_the_exact_downtime_confirmation(monkeypatch, capsys) -> None:
    calls: list[dict[str, str]] = []
    vault_id = "00000000-0000-0000-0000-000000000093"

    async def fake_retire(**kwargs):
        calls.append(kwargs)
        return {"status": "retired", "vault_id": vault_id}

    monkeypatch.setattr(cli, "_execute_external_git_retirement", fake_retire, raising=False)
    args = [
        "migrate-revision-backend",
        "retire-external-git",
        "--vault-id",
        vault_id,
        "--manifest-file",
        "/safe/operator/manifest.json",
        "--idempotency-key",
        "00000000-0000-0000-0000-000000000094",
        "--requested-by",
        "collector-adoption-operator",
        "--confirm-planned-downtime",
        f"RETIRE-EXTERNAL-GIT:{vault_id}",
    ]

    assert cli.main(args) == 0
    assert calls == [
        {
            "vault_id": vault_id,
            "manifest_file": "/safe/operator/manifest.json",
            "idempotency_key": "00000000-0000-0000-0000-000000000094",
            "requested_by": "collector-adoption-operator",
            "planned_downtime_confirmation": f"RETIRE-EXTERNAL-GIT:{vault_id}",
        }
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "retired"

    calls.clear()
    bad_args = [
        *args[:-1],
        "RETIRE-EXTERNAL-GIT:wrong-vault",
    ]
    assert cli.main(bad_args) == 1
    assert calls == []
    assert "planned-downtime confirmation" in capsys.readouterr().err


def test_external_git_retirement_cli_binds_the_operator_vault_id_to_the_service(monkeypatch) -> None:
    vault_id = uuid.UUID("00000000-0000-0000-0000-000000000093")
    manifest = object()
    calls: list[dict[str, object]] = []

    @dataclass
    class _Receipt:
        status: str

    class _Retirement:
        def __init__(self, pool) -> None:
            assert pool == "pool"

        async def retire(self, **kwargs):
            calls.append(kwargs)
            return _Receipt(status="retired")

    async def _init_db() -> None:
        return None

    async def _get_pool():
        return "pool"

    async def _close_pool() -> None:
        return None

    monkeypatch.setattr(postgres, "init_db", _init_db)
    monkeypatch.setattr(postgres, "get_pool", _get_pool)
    monkeypatch.setattr(postgres, "close_pool", _close_pool)
    monkeypatch.setattr(retirement_module, "load_adoption_manifest", lambda _path: manifest)
    monkeypatch.setattr(retirement_module, "ExternalGitRetirement", _Retirement)

    report = asyncio.run(
        cli._execute_external_git_retirement(
            vault_id=str(vault_id),
            manifest_file="/safe/operator/manifest.json",
            idempotency_key="00000000-0000-0000-0000-000000000094",
            requested_by="collector-adoption-operator",
            planned_downtime_confirmation=f"RETIRE-EXTERNAL-GIT:{vault_id}",
        )
    )

    assert report == {"status": "retired"}
    assert calls == [
        {
            "manifest": manifest,
            "expected_vault_id": vault_id,
            "idempotency_key": uuid.UUID("00000000-0000-0000-0000-000000000094"),
            "requested_by": "collector-adoption-operator",
        }
    ]
