from __future__ import annotations

import json
from unittest.mock import AsyncMock

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


@pytest.mark.parametrize("mode", [[], ["--verify"]], ids=["backfill", "verify"])
@pytest.mark.parametrize("option", ["--limit", "--batch-size"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_nonpositive_bounds_are_rejected_before_service_or_pool_work(
    monkeypatch, capsys, mode, option, value,
) -> None:
    backfill = AsyncMock(return_value=BridgeBodyBackfillReport(dry_run=False))
    verify = AsyncMock(return_value=bridge_body_backfill.BridgeBodyVerifyReport())
    close_pool = AsyncMock()
    monkeypatch.setattr(bridge_body_backfill, "backfill_bridge_bodies", backfill)
    monkeypatch.setattr(bridge_body_backfill, "verify_bridge_bodies", verify)
    monkeypatch.setattr("app.db.postgres.close_pool", close_pool)

    code = cli.main(["bridge-body-backfill", *mode, option, value])

    backfill.assert_not_awaited()
    verify.assert_not_awaited()
    close_pool.assert_not_awaited()
    assert code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert f"{option} must be positive" in output.err


def test_a_missing_vault_is_reported_as_usage_not_a_crash(monkeypatch, capsys) -> None:
    from app.exceptions import ValidationError

    async def fake(**kwargs):
        raise ValidationError("Vault not found: nope")

    monkeypatch.setattr(bridge_body_backfill, "backfill_bridge_bodies", fake)
    assert cli.main(["bridge-body-backfill", "--vault", "nope"]) == 2
    assert "Vault not found" in capsys.readouterr().err


def test_verify_reports_and_exits_zero_when_every_body_still_agrees(monkeypatch, capsys) -> None:
    from app.services.bridge_body_backfill import BridgeBodyVerifyReport

    async def fake(**kwargs):
        return BridgeBodyVerifyReport(vault="one-vault", checked=665, matched=665)

    monkeypatch.setattr(bridge_body_backfill, "verify_bridge_bodies", fake)
    assert cli.main(["bridge-body-backfill", "--vault", "one-vault", "--verify"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True
    assert printed["matched"] == 665


def test_verify_exits_nonzero_on_a_body_that_no_longer_agrees(monkeypatch, capsys) -> None:
    """The one result that must not pass quietly."""
    from app.services.bridge_body_backfill import BridgeBodyVerifyReport

    async def fake(**kwargs):
        return BridgeBodyVerifyReport(checked=665, matched=664, mismatched=1)

    monkeypatch.setattr(bridge_body_backfill, "verify_bridge_bodies", fake)
    assert cli.main(["bridge-body-backfill", "--verify"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_dry_run_and_verify_are_not_the_same_question(monkeypatch, capsys) -> None:
    calls = _capture(monkeypatch, BridgeBodyBackfillReport(dry_run=False))
    assert cli.main(["bridge-body-backfill", "--dry-run", "--verify"]) == 2
    assert calls == []


def test_verify_without_a_limit_surveys_the_whole_population(monkeypatch, capsys) -> None:
    """The backfill's batch default is the wrong default for a survey.

    A verify that stops at the backfill's 1000 and still prints `ok` is a
    check that passed by not looking — measured live, it reported ok after
    seeing 1,000 of 2,191.
    """
    from app.services.bridge_body_backfill import BridgeBodyVerifyReport

    seen: list[int] = []

    async def fake(**kwargs):
        seen.append(kwargs["limit"])
        return BridgeBodyVerifyReport(checked=2191, total=2191, matched=2191)

    monkeypatch.setattr(bridge_body_backfill, "verify_bridge_bodies", fake)
    assert cli.main(["bridge-body-backfill", "--vault", "v", "--verify"]) == 0
    assert seen == [100_000_000]
    printed = json.loads(capsys.readouterr().out)
    assert printed["complete"] is True


def test_a_backfill_without_a_limit_keeps_its_batch_default(monkeypatch, capsys) -> None:
    calls = _capture(monkeypatch, BridgeBodyBackfillReport(dry_run=False))
    assert cli.main(["bridge-body-backfill", "--vault", "v"]) == 0
    assert calls[0]["limit"] == 1000


def test_a_partial_survey_says_so_instead_of_looking_complete(monkeypatch, capsys) -> None:
    from app.services.bridge_body_backfill import BridgeBodyVerifyReport

    async def fake(**kwargs):
        assert kwargs == {
            "vault": None, "limit": 1000, "batch_size": bridge_body_backfill.DEFAULT_BATCH_SIZE,
        }
        return BridgeBodyVerifyReport(checked=1000, total=2191, matched=1000)

    monkeypatch.setattr(bridge_body_backfill, "verify_bridge_bodies", fake)
    assert cli.main(["bridge-body-backfill", "--verify", "--limit", "1000"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["complete"] is False
    assert "surveyed 1000 of 2191" in out.err
