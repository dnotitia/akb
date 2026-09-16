from __future__ import annotations

import json

import pytest

from app import cli
from app.services import bridge_body_backfill
from app.services.bridge_body_backfill import BridgeBodyBackfillReport


@pytest.fixture(autouse=True)
def _no_pool(monkeypatch):
    """The CLI closes the pool on the way out; nothing here opened one."""

    async def noop():
        return None

    monkeypatch.setattr("app.db.postgres.close_pool", noop)


def _capture(monkeypatch, report: BridgeBodyBackfillReport) -> list[dict]:
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return report

    monkeypatch.setattr(bridge_body_backfill, "backfill_bridge_bodies", fake)
    return calls


def test_the_cli_passes_every_operator_option_through(monkeypatch, capsys) -> None:
    report = BridgeBodyBackfillReport(dry_run=True, vault="one-vault", examined=3, migrated=3)
    calls = _capture(monkeypatch, report)

    code = cli.main([
        "bridge-body-backfill",
        "--vault", "one-vault",
        "--limit", "25",
        "--batch-size", "5",
        "--dry-run",
    ])

    assert code == 0
    assert calls == [{"vault": "one-vault", "limit": 25, "batch_size": 5, "dry_run": True}]
    printed = json.loads(capsys.readouterr().out)
    assert printed["migrated"] == 3
    assert printed["dry_run"] is True


def test_a_run_that_moved_nothing_exits_nonzero(monkeypatch, capsys) -> None:
    """An operator must not schedule around a stalled run as if it succeeded."""
    report = BridgeBodyBackfillReport(dry_run=False, examined=4, migrated=0, stalled=True)
    _capture(monkeypatch, report)

    assert cli.main(["bridge-body-backfill"]) == 1
    assert json.loads(capsys.readouterr().out)["stalled"] is True


def test_an_unknown_option_does_not_start_a_run(monkeypatch, capsys) -> None:
    calls = _capture(monkeypatch, BridgeBodyBackfillReport(dry_run=False))
    assert cli.main(["bridge-body-backfill", "--evrything"]) == 2
    assert calls == []


def test_a_non_numeric_limit_does_not_start_a_run(monkeypatch, capsys) -> None:
    calls = _capture(monkeypatch, BridgeBodyBackfillReport(dry_run=False))
    assert cli.main(["bridge-body-backfill", "--limit", "many"]) == 2
    assert calls == []


def test_a_missing_vault_is_reported_as_usage_not_a_crash(monkeypatch, capsys) -> None:
    from app.exceptions import ValidationError

    async def fake(**kwargs):
        raise ValidationError("Vault not found: nope")

    monkeypatch.setattr(bridge_body_backfill, "backfill_bridge_bodies", fake)
    assert cli.main(["bridge-body-backfill", "--vault", "nope"]) == 2
    assert "Vault not found" in capsys.readouterr().err
