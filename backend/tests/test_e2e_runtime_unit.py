"""Focused contract tests for the repository-owned E2E runtime."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_DIR = REPO_ROOT / "scripts" / "ci"
sys.path.insert(0, str(CI_DIR))

from e2e_runtime import (  # noqa: E402
    BlockedRuntimeConfig,
    CapabilityProfile,
    CredentialNames,
    E2ERuntime,
    ManagedProcess,
    ProvisioningFailure,
    SOURCE_REVISION_ENV,
    RuntimeConfig,
    _fixture_schema_fingerprint,
    _fixture_v2_manifest,
    _parse_args,
    prepare_private_runtime_root,
    select_capability_profile,
    terminate_process,
)
import e2e_runtime  # noqa: E402
from e2e_gate_observability import (  # noqa: E402
    EVENT_PREFIX,
    emit_gate_event,
    shell_exit_code,
    signal_from_returncode,
)
import e2e_suite_runner  # noqa: E402
from e2e_suite_runner import (  # noqa: E402
    CURATED_SUITES,
    DEFERRED_SUITE_GROUPS,
    DeferredSuiteGroup,
    SuiteResult,
    parse_assertion_summary,
    validate_suite_manifest,
)
from fixture_control import create_app  # noqa: E402


COMPOSE_FILE = CI_DIR / "dependency-compose.yaml"
BOOTSTRAP = CI_DIR / "ubuntu_e2e_bootstrap.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
LOCAL_CANONICAL_RUNNER = REPO_ROOT / "scripts" / "run_canonical_e2e.sh"


def test_stdio_sample_image_fixture_is_a_small_decodable_png() -> None:
    path = REPO_ROOT / "eval" / "mcp-catalog" / "fixtures" / "sample-image.png"

    assert path.stat().st_size <= 64 * 1024
    with Image.open(path) as image:
        assert image.format == "PNG"
        image.load()
        assert image.size == (400, 400)


def test_authorization_fixture_vault_repository_is_initialized_idempotently(tmp_path: Path) -> None:
    runtime = E2ERuntime(make_config(tmp_path))
    runtime.config.vault_dir.mkdir(parents=True)

    first = runtime._ensure_fixture_git_repository("catalog-bench-vault-authorization")
    second = runtime._ensure_fixture_git_repository("catalog-bench-vault-authorization")

    assert first == second
    bare = Path(first)
    assert bare.is_dir()
    assert (bare / "HEAD").is_file()
    assert (bare / "objects").is_dir()


def make_config(tmp_path: Path, *, mode: str = "serve") -> RuntimeConfig:
    return RuntimeConfig(
        checkout=REPO_ROOT,
        runtime_root=tmp_path / "runtime",
        mode=mode,  # type: ignore[arg-type]
        compose_file=COMPOSE_FILE,
        compose_project="akb-e2e-unit",
        credentials=CredentialNames("TEST_USERNAME_ENV", "TEST_PASSWORD_ENV"),
    )


@pytest.mark.asyncio
async def test_fixture_vault_rejects_owner_access_grants_before_writing(tmp_path: Path) -> None:
    runtime = E2ERuntime(make_config(tmp_path))
    executed: list[object] = []

    class Connection:
        async def execute(self, *args: object) -> None:
            executed.append(args)

    with pytest.raises(ProvisioningFailure, match="owner_id"):
        await runtime._insert_fixture_vault(
            Connection(),
            namespace="fixture",
            label="owner-grant",
            owner_id=uuid.uuid4(),
            grants=[(uuid.uuid4(), "owner")],
            granted_by=uuid.uuid4(),
        )

    assert executed == []


def test_descriptor_is_schema_v2_and_never_contains_credential_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_USERNAME_ENV", "external-user-value")
    monkeypatch.setenv("TEST_PASSWORD_ENV", "external-password-value")
    runtime = E2ERuntime(make_config(tmp_path))

    descriptor = runtime.descriptor()
    assert set(descriptor) == {
        "schema_version",
        "status",
        "scenario",
        "services",
        "credentials",
    }
    assert descriptor["schema_version"] == 2
    assert descriptor["status"] == "ready"
    assert descriptor["scenario"] == "empty"
    assert set(descriptor["services"]) == {"app", "fixture"}
    assert descriptor["services"]["app"] == {
        "origin": "http://127.0.0.1:8000",
        "health": {"method": "GET", "url": "http://127.0.0.1:8000/readyz"},
        "discovery": {
            "method": "GET",
            "url": "http://127.0.0.1:8000/openapi.json",
        },
    }
    fixture = descriptor["services"]["fixture"]
    assert fixture["origin"] == "http://127.0.0.1:8889"
    assert fixture["health"] == {
        "method": "GET",
        "url": "http://127.0.0.1:8889/health",
    }
    assert fixture["discovery"] == {
        "method": "GET",
        "url": "http://127.0.0.1:8889/discover",
    }
    assert set(fixture) == {"origin", "health", "reset", "discovery"}
    assert fixture["reset"] == {
        "method": "POST",
        "url": "http://127.0.0.1:8889/reset",
        "content_type": "application/json",
        "body": {"scenario": "empty"},
    }
    assert "schema" not in descriptor
    assert "app" not in descriptor
    assert "fixture" not in descriptor
    for service in descriptor["services"].values():
        for operation_name, operation in service.items():
            if operation_name == "origin":
                continue
            assert "path" not in operation
            assert operation["url"].startswith(service["origin"])
    assert set(descriptor["credentials"]) == {
        "username_env",
        "password_env",
        "login_path",
    }
    assert descriptor["credentials"]["username_env"] == "TEST_USERNAME_ENV"
    assert descriptor["credentials"]["password_env"] == "TEST_PASSWORD_ENV"
    serialized = str(descriptor)
    assert "external-user-value" not in serialized
    assert "external-password-value" not in serialized


def test_frontend_descriptor_exposes_web_origin_and_backend_proxy_target(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path),
            frontend_enabled=True,
            frontend_port=3017,
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"

    descriptor = runtime.descriptor()
    assert descriptor["services"]["web"] == {
        "origin": "http://127.0.0.1:3017",
        "health": {
            "method": "GET",
            "url": "http://127.0.0.1:3017",
        },
    }
    evidence = descriptor["evidence"]
    assert evidence["origin"]["frontend"] == "http://127.0.0.1:3017"
    assert evidence["frontend"] == {
        "service": "web",
        "origin": "http://127.0.0.1:3017",
        "health": {
            "method": "GET",
            "url": "http://127.0.0.1:3017",
        },
        "backend_proxy_target": "http://127.0.0.1:8000",
        "browser_input": "AKB_FRONTEND_URL",
    }

    discovery = runtime.fixture_discovery()
    assert discovery["web"] == {
        "service": "web",
        "origin": "http://127.0.0.1:3017",
        "health": {"method": "GET", "path": "/"},
        "backend_proxy_target": "http://127.0.0.1:8000",
    }


def test_frontend_runtime_requires_explicit_flag_and_supports_isolated_port():
    default = _parse_args(["serve"])
    assert default.frontend_enabled is False
    assert default.frontend_port == 3000

    configured = _parse_args(
        ["serve", "--with-frontend", "--frontend-port", "3017"]
    )
    assert configured.frontend_enabled is True
    assert configured.frontend_port == 3017


def test_runtime_accepts_explicit_dependency_ports():
    configured = _parse_args(
        [
            "serve",
            "--postgres-port",
            "15532",
            "--minio-port",
            "9100",
        ]
    )

    assert configured.postgres_port == 15532
    assert configured.minio_port == 9100


def test_frontend_owns_package_script_and_toolchain_contract():
    package = json.loads((REPO_ROOT / "frontend" / "package.json").read_text())

    assert package["packageManager"] == "pnpm@11.21.0"
    assert package["engines"]["node"] == "22.19.0"
    assert package["scripts"]["dev"] == "vite"


def test_runtime_assets_live_in_repository_common_layer():
    common_runtime = REPO_ROOT / "scripts" / "ci"
    backend_runtime = REPO_ROOT / "backend" / "scripts" / "ci"

    for name in (
        "e2e_runtime.py",
        "e2e_backend.py",
        "e2e_suite_runner.py",
        "fixture_control.py",
        "oidc_fixture.py",
        "embed_stub.py",
        "ubuntu_e2e_bootstrap.sh",
    ):
        assert (common_runtime / name).is_file()
        assert not (backend_runtime / name).exists()


@pytest.mark.asyncio
async def test_frontend_start_uses_private_root_and_per_run_backend_target(tmp_path, monkeypatch):
    pnpm_path = "/fake/toolchain/pnpm"
    monkeypatch.setattr(
        e2e_runtime.shutil,
        "which",
        lambda name: pnpm_path if name == "pnpm" else None,
    )
    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path),
            frontend_enabled=True,
            frontend_port=3017,
        )
    )
    started: list[tuple[str, list[str], str, dict[str, str] | None]] = []
    waited: list[tuple[str, str]] = []

    async def fake_spawn(
        name: str,
        command: list[str],
        log_name: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> None:
        started.append((name, command, log_name, environment))

    async def fake_wait(label: str, url: str, _predicate) -> bytes:
        waited.append((label, url))
        return b""

    runtime._spawn_host_process = fake_spawn  # type: ignore[method-assign]
    runtime._wait_http = fake_wait  # type: ignore[method-assign]

    await runtime._start_frontend()

    assert len(started) == 1
    name, command, log_name, environment = started[0]
    assert name == "frontend"
    assert log_name == "frontend.log"
    assert command == [
        pnpm_path,
        "--dir",
        str(REPO_ROOT / "frontend"),
        "run",
        "dev",
        "--host",
        "127.0.0.1",
        "--port",
        "3017",
        "--strictPort",
    ]
    assert environment == {
        "AKB_FRONTEND_BACKEND_URL": "http://127.0.0.1:8000",
        "AKB_FRONTEND_CACHE_DIR": str(tmp_path / "runtime" / "frontend-cache"),
    }
    assert waited == [("frontend", "http://127.0.0.1:3017")]


@pytest.mark.asyncio
async def test_cleanup_stops_frontend_with_the_owned_runtime_processes(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), frontend_enabled=True)
    )
    runtime._children["frontend"] = ManagedProcess(_LifecycleProcess())
    stopped: list[str] = []

    async def record_stop(name: str) -> None:
        stopped.append(name)

    async def record_compose(*_arguments: str, **_kwargs: object) -> int:
        return 0

    runtime._stop_fixture_control = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    runtime._stop_named_process = record_stop  # type: ignore[method-assign]
    runtime._compose = record_compose  # type: ignore[method-assign]

    await runtime.cleanup()

    assert stopped == ["stdio", "frontend", "backend", "embed"]


def test_fixture_discovery_declares_auth_and_observability_without_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEST_USERNAME_ENV", "external-user-value")
    monkeypatch.setenv("TEST_PASSWORD_ENV", "external-password-value")
    runtime = E2ERuntime(make_config(tmp_path))
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-installation-lifecycle",
        "namespace": "fixture-randomized",
        "fixtures": {
            "active": {"installation_id": "installation-randomized"},
        },
    }

    discovery = runtime.fixture_discovery()

    assert discovery["scenario"] == "app-installation-lifecycle"
    assert discovery["namespace"] == "fixture-randomized"
    assert discovery["fixtures"] == {
        "active": {"installation_id": "installation-randomized"},
    }
    assert discovery["access"] == {
        "login": {
            "service": "app",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "fields": ["username", "password"],
        }
    }
    assert discovery["observability"] == {
        "log_observation": {
            "service": "fixture",
            "method": "GET",
            "path": "/log-observation",
        }
    }
    serialized = json.dumps(discovery)
    assert "external-user-value" not in serialized
    assert "external-password-value" not in serialized


def test_member_installation_discovery_declares_http_contract_and_fixture_controls(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path), scenario="app-installation-lifecycle"
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    app_id = str(uuid.uuid4())
    vault_id = str(uuid.uuid4())
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-installation-lifecycle",
        "namespace": "fixture-members",
        "actors": {
            "system_admin": {"id": str(uuid.uuid4()), "role": "system_admin"},
            "reader": {
                "id": str(uuid.uuid4()),
                "username": "fixture-reader",
                "vault_role": "reader",
            },
            "writer": {
                "id": str(uuid.uuid4()),
                "username": "fixture-writer",
                "vault_role": "writer",
            },
            "target_owner": {
                "id": str(uuid.uuid4()),
                "username": "fixture-owner",
                "vault_role": "owner",
            },
            "target_admin": {
                "id": str(uuid.uuid4()),
                "username": "fixture-admin",
                "vault_role": "admin",
            },
        },
        "apps": {"target": {"id": app_id}},
        "vaults": {"active": {"id": vault_id}},
        "fixtures": {
            "status_active": {
                "app_id": app_id,
                "vault_id": vault_id,
                "installation_id": str(uuid.uuid4()),
            }
        },
    }

    discovery = runtime.fixture_discovery()

    assert discovery["member_installation_active"] == {
        "service": "app",
        "method": "GET",
        "path": f"/api/v1/apps/{app_id}/installations/{vault_id}/active",
        "app_id": app_id,
        "vault_id": vault_id,
    }
    assert discovery["controls"]["member_installation_state"] == {
        "service": "fixture",
        "method": "POST",
        "path": "/control",
        "body": {
            "action": "member_installation_state",
            "target": "status_active",
            "kind": "upgrading",
            "enabled": True,
        },
        "states": ["installing", "active", "upgrading", "blocked", "uninstalled"],
    }
    assert discovery["controls"]["member_access"] == {
        "service": "fixture",
        "method": "POST",
        "path": "/control",
        "body": {
            "action": "member_access",
            "target": "reader",
            "kind": "status_active",
            "enabled": False,
        },
    }
    assert discovery["controls"]["fault_injection"] == {
        "service": "fixture",
        "method": "POST",
        "path": "/control",
        "body": {
            "action": "fault_injection",
            "target": "status_active",
            "kind": "member_installation_unavailable",
            "enabled": True,
        },
        "disable_body": {
            "action": "fault_injection",
            "target": "status_active",
            "kind": "member_installation_unavailable",
            "enabled": False,
        },
        "kinds": ["member_installation_unavailable"],
        "targets": [
            {
                "fixture_id": "status_active",
                "installation_id": runtime._fixture_catalog["fixtures"]["status_active"]["installation_id"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_member_installation_fixture_controls_touch_only_the_selected_fixture(
    tmp_path, monkeypatch
):
    import asyncpg

    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path), scenario="app-installation-lifecycle"
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    actor_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    installation_id = uuid.uuid4()
    active_release_id = uuid.uuid4()
    next_release_id = uuid.uuid4()
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-installation-lifecycle",
        "actors": {
            "system_admin": {"id": str(admin_id), "role": "system_admin"},
            "reader": {
                "id": str(actor_id),
                "username": "fixture-reader",
                "vault_role": "reader",
            },
        },
        "releases": {"target_next": {"id": str(next_release_id)}},
        "fixtures": {
            "status_active": {
                "vault_id": str(vault_id),
                "installation_id": str(installation_id),
                "release_id": str(active_release_id),
            }
        },
    }
    calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeConnection:
        async def execute(self, query, *args):
            calls.append((query, args))
            if query.lstrip().startswith("UPDATE vault_app_installations"):
                required_fields = (
                    "lifecycle=$1",
                    "desired_release_id=$2",
                    "current_release_id=$3",
                    "blocked_reason=$4",
                )
                if not all(field in query.replace(" ", "") for field in required_fields) or len(args) != 5:
                    raise RuntimeError("CheckViolation: vault_app_installations_release_coherence")
                lifecycle, desired_release_id, current_release_id, blocked_reason, requested_id = args
                expected_state = {
                    "installing": (active_release_id, None, None),
                    "active": (active_release_id, active_release_id, None),
                    "upgrading": (next_release_id, active_release_id, None),
                    "blocked": (
                        next_release_id,
                        active_release_id,
                        "fixture_controlled_blocked",
                    ),
                    "uninstalled": (None, active_release_id, None),
                }.get(lifecycle)
                actual_state = (desired_release_id, current_release_id, blocked_reason)
                if requested_id != installation_id or actual_state != expected_state:
                    raise RuntimeError(
                        "CheckViolation: vault_app_installations_release_coherence or "
                        "vault_app_installations_blocked_reason_check"
                    )
                return "UPDATE 1"
            return "DELETE 1"

        async def close(self):
            return None

    async def fake_connect(**_kwargs):
        return FakeConnection()

    monkeypatch.setattr(asyncpg, "connect", fake_connect)

    states = ["installing", "active", "upgrading", "blocked", "uninstalled", "active"]
    state_results = [
        await runtime.fixture_control(
            "member_installation_state",
            "status_active",
            True,
            lifecycle,
        )
        for lifecycle in states
    ]
    access = await runtime.fixture_control(
        "member_access",
        "reader",
        False,
        "status_active",
    )
    restored_access = await runtime.fixture_control(
        "member_access",
        "reader",
        True,
        "status_active",
    )

    assert [result["status"] for result in state_results] == ["accepted"] * len(states)
    assert state_results[-1]["observed"] == {"installation_state": "active"}
    assert access["status"] == "accepted"
    assert restored_access["status"] == "accepted"
    assert len(calls) == len(states) + 2
    assert calls[0][0].lstrip().startswith("UPDATE vault_app_installations")
    assert calls[0][1] == ("installing", active_release_id, None, None, installation_id)
    assert calls[1][1] == ("active", active_release_id, active_release_id, None, installation_id)
    assert calls[2][1] == ("upgrading", next_release_id, active_release_id, None, installation_id)
    assert calls[3][1] == ("blocked", next_release_id, active_release_id, "fixture_controlled_blocked", installation_id)
    assert calls[4][1] == ("uninstalled", None, active_release_id, None, installation_id)
    assert calls[5][1] == ("active", active_release_id, active_release_id, None, installation_id)
    assert calls[-2][0].startswith("DELETE FROM vault_access")
    assert calls[-2][1] == (vault_id, actor_id)
    assert calls[-1][0].lstrip().startswith("INSERT INTO vault_access")
    assert calls[-1][1] == (vault_id, actor_id, admin_id)


@pytest.mark.asyncio
async def test_member_installation_unavailable_fault_is_bounded_and_reversible(
    tmp_path, monkeypatch
):
    import asyncpg
    from app.exceptions import AKBError
    from app.services import app_installation_service
    from app.services.auth_service import AuthenticatedUser

    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path), scenario="app-installation-lifecycle"
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    app_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    installation_id = uuid.uuid4()
    user = AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="fixture-owner",
        email="fixture-owner@example.invalid",
        display_name=None,
        is_admin=False,
        auth_method="jwt",
    )
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-installation-lifecycle",
        "fixtures": {
            "status_active": {
                "app_id": str(app_id),
                "vault_id": str(vault_id),
                "installation_id": str(installation_id),
            }
        },
    }
    relation_state = {"canonical": True, "renamed": False}
    sql_calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeTransaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return False

    class FakeConnection:
        def transaction(self):
            return FakeTransaction()

        async def fetchrow(self, _query, requested_vault_id):
            assert requested_vault_id == vault_id
            return {
                "id": vault_id,
                "name": "fixture-vault",
                "owner_id": uuid.UUID(user.user_id),
            }

        async def fetchval(self, query, *args):
            normalized = " ".join(query.split())
            sql_calls.append((normalized, args))
            if "to_regclass('public.vault_app_installations')" in normalized:
                return relation_state["canonical"]
            if "to_regclass('public.akb_fixture_member_status_unavailable')" in normalized:
                return relation_state["renamed"]
            if "FROM public.vault_app_installations" in normalized:
                assert args == (installation_id, app_id, vault_id)
                return True
            if "FROM vault_app_installations" in normalized:
                if not relation_state["canonical"]:
                    raise asyncpg.exceptions.UndefinedTableError(
                        'relation "vault_app_installations" does not exist'
                    )
                return True
            raise AssertionError(f"unexpected fixture query: {normalized}")

        async def execute(self, query, *args):
            normalized = " ".join(query.split())
            sql_calls.append((normalized, args))
            if normalized == (
                "ALTER TABLE public.vault_app_installations RENAME TO "
                "akb_fixture_member_status_unavailable"
            ):
                assert relation_state == {"canonical": True, "renamed": False}
                relation_state.update(canonical=False, renamed=True)
                return "ALTER TABLE"
            if normalized == (
                "ALTER TABLE public.akb_fixture_member_status_unavailable "
                "RENAME TO vault_app_installations"
            ):
                assert relation_state == {"canonical": False, "renamed": True}
                relation_state.update(canonical=True, renamed=False)
                return "ALTER TABLE"
            raise AssertionError(f"unexpected fixture SQL: {normalized}")

        async def close(self):
            return None

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_args):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    async def fake_get_pool():
        return FakePool()

    async def allow_owner_access(*_args, **_kwargs):
        return None

    async def fake_connect(**kwargs):
        assert (
            kwargs["host"],
            kwargs["port"],
            kwargs["user"],
            kwargs["database"],
        ) == ("127.0.0.1", runtime.config.postgres_port, "akb", "akb")
        return FakeConnection()

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    monkeypatch.setattr(app_installation_service, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        app_installation_service, "check_vault_access", allow_owner_access
    )

    before = await app_installation_service.get_member_installation_active_status(
        app_id, vault_id, user=user, correlation_id="before-fault"
    )

    invalid_target = await runtime.fixture_control(
        "fault_injection", "another_fixture", True, "member_installation_unavailable"
    )
    invalid_kind = await runtime.fixture_control(
        "fault_injection", "status_active", True, "missing_owned_table"
    )
    enabled = await runtime.fixture_control(
        "fault_injection", "status_active", True, "member_installation_unavailable"
    )
    with pytest.raises(AKBError) as unavailable:
        await app_installation_service.get_member_installation_active_status(
            app_id, vault_id, user=user, correlation_id="during-fault"
        )
    disabled = await runtime.fixture_control(
        "fault_injection", "status_active", False, "member_installation_unavailable"
    )
    after = await app_installation_service.get_member_installation_active_status(
        app_id, vault_id, user=user, correlation_id="after-fault"
    )
    other_scenario = E2ERuntime(make_config(tmp_path / "other"))
    other_scenario._prepared = True
    other_scenario._lifecycle_state = "ready"
    rejected_scenario = await other_scenario.fixture_control(
        "fault_injection", "status_active", True, "member_installation_unavailable"
    )

    assert invalid_target["reason"] == "unsupported_target"
    assert invalid_kind["reason"] == "unsupported_kind"
    assert before == {"active": True}
    assert enabled["status"] == "accepted"
    assert enabled["enabled"] is True
    assert enabled["observed"]["fault_injection"] == {
        "target": "status_active",
        "kind": "member_installation_unavailable",
    }
    assert unavailable.value.status_code == 503
    assert unavailable.value.code == "member_installation_status_unavailable"
    assert after == {"active": True}
    assert relation_state == {"canonical": True, "renamed": False}
    assert disabled["status"] == "accepted"
    assert disabled["enabled"] is False
    assert disabled["observed"]["fault_injection"] is None
    assert rejected_scenario["reason"] == "unsupported_scenario"
    assert sum(query.startswith("ALTER TABLE") for query, _args in sql_calls) == 2


def test_installation_discovery_publishes_success_lifecycle_command_bodies(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path), scenario="app-installation-lifecycle"
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-installation-lifecycle",
        "commands": {
            "restore_compatible": {
                "app_id": "target-app",
                "vault_id": "restore-vault",
                "release_id": "release-primary",
                "capabilities": ["installation:read", "inventory:read"],
                "mode": "restore",
                "request": {
                    "service": "app",
                    "method": "PUT",
                    "path": "/api/v1/apps/target-app/installations/restore-vault",
                    "body": {
                        "release_id": "release-primary",
                        "capabilities": ["installation:read", "inventory:read"],
                        "mode": "restore",
                    },
                },
            },
            "fresh_empty": {
                "app_id": "target-app",
                "vault_id": "fresh-vault",
                "release_id": "release-next",
                "capabilities": ["installation:read", "inventory:read"],
                "mode": "fresh",
                "request": {
                    "service": "app",
                    "method": "PUT",
                    "path": "/api/v1/apps/target-app/installations/fresh-vault",
                    "body": {
                        "release_id": "release-next",
                        "capabilities": ["installation:read", "inventory:read"],
                        "mode": "fresh",
                    },
                },
            },
        },
    }

    discovery = runtime.fixture_discovery()

    assert discovery["commands"] == runtime._fixture_catalog["commands"]


def test_scenario_argument_supports_the_lifecycle_fixture():
    config = _parse_args(["serve", "--scenario", "empty"])
    assert config.scenario == "empty"
    lifecycle = _parse_args(["serve", "--scenario", "app-installation-lifecycle"])
    assert lifecycle.scenario == "app-installation-lifecycle"
    rollout = _parse_args(["serve", "--scenario", "app-release-rollout"])
    assert rollout.scenario == "app-release-rollout"
    control_plane = _parse_args(["serve", "--scenario", "app-control-plane"])
    assert control_plane.scenario == "app-control-plane"
    with pytest.raises(SystemExit):
        _parse_args(["serve", "--scenario", "project"])


def test_app_control_plane_descriptor_keeps_schema_v2_discovery_contract(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), scenario="app-control-plane")
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-control-plane",
        "namespace": "fixture-randomized",
        "installations": [{"fixture_id": "target-00", "id": "installation-randomized"}],
        "coordinates": {
            "admin": {
                "registry": {"app_create": {"path": "/api/v1/apps"}},
                "resume": {"method": "POST"},
            },
            "self_app": {
                "resume": {"path": "/api/v1/app/rollouts/{rollout_id}/resume"},
            },
        },
    }
    descriptor = runtime.descriptor()
    discovery = runtime.fixture_discovery()
    assert descriptor["schema_version"] == 2
    assert descriptor["scenario"] == "app-control-plane"
    assert descriptor["services"]["fixture"]["reset"]["body"] == {
        "scenario": "app-control-plane"
    }
    assert discovery["scenario"] == "app-control-plane"
    assert discovery["controls"]["fault_injection"]["path"] == "/control"
    assert discovery["coordinates"]["admin"]["registry"]["app_create"]["path"] == "/api/v1/apps"
    assert discovery["coordinates"]["admin"]["resume"]["method"] == "POST"
    assert discovery["coordinates"]["self_app"]["resume"]["path"] == "/api/v1/app/rollouts/{rollout_id}/resume"


def test_raw_checkout_uses_explicit_source_revision_in_descriptor_and_discovery(tmp_path, monkeypatch):
    raw_checkout = tmp_path / "raw-checkout"
    raw_checkout.mkdir()
    revision = "c" * 40
    monkeypatch.setenv(SOURCE_REVISION_ENV, revision)
    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path),
            checkout=raw_checkout,
            profile="transport-proxy",
            scenario="app-control-plane",
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"

    descriptor = runtime.descriptor()
    discovery = runtime.fixture_discovery()

    assert descriptor["evidence"]["source_revision"] == revision
    assert discovery["runtime"]["source_revision"] == revision
    assert SOURCE_REVISION_ENV not in json.dumps(descriptor)
    assert SOURCE_REVISION_ENV not in json.dumps(discovery)


@pytest.mark.parametrize("value", ["", "not-a-sha", "a" * 39, "g" * 40, "a" * 41])
def test_invalid_explicit_source_revision_fails_closed(value, tmp_path, monkeypatch):
    monkeypatch.setenv(SOURCE_REVISION_ENV, value)
    runtime = E2ERuntime(make_config(tmp_path))

    with pytest.raises(BlockedRuntimeConfig, match="blocked_runtime_config"):
        runtime._source_revision()


def test_raw_checkout_without_explicit_source_revision_fails_closed(tmp_path, monkeypatch):
    raw_checkout = tmp_path / "raw-checkout"
    raw_checkout.mkdir()
    monkeypatch.delenv(SOURCE_REVISION_ENV, raising=False)
    runtime = E2ERuntime(dataclasses.replace(make_config(tmp_path), checkout=raw_checkout))

    with pytest.raises(BlockedRuntimeConfig, match="blocked_runtime_config"):
        runtime._source_revision()


@pytest.mark.asyncio
async def test_raw_checkout_preparation_blocks_before_creating_resources(tmp_path, monkeypatch):
    raw_checkout = tmp_path / "raw-checkout"
    raw_checkout.mkdir()
    monkeypatch.delenv(SOURCE_REVISION_ENV, raising=False)
    runtime = E2ERuntime(dataclasses.replace(make_config(tmp_path), checkout=raw_checkout))
    monkeypatch.setattr(runtime, "_validate_checkout", lambda: None)
    monkeypatch.setattr(runtime, "_validate_profile", lambda: None)
    monkeypatch.setattr(
        e2e_runtime,
        "prepare_private_runtime_root",
        lambda _path: pytest.fail("raw source revision must block before runtime setup"),
    )

    with pytest.raises(BlockedRuntimeConfig, match="blocked_runtime_config"):
        await runtime.prepare()

    assert runtime._children == {}
    assert runtime._fixture_task is None


def test_git_checkout_remains_the_fallback_source_revision_authority(tmp_path, monkeypatch):
    monkeypatch.delenv(SOURCE_REVISION_ENV, raising=False)
    runtime = E2ERuntime(make_config(tmp_path))

    revision = runtime._source_revision()

    assert len(revision) == 40
    assert all(character in "0123456789abcdef" for character in revision)


def test_app_control_plane_discovery_exposes_legacy_adoption_target_and_drift_control(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), scenario="app-control-plane")
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-control-plane",
        "fixtures": {
            "legacy_adoption": {
                "fixture_id": "legacy-adoption",
                "vault_id": "vault-legacy",
                "before": {"row_count": 3},
                "after_adoption": {
                    "grant_generation": 0,
                    "observed_grant_generation": 0,
                },
                "after_initial_grant": {
                    "grant_generation": 1,
                    "observed_grant_generation": 1,
                },
                "after": {
                    "grant_generation": 1,
                    "observed_grant_generation": 1,
                    "desired_current_release_id": "release-next",
                },
            }
        },
    }

    discovery = runtime.fixture_discovery()
    control = discovery["controls"]["fault_injection"]
    assert "legacy_schema_drift" in control["kinds"]
    assert {
        "fixture_id": "legacy-adoption",
        "vault_id": "vault-legacy",
        "target_type": "legacy_adoption",
    } in control["targets"]
    assert discovery["fixtures"]["legacy_adoption"]["before"]["row_count"] == 3
    assert discovery["fixtures"]["legacy_adoption"]["after_adoption"]["grant_generation"] == 0
    assert discovery["fixtures"]["legacy_adoption"]["after_initial_grant"]["grant_generation"] == 1
    assert discovery["fixtures"]["legacy_adoption"]["after"]["desired_current_release_id"] == "release-next"


def test_legacy_noop_release_fixture_has_fresh_and_exact_source_plans():
    table = {
        "name": "legacy_orders",
        "columns": [
            {"name": "amount", "type": "numeric"},
            {"name": "state", "type": "text"},
        ],
        "unique_keys": [],
        "indexes": [],
    }
    fingerprint = _fixture_schema_fingerprint([table])
    manifest, checksum = _fixture_v2_manifest(
        app_key="fixture-legacy-target",
        version="6.0.0",
        tables=[table],
        transition_plans=[
            (
                "fresh",
                [
                    {
                        "id": "create_legacy_orders",
                        "phase": "expand",
                        "operation": "create_table",
                        "payload": {
                            "table": "legacy_orders",
                            "columns": table["columns"],
                            "unique_keys": [],
                            "indexes": [],
                        },
                    }
                ],
            ),
            (
                {
                    "release_version": "5.0.0",
                    "schema_fingerprint": fingerprint,
                },
                [],
            ),
        ],
    )

    assert len(checksum) == 64
    assert manifest["schema"]["fingerprint"] == fingerprint
    assert manifest["transition_plans"][1] == {
        "source": {
            "release_version": "5.0.0",
            "schema_fingerprint": fingerprint,
        },
        "steps": [],
    }


def test_suite_sql_uses_compose_psql_by_default_and_preserves_override(tmp_path, monkeypatch):
    runtime = E2ERuntime(make_config(tmp_path))
    assert runtime._child_environment()["PYTHONFAULTHANDLER"] == "1"
    monkeypatch.delenv("AKB_PG_EXEC", raising=False)
    child_env = runtime._child_environment()
    assert child_env["AKB_PG_EXEC"].endswith("exec -T postgres")
    assert child_env["AKB_E2E_POSTGRES_PORT"] == "15432"
    assert child_env["AKB_E2E_MINIO_PORT"] == "9000"

    monkeypatch.setenv("AKB_PG_EXEC", "custom-pg-command")
    assert runtime._child_environment()["AKB_PG_EXEC"] == "custom-pg-command"


def test_runtime_uses_selected_dependency_ports_consistently(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path),
            postgres_port=25432,
            minio_port=29000,
        )
    )
    prepare_private_runtime_root(runtime.config.runtime_root)

    runtime._write_config()

    child_env = runtime._child_environment()
    app_config = yaml.safe_load(
        (runtime.config.config_dir / "app.yaml").read_text(encoding="utf-8")
    )
    assert child_env["AKB_E2E_POSTGRES_PORT"] == "25432"
    assert child_env["AKB_E2E_MINIO_PORT"] == "29000"
    assert app_config["db_port"] == 25432
    assert app_config["s3_endpoint_url"] == "http://127.0.0.1:29000"
    assert app_config["s3_public_url"] == "http://127.0.0.1:29000"


def test_runtime_root_is_private_and_separate(tmp_path):
    root = tmp_path / "private-runtime"
    config_dir, logs_dir, state_dir, vault_dir = prepare_private_runtime_root(root)

    assert {config_dir, logs_dir, state_dir, vault_dir} <= set(root.rglob("*"))
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for directory in (config_dir, logs_dir, state_dir, vault_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_runtime_configures_local_auth_mode_keys_and_distinct_app_token_secret(tmp_path):
    runtime = E2ERuntime(make_config(tmp_path))
    prepare_private_runtime_root(runtime.config.runtime_root)

    runtime._write_config()

    app_config = yaml.safe_load(
        (runtime.config.config_dir / "app.yaml").read_text(encoding="utf-8")
    )
    secret_config = yaml.safe_load(
        (runtime.config.config_dir / "secret.yaml").read_text(encoding="utf-8")
    )
    assert app_config["document_revision_backend"] == "bare_git"
    assert app_config["git_storage_path"] == str(runtime.config.vault_dir)
    assert app_config["auth_mode"] == "local"
    assert app_config["jwt_algorithm"] == "RS256"
    assert Path(app_config["local_session_private_key_path"]).is_file()
    assert Path(app_config["local_session_jwks_path"]).is_file()
    assert secret_config["app_token_secret"]
    assert secret_config["app_token_secret"] != secret_config["system_hmac_secret"]


def test_suite_summary_uses_last_complete_line_and_fails_closed():
    output = "Results: 1 passed, 0 failed\nnoise\nResults: 3 passed, 1 failed\n"
    assert parse_assertion_summary(output) == (3, 1, "Results: 3 passed, 1 failed")
    assert parse_assertion_summary("Results: 5 passed") is None
    assert SuiteResult("suite.sh", 0, 0, 0, None, "").gate_failed
    assert SuiteResult("suite.sh", 0, 2, 0, "Results: 2 passed, 0 failed", "").gate_failed is False
    assert SuiteResult("suite.sh", -4, 0, 0, None, "").signal == 4
    assert len(CURATED_SUITES) == 23


def test_shell_e2e_manifest_classifies_every_suite_exactly_once():
    assert validate_suite_manifest(REPO_ROOT) == ()
    discovered = {
        path.name for path in (REPO_ROOT / "backend" / "tests").glob("*_e2e.sh")
    }
    deferred = {
        suite for group in DEFERRED_SUITE_GROUPS for suite in group.suites
    }
    assert discovered == set(CURATED_SUITES) | deferred
    assert set(CURATED_SUITES).isdisjoint(deferred)


def test_shell_e2e_manifest_reports_unclassified_overlap_and_missing(
    tmp_path, monkeypatch
):
    tests_dir = tmp_path / "backend" / "tests"
    tests_dir.mkdir(parents=True)
    for name in ("test_run_e2e.sh", "test_defer_e2e.sh", "test_new_e2e.sh"):
        (tests_dir / name).touch()

    monkeypatch.setattr(
        e2e_suite_runner,
        "CURATED_SUITES",
        ("test_run_e2e.sh", "test_overlap_e2e.sh"),
    )
    monkeypatch.setattr(
        e2e_suite_runner,
        "DEFERRED_SUITE_GROUPS",
        (
            DeferredSuiteGroup(
                reason="fixture",
                suites=("test_defer_e2e.sh", "test_overlap_e2e.sh"),
            ),
        ),
    )

    errors = validate_suite_manifest(tmp_path)
    assert any(
        "unclassified E2E suites" in error and "test_new_e2e.sh" in error
        for error in errors
    )
    assert any(
        "both curated and deferred" in error and "test_overlap_e2e.sh" in error
        for error in errors
    )
    assert any(
        "without a matching suite file" in error and "test_overlap_e2e.sh" in error
        for error in errors
    )


def test_gate_events_are_safe_and_shell_signal_aware(capsys):
    emit_gate_event(
        {
            "event": "suite_complete",
            "process": "suite_runner",
            "suite": "test_publications_e2e.sh",
            "returncode": -4,
            "passed": 0,
            "failed": 0,
            "summary": None,
            "signal": 4,
        }
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(EVENT_PREFIX)
    stderr_event = json.loads(captured.err[len(EVENT_PREFIX) :])
    assert stderr_event["suite"] == "test_publications_e2e.sh"
    assert stderr_event["signal"] == 4
    assert signal_from_returncode(-4) == 4
    assert shell_exit_code(-4) == 132
    assert "external-password-value" not in captured.err


def test_suite_runner_emits_suite_and_gate_events(monkeypatch, capsys):
    monkeypatch.setattr(e2e_suite_runner, "CURATED_SUITES", ("test_fake.sh",))
    monkeypatch.setattr(e2e_suite_runner, "check_suite_manifest", lambda _repo_root: 0)
    monkeypatch.setattr(
        e2e_suite_runner,
        "run_suite",
        lambda *_args, **_kwargs: SuiteResult(
            "test_fake.sh", 0, 2, 0, "Results: 2 passed, 0 failed", ""
        ),
    )
    assert e2e_suite_runner.run_curated(REPO_ROOT) == 0

    events = [
        json.loads(line[len(EVENT_PREFIX) :])
        for line in capsys.readouterr().err.splitlines()
        if line.startswith(EVENT_PREFIX)
    ]
    assert [event["event"] for event in events] == [
        "suite_start",
        "suite_complete",
        "gate_complete",
    ]
    assert events[1]["suite"] == "test_fake.sh"
    assert events[1]["summary"] == "Results: 2 passed, 0 failed"
    assert events[2]["passed"] == 2


@pytest.mark.asyncio
async def test_gate_child_stdout_is_private_and_stderr_is_inherited(tmp_path, capfd, monkeypatch):
    monkeypatch.setenv(SOURCE_REVISION_ENV, "d" * 40)
    checkout = tmp_path / "checkout"
    suite_path = checkout / "scripts" / "ci" / "e2e_suite_runner.py"
    suite_path.parent.mkdir(parents=True)
    suite_path.write_text(
        "import sys\n"
        "print('credential-looking suite output')\n"
        "print('AKB_E2E_GATE {\"event\":\"suite_complete\","
        "\"suite\":\"test_fake.sh\",\"returncode\":132}', file=sys.stderr)\n",
        encoding="utf-8",
    )
    mcp_pytest_path = checkout / "backend" / "tests" / "mcp_e2e"
    mcp_pytest_path.mkdir(parents=True)
    (mcp_pytest_path / "conftest.py").write_text(
        "def pytest_addoption(parser):\n"
        "    parser.addoption('--runtime-descriptor')\n",
        encoding="utf-8",
    )
    (mcp_pytest_path / "test_fake.py").write_text(
        "def test_fake():\n"
        "    pass\n",
        encoding="utf-8",
    )
    config = RuntimeConfig(
        checkout=checkout,
        runtime_root=tmp_path / "runtime",
        mode="gate",
        compose_file=COMPOSE_FILE,
        compose_project="akb-e2e-io-unit",
    )
    prepare_private_runtime_root(config.runtime_root)
    runtime = E2ERuntime(config)

    phase_called = []

    async def fake_publication_phase():
        phase_called.append(True)

    runtime._run_publication_budget_check = fake_publication_phase
    assert await runtime._run_gate() == 0
    assert phase_called == [True]

    captured = capfd.readouterr()
    gate_log = (config.logs_dir / "gate.log").read_text(encoding="utf-8")
    assert "credential-looking suite output" in gate_log
    assert "credential-looking suite output" not in captured.err
    assert '"event":"suite_complete"' in captured.err
    assert '"returncode":132' in captured.err


@pytest.mark.asyncio
async def test_process_termination_cleans_a_process_group():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        start_new_session=True,
    )
    await terminate_process(process, grace_seconds=0.2)
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_dependency_start_waits_for_compose_health_before_backend_boot(tmp_path, monkeypatch):
    runtime = E2ERuntime(make_config(tmp_path))
    compose_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    async def fake_compose(*arguments: str, **kwargs: object) -> int:
        compose_calls.append((arguments, kwargs))
        return 0

    async def fake_wait_tcp(*_args: object) -> None:
        return None

    async def fake_wait_http(*_args: object) -> bytes:
        return b""

    dependency_identity = {
        "services": {
            "postgres": {
                "container_id": "postgres-container",
                "network_ids": ["runtime-network"],
                "volume_names": ["runtime-postgres-volume"],
            },
            "minio": {
                "container_id": "minio-container",
                "network_ids": ["runtime-network"],
                "volume_names": ["runtime-minio-volume"],
            },
        }
    }
    identity_calls = 0

    def fake_dependency_identity_snapshot() -> dict[str, object]:
        nonlocal identity_calls
        identity_calls += 1
        return dependency_identity

    monkeypatch.setattr(runtime, "_compose", fake_compose)
    monkeypatch.setattr(runtime, "_wait_tcp", fake_wait_tcp)
    monkeypatch.setattr(runtime, "_wait_http", fake_wait_http)
    monkeypatch.setattr(runtime, "_ensure_minio_bucket", lambda: None)
    monkeypatch.setattr(runtime, "_dependency_identity_snapshot", fake_dependency_identity_snapshot)

    await runtime._start_dependencies()

    # Always --build: a reused project name must not run an image an earlier
    # checkout built from deploy/postgres.
    assert compose_calls == [(("up", "--build", "--detach", "--wait"), {})]
    assert identity_calls == 1
    assert runtime._dependency_identity == dependency_identity


class FakeFixtureRuntime:
    def __init__(self, scenario="empty"):
        self.reset_count = 0
        self.scenario = scenario
        self.oidc_fixture = None

    def fixture_health(self):
        return {"status": "ready", "scenario": self.scenario, "app_ready": True}

    def fixture_discovery(self):
        return {
            "status": "ready",
            "scenario": self.scenario,
            "fixtures": {},
            "access": {
                "login": {
                    "service": "app",
                    "method": "POST",
                    "path": "/api/v1/auth/login",
                    "fields": ["username", "password"],
                }
            },
            "observability": {
                "log_observation": {
                    "service": "fixture",
                    "method": "GET",
                    "path": "/log-observation",
                }
            },
        }

    def fixture_log_observation(self):
        return {
            "status": "ready",
            "scenario": self.scenario,
            "redacted": True,
            "redaction_scan": {"private_value_hits": 0, "raw_log_exposed": False},
        }

    async def fixture_control(self, action, target, enabled, kind=None):
        return {
            "status": "accepted",
            "scenario": self.scenario,
            "action": action,
            "enabled": enabled,
            "kind": kind,
        }

    async def reset_scenario(self):
        self.reset_count += 1


@pytest.mark.asyncio
async def test_fixture_control_exposes_reset_discovery_and_sanitized_logs():
    runtime = FakeFixtureRuntime()
    app = create_app(runtime)
    paths = {route.path for route in app.routes}
    assert {
        "/health",
        "/reset",
        "/discover",
        "/log-observation",
        "/control",
        "/openapi.json",
    } <= paths
    assert "/stop" not in paths

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.test"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        discovery = await client.get("/discover")
        assert discovery.json()["fixtures"] == {}
        assert discovery.json()["access"]["login"] == {
            "service": "app",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "fields": ["username", "password"],
        }
        assert discovery.json()["observability"]["log_observation"] == {
            "service": "fixture",
            "method": "GET",
            "path": "/log-observation",
        }
        log_observation = await client.get("/log-observation")
        assert log_observation.json()["redaction_scan"]["private_value_hits"] == 0
        reset = await client.post("/reset", json={"scenario": "empty"})
        assert reset.status_code == 200
        assert runtime.reset_count == 1
        control = await client.post(
            "/control",
            json={"action": "restart", "enabled": True},
        )
        assert control.status_code == 200
        assert control.json()["scenario"] == "empty"
        lifecycle_runtime = FakeFixtureRuntime("app-installation-lifecycle")
        lifecycle_app = create_app(lifecycle_runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lifecycle_app),
            base_url="http://lifecycle-fixture.test",
        ) as lifecycle_client:
            lifecycle_reset = await lifecycle_client.post(
                "/reset", json={"scenario": "app-installation-lifecycle"}
            )
            member_control = await lifecycle_client.post(
                "/control",
                json={
                    "action": "member_access",
                    "target": "reader",
                    "kind": "status_active",
                    "enabled": False,
                },
            )
        assert lifecycle_reset.status_code == 200
        assert lifecycle_runtime.reset_count == 1
        assert member_control.status_code == 200
        unsupported = await client.post("/reset", json={"scenario": "project"})
        assert unsupported.status_code == 422
        openapi = await client.get("/openapi.json")
        assert openapi.status_code == 200
        assert "/reset" in openapi.json()["paths"]


@pytest.mark.asyncio
async def test_control_plane_reset_discovery_serializes_success_installation_commands(tmp_path):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-control-plane-discovery-unit",
            scenario="app-control-plane",
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    runtime._prepared = True
    app_id = "11111111-1111-4111-8111-111111111111"
    restore_vault_id = "22222222-2222-4222-8222-222222222222"
    fresh_vault_id = "33333333-3333-4333-8333-333333333333"
    restore_release_id = "44444444-4444-4444-8444-444444444444"
    fresh_release_id = "55555555-5555-4555-8555-555555555555"

    async def reset_scenario() -> None:
        runtime._fixture_catalog = {
            "status": "ready",
            "scenario": "app-control-plane",
        }
        runtime._publish_installation_lifecycle_commands(
            app_id=app_id,
            restore_vault_id=restore_vault_id,
            restore_release_id=restore_release_id,
            fresh_vault_id=fresh_vault_id,
            fresh_release_id=fresh_release_id,
        )

    runtime.reset_scenario = reset_scenario  # type: ignore[method-assign]
    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control-plane-fixture.test"
    ) as client:
        reset = await client.post("/reset", json={"scenario": "app-control-plane"})
        assert reset.status_code == 200
        discovery = await client.get("/discover")

    commands = discovery.json()["commands"]
    assert commands["restore_compatible"] == {
        "app_id": app_id,
        "vault_id": restore_vault_id,
        "release_id": restore_release_id,
        "capabilities": ["installation:read", "inventory:read"],
        "mode": "restore",
        "request": {
            "service": "app",
            "method": "PUT",
            "path": f"/api/v1/apps/{app_id}/installations/{restore_vault_id}",
            "body": {
                "release_id": restore_release_id,
                "capabilities": ["installation:read", "inventory:read"],
                "mode": "restore",
            },
        },
    }
    assert commands["fresh_empty"]["mode"] == "fresh"
    assert commands["fresh_empty"]["request"]["body"] == {
        "release_id": fresh_release_id,
        "capabilities": ["installation:read", "inventory:read"],
        "mode": "fresh",
    }


class _LifecycleProcess:
    _next_pid = 900_000

    def __init__(self) -> None:
        self.pid = type(self)._next_pid
        type(self)._next_pid += 1
        self.release = asyncio.Event()
        self.returncode: int | None = None

    async def wait(self) -> int:
        await self.release.wait()
        self.returncode = 0
        return 0


@pytest.mark.asyncio
async def test_serve_ignores_process_exit_after_reset_replaces_the_process(tmp_path):
    runtime = E2ERuntime(make_config(tmp_path))
    old_process = _LifecycleProcess()
    new_process = _LifecycleProcess()
    runtime._children["backend"] = ManagedProcess(old_process)  # type: ignore[arg-type]

    serve_task = asyncio.create_task(runtime._serve_foreground())
    await asyncio.sleep(0)

    runtime._resetting = True
    old_process.release.set()
    runtime._children["backend"] = ManagedProcess(new_process)  # type: ignore[arg-type]
    runtime._resetting = False
    for _ in range(3):
        await asyncio.sleep(0)

    assert not serve_task.done()
    runtime.request_stop()
    assert await asyncio.wait_for(serve_task, timeout=1) == 0
    assert runtime._children["backend"].process is new_process


def test_rollout_fixture_discovery_is_generic_and_redacted(tmp_path):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-rollout-unit",
            scenario="app-release-rollout",
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    descriptor = runtime.descriptor()
    assert descriptor["schema_version"] == 2
    assert descriptor["status"] == "ready"
    assert descriptor["scenario"] == "app-release-rollout"
    runtime._fixture_private_values = ("private-credential", "private-marker")
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-release-rollout",
        "namespace": "fixture-randomized",
        "apps": {"target": {"id": "target-app"}, "foreign": {"id": "foreign-app"}},
        "installations": [{"fixture_id": "target-00", "id": "target-installation"}],
        "coordinates": {
            "admin": {"request": {"method": "POST", "path": "/api/v1/apps/{app_id}/rollouts"}},
            "self_app": {"request": {"method": "POST", "path": "/api/v1/app/rollouts"}},
        },
        "controls": {"fault_injection": {"method": "POST", "path": "/control"}},
        "secret": "private-marker-suffix",  # pragma: allowlist secret
    }
    discovery = runtime.fixture_discovery()
    assert discovery["scenario"] == "app-release-rollout"
    assert len(discovery["installations"]) >= 1
    assert "private-marker" not in json.dumps(discovery)
    # Fixture discovery must never include runtime values, even if test setup
    # accidentally placed one in its private catalog.
    assert "private-credential" not in json.dumps(discovery)
    assert discovery["coordinates"]["admin"]["request"]["method"] == "POST"
    assert discovery["controls"]["fault_injection"]["body"]["kind"] == "missing_owned_table"


def test_rollout_discovery_advertises_same_coordinates_for_each_app(tmp_path):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-rollout-app-coordinates-unit",
            scenario="app-release-rollout",
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-release-rollout",
        "apps": {
            "target": {
                "id": "target-app",
                "rollout": {
                    "release_id": "target-release",
                    "manifest_checksum": "a" * 64,
                    "request": {"method": "POST", "path": "/api/v1/apps/target-app/rollouts"},
                    "status": {"method": "GET", "path": "/api/v1/apps/target-app/rollouts/{rollout_id}"},
                },
            },
            "foreign": {
                "id": "foreign-app",
                "rollout": {
                    "release_id": "foreign-release",
                    "manifest_checksum": "b" * 64,
                    "request": {"method": "POST", "path": "/api/v1/apps/foreign-app/rollouts"},
                    "status": {"method": "GET", "path": "/api/v1/apps/foreign-app/rollouts/{rollout_id}"},
                },
            },
        },
    }

    discovery = runtime.fixture_discovery()
    apps = discovery["apps"]
    assert set(apps) == {"target", "foreign"}
    assert {item["rollout"]["request"]["method"] for item in apps.values()} == {"POST"}
    assert {item["rollout"]["status"]["method"] for item in apps.values()} == {"GET"}
    assert {item["rollout"]["release_id"] for item in apps.values()} == {
        "target-release",
        "foreign-release",
    }


def test_rollout_fault_control_discovery_uses_fixture_ids_and_kinds(tmp_path):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-rollout-control-unit",
            scenario="app-release-rollout",
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-release-rollout",
        "apps": {"target": {"id": "target-app"}},
        "installations": [
            {"fixture_id": f"target-{index:02d}", "id": f"installation-{index}"}
            for index in range(13)
        ],
    }

    discovery = runtime.fixture_discovery()
    fault = discovery["controls"]["fault_injection"]
    assert fault["kinds"] == ["missing_owned_table"]
    assert fault["body"] == {
        "action": "fault_injection",
        "kind": "missing_owned_table",
        "target": "target-00",
        "enabled": True,
    }
    assert {item["fixture_id"] for item in fault["targets"]} == {
        f"target-{index:02d}" for index in range(13)
    }
    assert runtime._resolve_fault_target("target-00")["id"] == "installation-0"
    assert runtime._resolve_fault_target("target-11")["id"] == "installation-11"
    with pytest.raises(ValueError):
        runtime._resolve_fault_target("target-13")


@pytest.mark.asyncio
async def test_rollout_fault_control_applies_before_ack(tmp_path, monkeypatch):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-rollout-control-ack-unit",
            scenario="app-release-rollout",
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-release-rollout",
        "installations": [
            {
                "fixture_id": f"target-{index:02d}",
                "id": f"installation-{index}",
                "vault_id": f"vault-{index}",
            }
            for index in range(13)
        ],
    }
    applied: list[tuple[str, str]] = []

    async def record_application(fixture: dict[str, object], kind: str) -> bool:
        applied.append((str(fixture["fixture_id"]), kind))
        return True

    monkeypatch.setattr(runtime, "_apply_fault", record_application)
    result = await runtime.fixture_control(
        "fault_injection", "target-11", True, "missing_owned_table"
    )

    assert result["status"] == "accepted"
    assert applied == [("target-11", "missing_owned_table")]
    assert runtime._fixture_controls["fault"] == {
        "target": "target-11",
        "kind": "missing_owned_table",
    }


@pytest.mark.asyncio
async def test_rollout_fault_disable_restores_fixture_before_ack(tmp_path, monkeypatch):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-rollout-control-recovery-unit",
            scenario="app-release-rollout",
        )
    )
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-release-rollout",
        "installations": [
            {
                "fixture_id": "target-00",
                "id": "installation-0",
                "vault_id": "vault-0",
            }
        ],
    }
    runtime._fixture_controls["fault"] = {
        "target": "target-00",
        "kind": "missing_owned_table",
    }
    restored: list[tuple[str, str]] = []

    async def record_restore(fixture: dict[str, object], kind: str) -> bool:
        restored.append((str(fixture["fixture_id"]), kind))
        return True

    monkeypatch.setattr(runtime, "_restore_fault", record_restore)
    result = await runtime.fixture_control(
        "fault_injection", "target-00", False, "missing_owned_table"
    )

    assert result["status"] == "accepted"
    assert result["enabled"] is False
    assert restored == [("target-00", "missing_owned_table")]
    assert result["observed"]["fault_injection"] is None


def test_compose_and_hosted_workflow_preserve_the_live_topology():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    assert set(compose["services"]) == {"postgres", "minio"}
    # Asserted as "the pg16 tag, carrying a digest" rather than as one exact
    # string. A bare tag here would let the e2e runtime drift to whatever
    # `pg16` resolves to on the day while every deployment manifest is pinned,
    # which is the disagreement the pinning was meant to remove. Repeating the
    # digest instead would make this a second place to edit on every bump, and
    # the two copies would fall out of step the first time someone forgot.
    # The runtime builds the install paths' PostgreSQL, so the e2e runs the
    # default route: a new database that gets the `vchord` sparse shape. Its base
    # and extension pins live in that Dockerfile and are held by
    # test_container_image_pins_unit.py, not repeated here.
    postgres = compose["services"]["postgres"]
    assert "image" not in postgres, postgres
    assert (CI_DIR / postgres["build"]["context"]).resolve() == (CI_DIR.parents[1] / "deploy/postgres").resolve()
    # The IBM mirror keeps the upstream artifact pinned by digest.
    minio_image = compose["services"]["minio"]["image"]
    assert minio_image.startswith("icr.io/fusion-open/minio/minio@sha256:"), minio_image
    assert "@sha256:" in minio_image, minio_image
    assert compose["services"]["postgres"]["ports"] == [
        "${AKB_E2E_POSTGRES_PORT:-15432}:5432"
    ]
    assert compose["services"]["minio"]["ports"] == [
        "${AKB_E2E_MINIO_PORT:-9000}:9000"
    ]

    workflow = WORKFLOW.read_text()
    assert "scripts/ci/e2e_runtime.py gate" in workflow
    assert "--scenario empty" in workflow
    assert "app-installation-lifecycle" in (CI_DIR / "e2e_runtime.py").read_text()
    assert "uv sync --locked --extra dev --project backend" in workflow
    assert "services:" not in workflow
    for suite in CURATED_SUITES:
        assert suite not in workflow
    assert "akb-e2e-runtime-logs" in workflow

    local_runner = LOCAL_CANONICAL_RUNNER.read_text()
    assert "scripts/ci/e2e_suite_runner.py" in local_runner
    assert "SUITES=(" not in local_runner

def test_ubuntu_bootstrap_is_bash_safe_and_keeps_descriptor_stdout_clean():
    result = subprocess.run(["bash", "-n", str(BOOTSTRAP)], check=False)
    assert result.returncode == 0
    text = BOOTSTRAP.read_text()
    lines = text.splitlines()
    ubuntu_check = next(index for index, line in enumerate(lines) if "Ubuntu 24.04 is required" in line)
    sysctl_persist = next(index for index, line in enumerate(lines) if "99-akb-e2e-tcp-mtu-probing.conf" in line)
    apt_update = next(index for index, line in enumerate(lines) if '"${SUDO[@]}" apt-get update' in line)
    assert "exec 3>&1 1>&2" in text
    assert ubuntu_check < sysctl_persist < apt_update
    assert "net.ipv4.tcp_mtu_probing = 2" in text
    assert 'sysctl --load "$SYSCTL_CONF"' in text
    assert '[ "$SYSCTL_VALUE" = "2" ]' in text
    assert "TCP MTU probing configuration" in text
    assert "--scenario empty" in text
    assert "app-installation-lifecycle" in text
    assert "--with-frontend" in text
    assert "pnpm install --frozen-lockfile" in text
    assert "FRONTEND_PACKAGE_MANAGER" in text
    assert "FRONTEND_NODE_VERSION" in text
    assert '"node@$FRONTEND_NODE_VERSION" "pnpm@$FRONTEND_PNPM_VERSION"' in text
    assert "command -v pnpm" in text
    assert 'apt-get install -y nodejs npm' in text
    assert 'command -v node' in text
    assert 'command -v npm' in text
    assert "Node.js/npm package installation failed" in text
    assert "uv sync --locked" in text
    assert "exec 1>&3 3>&-" in text
    assert stat.S_IMODE(BOOTSTRAP.stat().st_mode) & stat.S_IXUSR


def test_capability_profiles_are_explicit_and_composable():
    assert select_capability_profile("tool-only") == CapabilityProfile(
        "tool-only", frozenset({"http", "pat"})
    )
    assert select_capability_profile("transport-proxy").needs_stdio
    assert select_capability_profile("oidc-resource-server").needs_oidc
    combined = select_capability_profile("tool-only", ("stdio", "oidc"))
    assert combined.name == "transport-oidc"
    assert combined.capabilities == frozenset({"http", "pat", "stdio", "oidc"})


def test_tool_only_does_not_advertise_optional_processes(tmp_path):
    runtime = E2ERuntime(make_config(tmp_path))
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    descriptor = runtime.descriptor()
    assert set(descriptor["services"]) == {"app", "fixture"}
    assert "profile" not in descriptor
    discovery = runtime.fixture_discovery()
    assert discovery["runtime"]["selected_capabilities"] == ["http", "pat"]
    assert "stdio" not in discovery["runtime"]
    assert "oidc" not in discovery["runtime"]


def test_transport_profile_exposes_real_proxy_boundary_without_secret(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), profile="transport-proxy")
    )
    runtime._pat_value = "akb_runtime_secret"  # pragma: allowlist secret
    descriptor = runtime.descriptor()
    assert descriptor["services"]["stdio"]["transport"] == "stdio"
    assert descriptor["credentials"]["pat_env"] == "AKB_E2E_PAT"
    serialized = json.dumps(descriptor)
    assert "akb_runtime_secret" not in serialized
    assert "AKB_E2E_PAT" in serialized
    assert descriptor["evidence"]["transport"] == ["http", "stdio"]


@pytest.mark.asyncio
async def test_oidc_profile_serves_jwks_metadata_and_deterministic_variants(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), profile="oidc-resource-server")
    )
    app = create_app(runtime)
    jwks_path = runtime.oidc_fixture.jwks_uri.removeprefix(runtime.config.fixture_origin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=runtime.config.fixture_origin
    ) as client:
        health = await client.get("/oidc/health")
        metadata_path = runtime.oidc_fixture.metadata_uri.removeprefix(runtime.config.fixture_origin)
        metadata = await client.get(metadata_path)
        jwks = await client.get(jwks_path)
        token = await client.post("/oidc/token", json={"variant": "valid"})
        bad = await client.post("/oidc/token", json={"variant": "wrong_issuer"})
    assert health.status_code == 200
    assert metadata.json()["issuer"] == runtime.oidc_fixture.issuer
    assert jwks.json()["keys"][0]["alg"] == "RS256"
    assert token.status_code == 200 and token.json()["token_type"] == "Bearer"
    assert bad.status_code == 200 and bad.json()["access_token"] != token.json()["access_token"]
    assert metadata.json()["scopes_supported"] == ["akb:vault:read", "akb:vault:write"]
    assert set(runtime.oidc_fixture.discovery()["token_variants"]) == {
        "valid",
        "wrong_issuer",
        "wrong_audience",
        "expired",
        "wrong_algorithm",
        "wrong_key_id",
        "insufficient_scope",
    }
    assert "challenge_cases" in runtime.oidc_fixture.discovery()
    assert "access_token" not in json.dumps(runtime.fixture_discovery())


def test_optional_capabilities_fail_closed_without_silent_skip(tmp_path, monkeypatch):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), profile="keycloak-overlay")
    )
    with pytest.raises(BlockedRuntimeConfig, match="blocked_runtime_config"):
        runtime._validate_profile()

    transport = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), profile="transport-proxy")
    )
    monkeypatch.setattr(
        "e2e_runtime.shutil.which",
        lambda name: None if name == "node" else "/usr/bin/npm",
    )
    with pytest.raises(BlockedRuntimeConfig, match="stdio capability"):
        transport._validate_profile()


def _ready_runtime(tmp_path):
    runtime = E2ERuntime(make_config(tmp_path))
    runtime._prepared = True
    runtime._lifecycle_state = "ready"
    runtime._children["backend"] = ManagedProcess(_LifecycleProcess())
    runtime._fixture_catalog = {"status": "ready", "private_old_fixture": "old-generation"}
    return runtime


def _stub_reuse_reset(monkeypatch, runtime, *, fail_at=None, block_at=None):
    """Replace external I/O while exercising admission, ordering and failure state."""
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()
    identity = {"services": {"postgres": "same-postgres", "minio": "same-minio"}}
    runtime._dependency_identity = identity
    embed = ManagedProcess(_LifecycleProcess())
    runtime._children["embed"] = embed

    async def operation(name):
        calls.append(name)
        assert not runtime._prepared
        assert runtime.fixture_health()["status"] != "ready"
        assert runtime.fixture_discovery()["status"] != "ready"
        if name == block_at:
            entered.set()
            await release.wait()
        if name == fail_at:
            raise ProvisioningFailure(f"injected failure: {name}")

    async def stop(name):
        assert name == "stdio", "reset must preserve the embedding process"
        await operation(f"stop:{name}")
        runtime._children.pop(name, None)

    async def drain():
        await operation("drain")
        runtime._children.pop("backend", None)

    async def reset_postgres():
        assert "backend" not in runtime._children
        await operation("postgres")

    async def seed():
        assert "backend" not in runtime._children
        await operation("seed")
        runtime._fixture_catalog = {"status": "ready", "fixture_generation": runtime.config.storage_generation}

    async def start_backend():
        await operation("backend")
        runtime._children["backend"] = ManagedProcess(_LifecycleProcess())

    async def mint_pat():
        await operation("pat")
        runtime._pat_value = f"new-token-{runtime.config.storage_generation}"

    def ensure_bucket():
        calls.append("bucket")
        if fail_at == "bucket":
            raise ProvisioningFailure("injected failure: bucket")

    async def forbidden(*args, **kwargs):
        pytest.fail("reset must reuse running dependencies instead of rebuilding them")

    monkeypatch.setattr(runtime, "_stop_named_process", stop)
    monkeypatch.setattr(runtime, "_drain_backend", drain)
    monkeypatch.setattr(runtime, "_reset_postgres_offline", reset_postgres)
    monkeypatch.setattr(runtime, "_write_config", lambda: calls.append("config"))
    monkeypatch.setattr(runtime, "_ensure_minio_bucket", ensure_bucket)
    monkeypatch.setattr(runtime, "_seed_external_credential", seed)
    monkeypatch.setattr(runtime, "_start_backend", start_backend)
    monkeypatch.setattr(runtime, "_mint_runtime_pat", mint_pat)
    monkeypatch.setattr(runtime, "_start_stdio_proxy", lambda: operation("stdio"))
    monkeypatch.setattr(runtime, "_compose", forbidden)
    monkeypatch.setattr(runtime, "_start_dependencies", forbidden)
    monkeypatch.setattr(runtime, "_start_embed_stub", forbidden)
    monkeypatch.setattr(runtime, "_bootstrap_backend_and_seed", forbidden)
    monkeypatch.setattr(runtime, "_dependency_identity_snapshot", lambda: identity)
    monkeypatch.setattr(runtime, "_process_identity_snapshot", lambda: {})
    return calls, entered, release, embed


@pytest.mark.asyncio
async def test_reset_reuses_dependencies_only_after_drain_and_publishes_new_generation(tmp_path, monkeypatch):
    runtime = _ready_runtime(tmp_path)
    old_git_path = runtime.config.vault_dir
    old_bucket = runtime.config.s3_bucket
    old_git_path.mkdir(parents=True)
    runtime._fixture_controls["fault"] = {"kind": "member_installation_unavailable"}
    runtime._pat_value = "old-token"
    calls, _, _, embed = _stub_reuse_reset(monkeypatch, runtime)

    await runtime.reset_scenario()

    assert calls[:3] == ["stop:stdio", "drain", "postgres"]
    assert calls.index("postgres") < calls.index("config") < calls.index("bucket") < calls.index("seed")
    assert calls.index("seed") < calls.index("backend") < calls.index("pat")
    assert runtime._children["embed"] is embed
    assert runtime.config.storage_generation == 1
    assert runtime.config.vault_dir != old_git_path
    assert runtime.config.s3_bucket != old_bucket
    # A delayed write keeps addressing the retired generation's storage.
    old_git_path.mkdir(parents=True, exist_ok=True)
    (old_git_path / "late-old-write").write_text("old data")
    assert not (runtime.config.vault_dir / "late-old-write").exists()
    assert runtime._fixture_controls == {}
    assert runtime._pat_value == "new-token-1"
    assert runtime._prepared
    assert runtime.fixture_health()["status"] == "ready"
    assert runtime.fixture_discovery()["fixture_generation"] == 1
    evidence = runtime._dependency_reset_evidence
    assert evidence["strategy"] == "reuse_after_drain"
    assert evidence["identity_before"] == evidence["identity_after"]
    assert evidence["last_succeeded"] is True
    assert set(evidence["phase_seconds"]) == {
        "drain_backend", "reset_postgres", "prepare_fixture", "start_backend", "start_stdio",
    }


@pytest.mark.asyncio
async def test_concurrent_resets_are_serialized(tmp_path, monkeypatch):
    runtime = _ready_runtime(tmp_path)
    calls, entered, release, _ = _stub_reuse_reset(monkeypatch, runtime, block_at="seed")
    first = asyncio.create_task(runtime.reset_scenario())
    await entered.wait()
    second = asyncio.create_task(runtime.reset_scenario())
    await asyncio.sleep(0)
    assert calls.count("postgres") == 1
    release.set()
    await asyncio.gather(first, second)
    assert calls.count("postgres") == 2
    assert runtime.config.storage_generation == 2
    assert runtime._dependency_reset_evidence["count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["drain", "postgres", "bucket", "seed", "backend", "pat", "stdio"])
async def test_reset_failure_never_republishes_ready_or_allows_another_reset(tmp_path, monkeypatch, fail_at):
    runtime = _ready_runtime(tmp_path)
    runtime.profile = dataclasses.replace(runtime.profile, capabilities=runtime.profile.capabilities | {"stdio"})
    calls, _, _, _ = _stub_reuse_reset(monkeypatch, runtime, fail_at=fail_at)
    with pytest.raises(ProvisioningFailure, match="injected failure"):
        await runtime.reset_scenario()
    assert runtime._lifecycle_state == "failed"
    assert not runtime._prepared
    assert runtime.fixture_health()["status"] == "failed"
    assert runtime.fixture_discovery()["status"] != "ready"
    assert runtime._dependency_reset_evidence["last_succeeded"] is False
    if fail_at == "drain":
        assert "postgres" not in calls, "uncertified shutdown must prevent data deletion"
        assert "config" not in calls
    with pytest.raises(ProvisioningFailure):
        await runtime.reset_scenario()
    result = await runtime.fixture_control("restart", None, True)
    assert result["status"] == "rejected"


@pytest.mark.asyncio
async def test_dependency_replacement_during_reset_cannot_publish_ready(tmp_path, monkeypatch):
    runtime = _ready_runtime(tmp_path)
    _stub_reuse_reset(monkeypatch, runtime)
    seed = runtime._seed_external_credential

    async def seed_after_dependency_changed():
        await seed()
        monkeypatch.setattr(runtime, "_dependency_identity_snapshot", lambda: {
            "services": {"postgres": "unexpected-postgres", "minio": "same-minio"},
        })

    monkeypatch.setattr(runtime, "_seed_external_credential", seed_after_dependency_changed)
    with pytest.raises(ProvisioningFailure):
        await runtime.reset_scenario()
    assert runtime._lifecycle_state == "failed"
    assert not runtime._prepared
    assert runtime.fixture_health()["status"] == "failed"
    assert runtime.fixture_discovery()["status"] != "ready"


@pytest.mark.asyncio
async def test_cancelled_reset_stays_failed(tmp_path, monkeypatch):
    runtime = _ready_runtime(tmp_path)
    _, entered, _, _ = _stub_reuse_reset(monkeypatch, runtime, block_at="seed")
    task = asyncio.create_task(runtime.reset_scenario())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime._lifecycle_state == "failed"
    assert not runtime._prepared
    assert runtime.fixture_health()["status"] == "failed"


@pytest.mark.asyncio
async def test_control_queued_before_generation_change_cannot_mutate_new_fixture(tmp_path):
    runtime = _ready_runtime(tmp_path)
    async with runtime._reset_lock:
        control = asyncio.create_task(runtime.fixture_control("restart", None, False))
        await asyncio.sleep(0)
        assert not control.done()
        runtime._lifecycle_generation += 1
    result = await control
    assert result["status"] == "rejected"
    assert runtime._fixture_controls == {}


@pytest.mark.asyncio
async def test_failed_process_stop_retains_process_for_cleanup(tmp_path, monkeypatch):
    runtime = _ready_runtime(tmp_path)
    original = runtime._children["backend"]

    async def fail(*args, **kwargs):
        raise ProvisioningFailure("termination failed")

    monkeypatch.setattr(e2e_runtime, "terminate_process", fail)
    with pytest.raises(ProvisioningFailure, match="termination failed"):
        await runtime._stop_named_process("backend")
    assert runtime._children["backend"] is original


@pytest.mark.asyncio
@pytest.mark.parametrize("certificate", [
    "valid", "missing", "malformed", "stale_launch", "wrong_pid",
    "incomplete", "truthy_acknowledgement", "unclean_exit", "already_exited",
])
async def test_backend_drain_requires_matching_clean_shutdown_certificate(tmp_path, monkeypatch, certificate):
    runtime = _ready_runtime(tmp_path)

    class GracefulProcess:
        pid = 654321
        returncode = 0 if certificate == "already_exited" else None

        def send_signal(self, signum):
            assert signum == e2e_runtime.signal.SIGTERM
            self.returncode = 1 if certificate == "unclean_exit" else 0

        async def wait(self):
            return self.returncode

    managed = ManagedProcess(GracefulProcess())
    runtime._children["backend"] = managed
    runtime._backend_launch_id = "current-launch"
    runtime._backend_drain_report = tmp_path / "drain.json"
    report = {
        "launch_id": "current-launch", "pid": GracefulProcess.pid,
        "shutdown_complete": True,
        "launcher_failed": False, "lifespan_failed": False, "forced_exit": False,
        "pending_tasks": 0, "http_tasks": 0, "connections": 0,
        "worker_loop_tasks": 0, "worker_inflight": 0, "worker_abandoned": 0,
        "runner_snapshot_failed": False,
    }
    if certificate == "stale_launch":
        report["launch_id"] = "previous-launch"
    elif certificate == "wrong_pid":
        report["pid"] += 1
    elif certificate == "incomplete":
        report["shutdown_complete"] = False
    elif certificate == "truthy_acknowledgement":
        report["shutdown_complete"] = "yes"
    if certificate == "malformed":
        runtime._backend_drain_report.write_text("not-json")
    elif certificate != "missing":
        runtime._backend_drain_report.write_text(json.dumps(report))
    monkeypatch.setattr(e2e_runtime, "_live_process_groups", lambda *args: set())

    if certificate == "valid":
        await runtime._drain_backend()
        assert "backend" not in runtime._children
    else:
        with pytest.raises(ProvisioningFailure):
            await runtime._drain_backend()
        assert runtime._children["backend"] is managed


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "missing_schema_table", "cleanup_error", "cleanup_close_error"])
async def test_offline_postgres_reset_fences_from_maintenance_database_and_rolls_back_failures(
    tmp_path, monkeypatch, outcome,
):
    import asyncpg

    runtime = E2ERuntime(make_config(tmp_path))
    monkeypatch.setenv("TEST_USERNAME_ENV", "fixture-user")
    monkeypatch.setenv("TEST_PASSWORD_ENV", "fixture-password")
    runtime._schema_tables = {
        ("public", name) for name in (
            "schema_migrations", "users", "tokens", "chunks", "bm25_stats", "bm25_vocab_epoch",
        )
    }
    current = runtime._schema_tables | {("public", "scenario_table")}
    if outcome == "missing_schema_table":
        current.remove(("public", "chunks"))
    events = []
    allowed = True
    pending_writes = []
    committed_writes = []
    in_transaction = False
    waiting_clients = iter((True, False))

    class Transaction:
        async def __aenter__(self):
            nonlocal in_transaction
            assert not allowed
            in_transaction = True
            events.append(("transaction", "begin"))

        async def __aexit__(self, exc_type, _exc, _traceback):
            nonlocal in_transaction
            in_transaction = False
            if exc_type is None:
                events.append(("transaction", "commit"))
                committed_writes.extend(pending_writes)
            else:
                events.append(("transaction", "rollback"))
            pending_writes.clear()
            return False

    class CleanupConnection:
        closed = False

        def transaction(self):
            return Transaction()

        async def fetchval(self, query):
            assert not allowed
            assert "pg_stat_activity" in query and "client backend" in query
            events.append(("cleanup", "wait_clients"))
            return next(waiting_clients)

        async def fetch(self, query):
            assert in_transaction
            assert "pg_tables" in query
            return [{"schemaname": schema, "tablename": table} for schema, table in current]

        async def execute(self, query, *_args):
            normalized = " ".join(query.split())
            assert not normalized.startswith("ALTER DATABASE"), "admission DDL must use maintenance DB"
            assert in_transaction, "all destructive cleanup must be atomic"
            events.append(("cleanup", normalized))
            if outcome == "cleanup_error" and normalized.startswith("DELETE FROM users"):
                raise asyncpg.UndefinedTableError("sensitive-query-marker")
            pending_writes.append(normalized)

        async def close(self, *, timeout):
            assert timeout == 5
            self.closed = True
            events.append(("cleanup", "close"))
            if outcome == "cleanup_close_error":
                raise OSError("cleanup close failed")

    class MaintenanceConnection:
        closed = False

        async def execute(self, query):
            nonlocal allowed
            assert query in {
                "ALTER DATABASE akb ALLOW_CONNECTIONS false",
                "ALTER DATABASE akb ALLOW_CONNECTIONS true",
            }
            allowed = query.endswith("true")
            events.append(("maintenance", query))

        async def close(self, *, timeout):
            assert timeout == 5
            self.closed = True
            events.append(("maintenance", "close"))

    cleanup, maintenance = CleanupConnection(), MaintenanceConnection()

    async def connect(**kwargs):
        if kwargs["database"] == "akb":
            return cleanup
        assert kwargs["database"] == "postgres"
        return maintenance

    monkeypatch.setattr(asyncpg, "connect", connect)
    if outcome in {"success", "cleanup_close_error"}:
        if outcome == "cleanup_close_error":
            with pytest.raises(OSError, match="cleanup close failed"):
                await runtime._reset_postgres_offline()
        else:
            await runtime._reset_postgres_offline()
        assert allowed
        assert committed_writes
        assert events.index(("transaction", "commit")) < events.index(
            ("maintenance", "ALTER DATABASE akb ALLOW_CONNECTIONS true")
        )
        truncate = next(query for query in committed_writes if query.startswith("TRUNCATE TABLE"))
        assert '"public"."tokens"' in truncate
        assert '"public"."users"' not in truncate
        assert '"public"."schema_migrations"' not in truncate
    else:
        with pytest.raises(ProvisioningFailure) as raised:
            await runtime._reset_postgres_offline()
        assert "sensitive-query-marker" not in str(raised.value)
        assert not allowed, "a failed reset must retain its admission fence"
        assert not committed_writes
        assert ("transaction", "rollback") in events
        assert ("maintenance", "ALTER DATABASE akb ALLOW_CONNECTIONS true") not in events
        attempted = [query for connection, query in events if connection == "cleanup"]
        if outcome == "missing_schema_table":
            assert not any(query.startswith(("DROP TABLE", "TRUNCATE TABLE", "DELETE")) for query in attempted)
        else:
            assert any(query.startswith("DROP TABLE") for query in attempted)
            assert any(query.startswith("TRUNCATE TABLE") for query in attempted)
    assert cleanup.closed and maintenance.closed
    assert events.index(("maintenance", "ALTER DATABASE akb ALLOW_CONNECTIONS false")) < events.index(
        ("transaction", "begin")
    )


def _fixture_seed_database(monkeypatch):
    """Retain the fixture user across seeds while exposing actual INSERT/UPDATE calls."""
    import asyncpg

    user = {"id": None}
    queries = []
    connections = []

    class Connection:
        closed = False

        async def fetchval(self, query, username):
            assert "SELECT id FROM users WHERE username = $1" in query
            if user["id"] is not None:
                assert user["username"] == username
            return user["id"]

        async def execute(self, query, *args):
            normalized = " ".join(query.split())
            queries.append((normalized, args))
            if normalized.startswith("INSERT INTO users"):
                actor_id, username, _email, password_hash = args
                assert user["id"] is None
                user.update(
                    id=actor_id, username=username, password_hash=password_hash,
                    is_admin=True, account_status="active", credential_change_required=False,
                )
            else:
                assert normalized.startswith("UPDATE users")
                assert all(field in normalized for field in (
                    "password_hash = $2", "is_admin = TRUE", "account_status = 'active'",
                    "credential_change_required = FALSE", "updated_at = NOW()",
                ))
                actor_id, password_hash = args
                assert actor_id == user["id"]
                user.update(
                    password_hash=password_hash, is_admin=True,
                    account_status="active", credential_change_required=False,
                )

        async def close(self):
            self.closed = True

    async def connect(**kwargs):
        assert kwargs["database"] == "akb"
        connection = Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    return user, queries, connections


def _fixture_hash_spy(monkeypatch):
    import bcrypt

    calls = []

    def hashpw(value, salt):
        assert salt == b"test-salt"
        calls.append(value)
        return f"test-hash-{len(calls)}".encode("ascii")

    monkeypatch.setattr(bcrypt, "gensalt", lambda: b"test-salt")
    monkeypatch.setattr(bcrypt, "hashpw", hashpw)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,expected_helpers", [
    ("empty", []),
    ("app-installation-lifecycle", ["_seed_app_installation_lifecycle"]),
    ("app-release-rollout", ["_seed_app_release_rollout"]),
    ("app-control-plane", [
        "_seed_app_release_rollout", "_seed_control_plane_installation_lifecycle",
        "_seed_control_plane_legacy_adoption",
    ]),
])
async def test_fixture_reuses_hash_but_restores_user_and_scenario_actors_on_every_seed(
    tmp_path, monkeypatch, scenario, expected_helpers,
):
    runtime = E2ERuntime(dataclasses.replace(make_config(tmp_path), scenario=scenario))
    monkeypatch.setenv("TEST_USERNAME_ENV", "fixture-user")
    monkeypatch.setenv("TEST_PASSWORD_ENV", "first test value")
    user, queries, connections = _fixture_seed_database(monkeypatch)
    hashes = _fixture_hash_spy(monkeypatch)
    helper_calls = []

    def seed_helper(name):
        async def seed(connection, *, password_hash, system_admin_id):
            assert connection in connections
            helper_calls.append((name, password_hash, system_admin_id))
        return seed

    for name in expected_helpers:
        monkeypatch.setattr(runtime, name, seed_helper(name))
    await runtime._seed_external_credential()
    actor_id = user["id"]
    initial_hash = user["password_hash"]
    user.update(
        password_hash="mutated test value", is_admin=False,  # pragma: allowlist secret
        account_status="suspended", credential_change_required=True,
    )
    await runtime._seed_external_credential()

    assert hashes == [b"first test value"]
    assert user == {
        "id": actor_id, "username": "fixture-user", "password_hash": initial_hash,
        "is_admin": True, "account_status": "active", "credential_change_required": False,
    }
    assert len(queries) == 2
    assert queries[0][0].startswith("INSERT INTO users")
    assert queries[1][0].startswith("UPDATE users")
    assert helper_calls == [
        (name, initial_hash, actor_id) for name in expected_helpers + expected_helpers
    ]
    assert all(connection.closed for connection in connections)
    assert runtime._fixture_password_cache[1] == initial_hash
    assert isinstance(runtime._fixture_password_cache[0], bytes)
    assert len(runtime._fixture_password_cache[0]) == 32
    assert runtime._fixture_password_cache[0] != b"first test value"


@pytest.mark.asyncio
async def test_fixture_hash_cache_is_one_entry_and_owned_by_each_runtime(tmp_path, monkeypatch):
    runtime = E2ERuntime(make_config(tmp_path))
    monkeypatch.setenv("TEST_USERNAME_ENV", "fixture-user")
    monkeypatch.setenv("TEST_PASSWORD_ENV", "first test value")
    _fixture_seed_database(monkeypatch)
    hashes = _fixture_hash_spy(monkeypatch)
    await runtime._seed_external_credential()
    first_fingerprint = runtime._fixture_password_cache[0]
    monkeypatch.setenv("TEST_PASSWORD_ENV", "second test value")
    await runtime._seed_external_credential()
    assert runtime._fixture_password_cache[0] != first_fingerprint
    monkeypatch.setenv("TEST_PASSWORD_ENV", "first test value")
    await runtime._seed_external_credential()
    assert hashes == [b"first test value", b"second test value", b"first test value"]

    independent = E2ERuntime(make_config(tmp_path))
    assert independent._fixture_password_cache is None
    await independent._seed_external_credential()
    assert hashes == [
        b"first test value", b"second test value", b"first test value", b"first test value",
    ]
    assert independent._fixture_password_cache[1] != runtime._fixture_password_cache[1]


@pytest.mark.asyncio
async def test_failed_fixture_hash_is_not_cached(tmp_path, monkeypatch):
    import bcrypt

    runtime = E2ERuntime(make_config(tmp_path))
    monkeypatch.setenv("TEST_USERNAME_ENV", "fixture-user")
    monkeypatch.setenv("TEST_PASSWORD_ENV", "first test value")
    _fixture_seed_database(monkeypatch)
    hashes = _fixture_hash_spy(monkeypatch)
    normal_hash = bcrypt.hashpw

    def broken_hash(*_args):
        raise ValueError("sensitive hash failure")

    monkeypatch.setattr(bcrypt, "hashpw", broken_hash)
    with pytest.raises(ProvisioningFailure) as raised:
        await runtime._seed_external_credential()
    assert "sensitive hash failure" not in str(raised.value)
    assert runtime._fixture_password_cache is None
    monkeypatch.setattr(bcrypt, "hashpw", normal_hash)
    await runtime._seed_external_credential()
    assert hashes == [b"first test value"]
    assert runtime._fixture_password_cache is not None


@pytest.mark.asyncio
async def test_cancelled_fixture_hash_thread_cannot_populate_cache(tmp_path, monkeypatch):
    import bcrypt
    import threading

    runtime = E2ERuntime(make_config(tmp_path))
    monkeypatch.setenv("TEST_USERNAME_ENV", "fixture-user")
    monkeypatch.setenv("TEST_PASSWORD_ENV", "first test value")
    _fixture_seed_database(monkeypatch)
    hashes = _fixture_hash_spy(monkeypatch)
    normal_hash = bcrypt.hashpw
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def delayed_hash(value, salt):
        entered.set()
        try:
            assert release.wait(timeout=5)
            return normal_hash(value, salt)
        finally:
            finished.set()

    monkeypatch.setattr(bcrypt, "hashpw", delayed_hash)
    seed = asyncio.create_task(runtime._seed_external_credential())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        seed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await seed
        assert runtime._fixture_password_cache is None
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert runtime._fixture_password_cache is None
    monkeypatch.setattr(bcrypt, "hashpw", normal_hash)
    await runtime._seed_external_credential()
    assert hashes == [b"first test value", b"first test value"]
    assert runtime._fixture_password_cache is not None


@pytest.mark.asyncio
async def test_reset_deadline_is_total_and_leaves_runtime_failed(tmp_path, monkeypatch):
    runtime = _ready_runtime(tmp_path)
    runtime.config = dataclasses.replace(runtime.config, timeout_seconds=0.02)
    _, entered, _, _ = _stub_reuse_reset(monkeypatch, runtime, block_at="seed")
    with pytest.raises((TimeoutError, ProvisioningFailure)):
        await asyncio.wait_for(runtime.reset_scenario(), timeout=1)
    assert entered.is_set()
    assert runtime._lifecycle_state == "failed"
    assert not runtime._prepared
    assert runtime._dependency_reset_evidence["last_succeeded"] is False


@pytest.mark.parametrize("state", ["starting", "resetting", "failed"])
def test_live_backend_does_not_make_incomplete_fixture_ready(tmp_path, state):
    runtime = _ready_runtime(tmp_path)
    runtime._lifecycle_state = state
    runtime._prepared = False
    assert runtime.app_ready  # A live backend alone is insufficient.
    assert runtime.fixture_health()["status"] == state
    assert runtime.fixture_discovery()["status"] == state


@pytest.mark.asyncio
@pytest.mark.parametrize("detached_child", [False, True])
async def test_process_termination_reaps_children_even_after_leader_exit_or_new_session(detached_child):
    import os
    import signal

    # The ordinary-group case exits its leader before termination. The detached
    # case keeps the leader alive so ownership of the separate session is known.
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    leader_code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}], "
        f"start_new_session={detached_child!r},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "print(child.pid,flush=True); "
        + ("time.sleep(60)" if detached_child else "time.sleep(0.1)")
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", leader_code,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    child_pid = int(await asyncio.wait_for(process.stdout.readline(), timeout=5))
    try:
        if not detached_child:
            await asyncio.wait_for(process.wait(), timeout=5)
        await asyncio.wait_for(terminate_process(process, grace_seconds=0.2), timeout=5)
        status = subprocess.run(
            ["ps", "-p", str(child_pid), "-o", "stat="],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        assert not status or status.startswith("Z")
        assert process.returncode is not None
    finally:
        for pid in (child_pid, process.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()


@pytest.mark.asyncio
async def test_failed_drain_retains_detached_child_ownership_for_cleanup(tmp_path, monkeypatch):
    import os
    import signal

    runtime = E2ERuntime(dataclasses.replace(make_config(tmp_path), timeout_seconds=0.3))
    child_code = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready',flush=True); time.sleep(60)"
    )
    leader_code = (
        "import signal,subprocess,sys,time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}], "
        "start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)\n"
        "assert child.stdout.readline().strip()==b'ready'\n"
        "child.stdout.close()\n"
        "print(child.pid,flush=True)\n"
        "while True: time.sleep(0.1)\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", leader_code,
        stdout=asyncio.subprocess.PIPE, start_new_session=True,
    )
    child_pid = int(await asyncio.wait_for(process.stdout.readline(), timeout=5))
    managed = ManagedProcess(process)
    runtime._children["backend"] = managed
    original_terminate = e2e_runtime.terminate_process

    async def terminate_with_short_grace(child, **kwargs):
        assert child is process
        assert kwargs["owned_groups"] is managed.owned_groups
        await original_terminate(child, grace_seconds=0.2, **kwargs)

    monkeypatch.setattr(e2e_runtime, "terminate_process", terminate_with_short_grace)
    try:
        with pytest.raises(TimeoutError):
            await runtime._drain_backend()
        assert process.returncode == 0
        assert runtime._children["backend"] is managed
        assert child_pid in managed.owned_groups
        parent = subprocess.run(
            ["ps", "-p", str(child_pid), "-o", "ppid="],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        assert parent and int(parent) != process.pid

        # The leader is gone and the child is reparented. A fresh descendant
        # search misses it; cleanup must use the ownership remembered by drain.
        await asyncio.wait_for(runtime._stop_named_process("backend"), timeout=5)
        status = subprocess.run(
            ["ps", "-p", str(child_pid), "-o", "stat="],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        assert not status or status.startswith("Z")
        assert "backend" not in runtime._children
    finally:
        for pid in (child_pid, process.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()


@pytest.mark.asyncio
async def test_backend_discovers_new_generation_config_from_its_working_directory(tmp_path):
    config = make_config(tmp_path)
    config.config_dir.mkdir(parents=True)
    (config.config_dir / "generation.txt").write_text("old")
    runtime = E2ERuntime(dataclasses.replace(config, storage_generation=1))
    runtime.config.config_dir.mkdir(parents=True)
    runtime.config.logs_dir.mkdir(parents=True)
    (runtime.config.config_dir / "generation.txt").write_text("new")
    command = [sys.executable, "-c", "from pathlib import Path; print(Path('config/generation.txt').read_text())"]
    for name, expected in (("backend", "new"), ("embed", "old")):
        await runtime._spawn_host_process(name, command, f"{name}.log")
        process = runtime._children[name].process
        assert await asyncio.wait_for(process.wait(), timeout=5) == 0
        assert (runtime.config.logs_dir / f"{name}.log").read_text().strip() == expected


@pytest.mark.asyncio
async def test_concurrent_logged_commands_remain_owned_until_each_finishes(tmp_path, monkeypatch):
    runtime = E2ERuntime(make_config(tmp_path))
    first, second = _LifecycleProcess(), _LifecycleProcess()
    processes = iter((first, second))
    spawned = asyncio.Queue()

    async def spawn(*args, **kwargs):
        process = next(processes)
        spawned.put_nowait(process)
        return process

    monkeypatch.setattr(e2e_runtime.asyncio, "create_subprocess_exec", spawn)
    task1 = asyncio.create_task(runtime._run_logged_command(["first"], log_path=tmp_path / "first.log"))
    assert await spawned.get() is first
    task2 = asyncio.create_task(runtime._run_logged_command(["second"], log_path=tmp_path / "second.log"))
    assert await spawned.get() is second
    assert runtime._active_commands == {first, second}
    first.release.set()
    assert await task1 == 0
    assert runtime._active_commands == {second}
    second.release.set()
    assert await task2 == 0
    assert runtime._active_commands == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("leader_exited", [False, True])
async def test_cancelled_logged_command_retains_ownership_if_termination_fails(tmp_path, monkeypatch, leader_exited):
    runtime = E2ERuntime(make_config(tmp_path))
    process = _LifecycleProcess()
    spawned = asyncio.Event()

    async def spawn(*args, **kwargs):
        spawned.set()
        return process

    async def fail_stop(child, *, owned_groups):
        assert child is process
        assert owned_groups == {process.pid}
        if leader_exited:
            process.returncode = 0  # A surviving descendant still requires ownership.
        raise ProvisioningFailure("cannot stop child")

    monkeypatch.setattr(e2e_runtime.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(e2e_runtime, "terminate_process", fail_stop)
    task = asyncio.create_task(runtime._run_logged_command(["blocked"], log_path=tmp_path / "blocked.log"))
    await spawned.wait()
    task.cancel()
    with pytest.raises(ProvisioningFailure, match="cannot stop child"):
        await task
    assert process in runtime._active_commands


@pytest.mark.asyncio
async def test_cleanup_cancels_reset_before_tearing_down_resources(tmp_path, monkeypatch):
    runtime = _ready_runtime(tmp_path)
    entered = asyncio.Event()
    operations = []

    async def resetting():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            # A reset cancellation handler must settle before cleanup touches
            # resources which it could otherwise still recreate.
            await asyncio.sleep(0)
            operations.append("reset_settled")

    async def stop_fixture():
        assert runtime._reset_task.done()
        operations.append("fixture_stop")

    async def stop(name):
        operations.append(f"stop:{name}")

    async def compose(*args, **kwargs):
        operations.append("compose_down")

    monkeypatch.setattr(runtime, "_stop_fixture_control", stop_fixture)
    monkeypatch.setattr(runtime, "_stop_named_process", stop)
    monkeypatch.setattr(runtime, "_compose", compose)
    runtime._reset_task = asyncio.create_task(resetting())
    await entered.wait()
    await runtime.cleanup()
    assert runtime._reset_task.cancelled()
    assert operations[0:2] == ["reset_settled", "fixture_stop"]
    assert operations[-1] == "compose_down"
    assert not runtime._prepared


@pytest.mark.asyncio
async def test_cleanup_continues_after_process_failure_and_can_retry(tmp_path, monkeypatch):
    runtime = _ready_runtime(tmp_path)
    for name in ("stdio", "frontend", "embed"):
        runtime._children[name] = ManagedProcess(_LifecycleProcess())
    backend = runtime._children["backend"]
    operations = []
    fail_backend = True

    async def stop_fixture():
        operations.append("fixture")

    async def stop(name):
        operations.append(name)
        if name == "backend" and fail_backend:
            raise ProvisioningFailure("injected termination failure")
        runtime._children.pop(name, None)

    async def compose(*args, **kwargs):
        assert args == ("down", "--volumes", "--remove-orphans")
        operations.append("down")
        return 0

    monkeypatch.setattr(runtime, "_stop_fixture_control", stop_fixture)
    monkeypatch.setattr(runtime, "_stop_named_process", stop)
    monkeypatch.setattr(runtime, "_compose", compose)
    with pytest.raises(ProvisioningFailure, match="runtime cleanup incomplete:.*backend"):
        await runtime.cleanup()
    assert operations == ["fixture", "stdio", "frontend", "backend", "embed", "down"]
    assert runtime._children == {"backend": backend}
    assert not runtime._cleaned
    assert not runtime._prepared

    fail_backend = False
    await runtime.cleanup()
    assert runtime._cleaned
    assert runtime._children == {}
    assert operations.count("down") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("unexpected_sibling_exit", [False, True])
async def test_supervisor_distinguishes_intentional_stop_from_unexpected_child_exit(tmp_path, monkeypatch, unexpected_sibling_exit):
    runtime = _ready_runtime(tmp_path)
    processes = {}
    for name in ("backend", "embed"):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            start_new_session=True,
        )
        processes[name] = process
        runtime._children[name] = ManagedProcess(process)
    original_terminate = e2e_runtime.terminate_process
    backend_exited = asyncio.Event()
    release_ownership = asyncio.Event()

    async def delayed_termination(process, **kwargs):
        await original_terminate(process, **kwargs)
        if process is processes["backend"]:
            backend_exited.set()
            # Exercise the interval between OS exit and confirmed release of
            # process ownership, which used to look like a backend crash.
            await release_ownership.wait()

    monkeypatch.setattr(e2e_runtime, "terminate_process", delayed_termination)
    supervisor = asyncio.create_task(runtime._serve_foreground())
    stop_backend = asyncio.create_task(runtime._stop_named_process("backend"))
    try:
        await asyncio.wait_for(backend_exited.wait(), timeout=5)
        assert runtime._children["backend"].process is processes["backend"]
        if unexpected_sibling_exit:
            # Suppressing the intentional backend exit must not hide a
            # simultaneous unexpected exit from a different owned child.
            processes["embed"].terminate()
            assert await asyncio.wait_for(supervisor, timeout=2) == 1
        else:
            await asyncio.sleep(0.15)
            assert not supervisor.done()
            release_ownership.set()
            await stop_backend
            runtime.request_stop()
            assert await asyncio.wait_for(supervisor, timeout=2) == 0
    finally:
        release_ownership.set()
        runtime.request_stop()
        await asyncio.gather(stop_backend, supervisor, return_exceptions=True)
        for process in processes.values():
            await original_terminate(process, grace_seconds=0.2)


@pytest.mark.asyncio
async def test_publication_phase_failure_prevents_later_gate_suites(tmp_path, monkeypatch):
    runtime = E2ERuntime(make_config(tmp_path, mode="gate"))
    monkeypatch.setenv(SOURCE_REVISION_ENV, "d" * 40)

    async def fail():
        raise AssertionError("replica assertion")

    async def must_not_run(*args, **kwargs):
        pytest.fail("later suites ran after the required replica phase failed")

    monkeypatch.setattr(runtime, "_run_publication_budget_check", fail)
    monkeypatch.setattr(runtime, "_run_logged_command", must_not_run)
    with pytest.raises(e2e_runtime.ProductAssertionFailure, match="replica budget"):
        await runtime._run_gate()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "assertion", "startup", "drain", "reset_during_replacement"])
async def test_publication_replica_phase_owns_processes_and_fences_reset(tmp_path, monkeypatch, outcome):
    import publication_budget_probe

    runtime = _ready_runtime(tmp_path)
    runtime._pat_value = "synthetic-fixture-pat"
    primary = runtime._children["backend"]
    calls, launches, completed = [], [], []

    class Replica:
        returncode = None

        def __init__(self, pid):
            self.pid = pid

    async def spawn(name, argv, log_name, **kwargs):
        assert runtime._reset_lock.locked()
        assert name == "publication-replica"
        assert kwargs["environment"] == {"AKB_PROCESS_ROLE": "api"}
        assert kwargs["cwd"] == runtime.config.config_dir.parent
        launch = {"id": argv[argv.index("--launch-id") + 1],
                  "report": Path(argv[argv.index("--drain-report") + 1]),
                  "port": argv[argv.index("--port") + 1]}
        launches.append(launch)
        runtime._children[name] = ManagedProcess(Replica(primary.process.pid + len(launches)))
        calls.append("start")

    async def ready(*args):
        calls.append("ready")
        if outcome == "startup":
            raise ProvisioningFailure("replica startup failed")

    async def drain(name, launch_id, report_path):
        assert runtime._reset_lock.locked()
        assert name == "publication-replica"
        assert launch_id == launches[-1]["id"]
        assert report_path == launches[-1]["report"]
        calls.append("drain")
        if outcome == "drain":
            raise ProvisioningFailure("replica drain failed")
        runtime._children.pop(name)
        if outcome == "reset_during_replacement":
            runtime.config = dataclasses.replace(runtime.config, storage_generation=1)
        return {}

    async def probe(origins, pat, *, replace_replica, hold_budget_lock):
        assert len(origins) == len(set(origins)) == 2
        assert pat == runtime._pat_value
        assert runtime._reset_lock.locked()
        if outcome == "assertion":
            raise AssertionError("wrong budget")
        evidence = await replace_replica()
        assert evidence["before"]["pid"] != evidence["after"]["pid"]
        assert evidence["before"]["launch_id"] != evidence["after"]["launch_id"]
        assert evidence["storage_generation"] == 0
        assert launches[0]["port"] == launches[1]["port"]
        return {"replacement": evidence}

    def emit(event):
        assert "publication-replica" not in runtime._children, "success emitted before drain"
        completed.append(event)

    monkeypatch.setattr(runtime, "_spawn_host_process", spawn)
    monkeypatch.setattr(runtime, "_wait_http", ready)
    monkeypatch.setattr(runtime, "_drain_backend_process", drain)
    monkeypatch.setattr(publication_budget_probe, "run_checks", probe)
    monkeypatch.setattr(e2e_runtime, "emit_gate_event", emit)
    if outcome == "success":
        await runtime._run_publication_budget_check()
        assert runtime._prepared and runtime._lifecycle_state == "ready"
        assert calls == ["start", "ready", "drain", "start", "ready", "drain"]
        assert completed[0]["event"] == "publication_budget_complete"
        assert completed[0]["checks"] == 5
    else:
        with pytest.raises((AssertionError, ProvisioningFailure)):
            await runtime._run_publication_budget_check()
        assert not runtime._prepared and runtime._lifecycle_state == "failed"
        assert completed == []
    assert not runtime._reset_lock.locked()
    assert runtime._children["backend"] is primary
    assert ("publication-replica" in runtime._children) is (outcome == "drain")
