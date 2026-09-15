from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path

import pytest

import mcp_catalog.cli as cli_module
from mcp_catalog.runner import BenchmarkRunFailure
from mcp_catalog.runtime import RuntimeContractError


class _SignalRunner:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.secrets: tuple[str, ...] = ()

    async def run(self) -> dict[str, object]:
        loop = asyncio.get_running_loop()
        loop.call_soon(os.kill, os.getpid(), signal.SIGTERM)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise BenchmarkRunFailure(
                RuntimeContractError("benchmark interrupted by signal", stage="signal"),
                {"status": "incomplete", "runs": {}, "catalogs": {}},
                (),
                (),
            ) from exc
        return {}


@pytest.mark.asyncio
async def test_sigterm_cancels_run_and_writes_incomplete_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "interrupted.json"
    monkeypatch.setattr(cli_module, "load_inputs", lambda *_args: (None, [], None))
    monkeypatch.setattr(cli_module, "BenchmarkRunner", _SignalRunner)
    args = argparse.Namespace(
        manifest=Path("manifest.json"),
        corpus=Path("tasks.json"),
        descriptor=Path("descriptor.json"),
        arm="baseline",
        output=output,
        checkpoint=None,
        resume=None,
    )

    with pytest.raises(BenchmarkRunFailure):
        await cli_module.run(args)

    assert '"status": "incomplete"' in output.read_text(encoding="utf-8")
