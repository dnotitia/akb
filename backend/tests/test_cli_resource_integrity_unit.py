from __future__ import annotations

import json

from app import cli
from app.services import resource_integrity


def test_repair_resource_hashes_cli_passes_operator_options(monkeypatch, capsys) -> None:
    calls: list[dict] = []

    async def fake_repair_resource_hashes(**kwargs):
        calls.append(kwargs)
        return {
            "documents_checked": 2,
            "documents_repaired": 2,
            "files_checked": 0,
            "files_repaired": 0,
            "errors": [],
        }

    monkeypatch.setattr(resource_integrity, "repair_resource_hashes", fake_repair_resource_hashes)

    code = cli.main([
        "repair-resource-hashes",
        "--vault",
        "repair-vault",
        "--documents-only",
        "--limit",
        "2",
    ])

    assert code == 0
    assert calls == [
        {
            "vault": "repair-vault",
            "include_documents": True,
            "include_files": False,
            "limit": 2,
        },
    ]
    assert json.loads(capsys.readouterr().out) == {
        "documents_checked": 2,
        "documents_repaired": 2,
        "files_checked": 0,
        "files_repaired": 0,
        "errors": [],
    }


def test_repair_resource_hashes_cli_returns_failure_when_repair_reports_errors(monkeypatch) -> None:
    async def fake_repair_resource_hashes(**kwargs):
        return {
            "documents_checked": 1,
            "documents_repaired": 0,
            "files_checked": 0,
            "files_repaired": 0,
            "errors": ["document repair-vault/missing.md: missing.md"],
        }

    monkeypatch.setattr(resource_integrity, "repair_resource_hashes", fake_repair_resource_hashes)

    code = cli.main(["repair-resource-hashes", "--files-only"])

    assert code == 1


def test_repair_resource_hashes_cli_rejects_invalid_limit(capsys) -> None:
    code = cli.main(["repair-resource-hashes", "--limit", "zero"])

    assert code == 2
    assert "--limit must be an integer" in capsys.readouterr().err
