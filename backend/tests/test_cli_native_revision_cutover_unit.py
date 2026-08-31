from __future__ import annotations

import json

from app import cli


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


def test_native_revision_cutover_cli_routes_abort(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_execute(phase: str, **kwargs):
        calls.append((phase, kwargs))
        return {"status": "aborted"}

    monkeypatch.setattr(cli, "_execute_native_revision_cutover", fake_execute)
    cutover_id = "00000000-0000-0000-0000-000000000002"

    assert cli.main(["migrate-revision-backend", "abort", "--cutover-id", cutover_id]) == 0
    assert calls == [("abort", {"coverage_version": None, "cutover_id": cutover_id})]


def test_native_revision_cutover_cli_rejects_incomplete_arguments(capsys) -> None:
    assert cli.main(["migrate-revision-backend", "apply"]) == 2
    assert "migrate-revision-backend" in capsys.readouterr().err
