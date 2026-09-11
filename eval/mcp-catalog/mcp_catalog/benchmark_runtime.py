"""Native four-cell launcher for the MCP catalog benchmark."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Literal, cast


LOGGER = logging.getLogger("akb.mcp_catalog_runtime")
REPO_ROOT = Path(__file__).resolve().parents[3]
BENCHMARK_PROFILE = "transport-proxy"
BENCHMARK_SCENARIO = "app-control-plane"
BENCHMARK_CELL_KEYS = (
    "primary:http",
    "primary:stdio",
    "lightweight:http",
    "lightweight:stdio",
)
BENCHMARK_CELL_PORT_STRIDE = 10
OPENROUTER_BASE_URL_ENV = "MCP_BENCH_OPENROUTER_BASE_URL"
OPENROUTER_PROVIDER_KEY_ENV = "MCP_BENCH_OPENROUTER_" + "API_KEY"


@dataclasses.dataclass(frozen=True, slots=True)
class BenchmarkCredentialNames:
    username_env: str = "AKB_E2E_USERNAME"
    password_env: str = "AKB_E2E_PASSWORD"
    pat_env: str = "AKB_E2E_PAT"


@dataclasses.dataclass(frozen=True, slots=True)
class BenchmarkRuntimeConfig:
    checkout: Path
    runtime_root: Path
    compose_file: Path
    compose_project: str
    app_port: int = 8000
    embed_port: int = 8888
    fixture_port: int = 8889
    postgres_port: int = 15432
    minio_port: int = 9000
    timeout_seconds: float = 180.0
    credentials: BenchmarkCredentialNames = dataclasses.field(default_factory=BenchmarkCredentialNames)
    scenario: Literal["app-control-plane"] = "app-control-plane"

    @property
    def runtime_script(self) -> Path:
        return self.checkout / "scripts" / "ci" / "e2e_runtime.py"


def benchmark_provider_credential_names() -> dict[str, str]:
    """Return the benchmark-only provider environment names for its descriptor."""

    return {
        "openrouter_base_url_env": OPENROUTER_BASE_URL_ENV,
        "openrouter_provider_key_env": OPENROUTER_PROVIDER_KEY_ENV,
    }


async def terminate_process(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 10.0,
) -> None:
    """Terminate a child process group and escalate after a grace period."""

    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await process.wait()


@dataclasses.dataclass
class BenchmarkCellProcess:
    key: str
    process: asyncio.subprocess.Process
    descriptor: dict[str, object]


class BenchmarkCellSupervisor:
    """Run one repository-owned E2E runtime per benchmark model/transport cell."""

    def __init__(self, config: BenchmarkRuntimeConfig) -> None:
        self.config = config
        self._children: dict[str, BenchmarkCellProcess] = {}
        self._stop_event = asyncio.Event()
        self._cleaned = False
        self._log_path = config.runtime_root / "logs" / "benchmark-cells.log"

    def request_stop(self) -> None:
        self._stop_event.set()

    def _child_command(self, key: str, index: int, root: Path) -> list[str]:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("benchmark runtime requires uv")
        offset = index * BENCHMARK_CELL_PORT_STRIDE
        return [
            uv,
            "run",
            "--locked",
            "--project",
            str(self.config.checkout / "backend"),
            "python",
            str(self.config.runtime_script),
            "serve",
            "--checkout",
            str(self.config.checkout),
            "--runtime-root",
            str(root),
            "--compose-file",
            str(self.config.compose_file),
            "--compose-project",
            f"{self.config.compose_project}-{key.replace(':', '-')}",
            "--app-port",
            str(self.config.app_port + offset),
            "--embed-port",
            str(self.config.embed_port + offset),
            "--fixture-port",
            str(self.config.fixture_port + offset),
            "--postgres-port",
            str(self.config.postgres_port + offset),
            "--minio-port",
            str(self.config.minio_port + offset),
            "--username-env",
            self.config.credentials.username_env,
            "--password-env",
            self.config.credentials.password_env,
            "--pat-env",
            self.config.credentials.pat_env,
            "--profile",
            BENCHMARK_PROFILE,
            "--scenario",
            BENCHMARK_SCENARIO,
        ]

    async def _start_cell(self, key: str, index: int) -> BenchmarkCellProcess:
        root = self.config.runtime_root / "cells" / key.replace(":", "-")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self._log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._log_path.open("ab", buffering=0) as handle:
            process = await asyncio.create_subprocess_exec(
                *self._child_command(key, index, root),
                cwd=str(root),
                env=os.environ.copy(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=handle,
                start_new_session=True,
            )
        child = BenchmarkCellProcess(key, process, {})
        self._children[key] = child
        if process.stdout is None:
            await terminate_process(process)
            self._children.pop(key, None)
            raise RuntimeError(f"benchmark cell {key} did not expose a descriptor stream")
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=self.config.timeout_seconds)
            descriptor = json.loads(line.decode("utf-8"))
        except (asyncio.TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
            await terminate_process(process)
            self._children.pop(key, None)
            raise RuntimeError(f"benchmark cell {key} did not become ready") from None
        if not isinstance(descriptor, dict) or descriptor.get("status") != "ready":
            await terminate_process(process)
            self._children.pop(key, None)
            raise RuntimeError(f"benchmark cell {key} returned an invalid descriptor")
        child.descriptor = descriptor
        return child

    async def _start_cells(self) -> list[BenchmarkCellProcess]:
        tasks = [
            asyncio.create_task(self._start_cell(key, index), name=f"start-{key}")
            for index, key in enumerate(BENCHMARK_CELL_KEYS)
        ]
        try:
            return await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.cleanup()
            raise

    def descriptor(self, cells: list[BenchmarkCellProcess]) -> dict[str, object]:
        keys = {cell.key for cell in cells}
        if keys != set(BENCHMARK_CELL_KEYS):
            raise RuntimeError("benchmark cells do not match the registered matrix")
        first = json.loads(json.dumps(cells[0].descriptor))
        first["benchmark_cells"] = {
            child.key: child.descriptor
            for child in sorted(cells, key=lambda item: item.key)
        }
        credentials = first.get("credentials")
        if isinstance(credentials, dict):
            credentials.update(benchmark_provider_credential_names())
        evidence = first.get("evidence")
        if isinstance(evidence, dict):
            evidence["benchmark_cells"] = {
                child.key: child.descriptor.get("evidence", {})
                for child in sorted(cells, key=lambda item: item.key)
            }
        return first

    async def run(self) -> int:
        try:
            self.config.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.config.runtime_root, 0o700)
            (self.config.runtime_root / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
            cells = await self._start_cells()
            print(json.dumps(self.descriptor(cells), separators=(",", ":"), ensure_ascii=False), flush=True)
            stop_task = asyncio.create_task(self._stop_event.wait(), name="benchmark-stop")
            wait_tasks = {
                key: asyncio.create_task(child.process.wait(), name=f"benchmark-{key}")
                for key, child in self._children.items()
            }
            try:
                done, _pending = await asyncio.wait(
                    [stop_task, *wait_tasks.values()],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done or self._stop_event.is_set():
                    return 0
                return 1
            finally:
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
                for task in wait_tasks.values():
                    if not task.done():
                        task.cancel()
                for task in wait_tasks.values():
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        except Exception as exc:
            LOGGER.error("benchmark cell runtime failed: %s", str(exc))
            return 1
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        await asyncio.gather(
            *(terminate_process(child.process) for child in self._children.values()),
            return_exceptions=True,
        )
        self._children.clear()


def _parse_args(argv: list[str] | None = None) -> BenchmarkRuntimeConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve",))
    parser.add_argument("--checkout", type=Path, default=REPO_ROOT)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--compose-file", type=Path)
    parser.add_argument("--compose-project", default="")
    parser.add_argument("--app-port", type=int, default=8000)
    parser.add_argument("--embed-port", type=int, default=8888)
    parser.add_argument("--fixture-port", type=int, default=8889)
    parser.add_argument("--postgres-port", type=int, default=15432)
    parser.add_argument("--minio-port", type=int, default=9000)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--username-env", default="AKB_E2E_USERNAME")
    parser.add_argument("--password-env", default="AKB_E2E_PASSWORD")
    parser.add_argument("--pat-env", default="AKB_E2E_PAT")
    parser.add_argument(
        "--scenario",
        choices=(BENCHMARK_SCENARIO,),
        default=BENCHMARK_SCENARIO,
        help="benchmark fixture scenario",
    )
    args = parser.parse_args(argv)

    checkout = args.checkout.expanduser().resolve()
    runtime_root = (
        args.runtime_root.expanduser().resolve()
        if args.runtime_root is not None
        else Path(tempfile.mkdtemp(prefix="akb-catalog-runtime-"))
    )
    compose_file = (
        args.compose_file.expanduser().resolve()
        if args.compose_file is not None
        else checkout / "scripts" / "ci" / "dependency-compose.yaml"
    )
    project = args.compose_project or f"akb-catalog-{os.getpid()}-{secrets.token_hex(3)}"
    return BenchmarkRuntimeConfig(
        checkout=checkout,
        runtime_root=runtime_root,
        compose_file=compose_file,
        compose_project=project,
        app_port=args.app_port,
        embed_port=args.embed_port,
        fixture_port=args.fixture_port,
        postgres_port=args.postgres_port,
        minio_port=args.minio_port,
        timeout_seconds=args.timeout_seconds,
        credentials=BenchmarkCredentialNames(
            args.username_env,
            args.password_env,
            args.pat_env,
        ),
        scenario=cast(Literal["app-control-plane"], args.scenario),
    )


async def _async_main(config: BenchmarkRuntimeConfig) -> int:
    supervisor = BenchmarkCellSupervisor(config)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, supervisor.request_stop)
    return await supervisor.run()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(_async_main(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
