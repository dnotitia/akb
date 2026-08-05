"""Project-neutral foreground runtime for the repository's HTTP E2E suites.

The runtime keeps the CI topology deliberately small: it starts the existing
embedding stub and backend against an externally supplied PostgreSQL service.
The process itself is the supervisor.  Its fixture control plane is local-only
and is separate from the product API:

* ``GET /__e2e/health`` reports runtime health.
* ``GET /__e2e/ready`` returns the public ready payload.
* ``POST /__e2e/reset`` empties the external database and Git fixture root,
  then restarts the same backend command.
* ``POST /__e2e/stop`` requests graceful supervisor shutdown.

The database URL is kept in memory only.  It is never written to generated
configuration, the ready artifact, or runtime output.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


DEFAULT_DATABASE_URL = "postgresql://akb:akb@localhost:15432/akb"  # pragma: allowlist secret
DEFAULT_ORIGIN = "http://localhost:8000"
DEFAULT_FIXTURE_ORIGIN = "http://localhost:8889"
DEFAULT_SCENARIO = "empty"
APP_LIFECYCLE_SCENARIO = "app_lifecycle"
DEFAULT_S3_BUCKET = "akb-files"
RUNTIME_TMP = Path(tempfile.gettempdir())
DEFAULT_READY_FILE = str(RUNTIME_TMP / "akb-e2e-ready.json")
EMBED_PORT = 8888
BACKEND_PORT = 8000
GIT_FIXTURE_ROOT = RUNTIME_TMP / "akb-vaults"
EMBED_LOG = RUNTIME_TMP / "embed-stub.log"
BACKEND_LOG = RUNTIME_TMP / "backend.log"
REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = Path(__file__).resolve().with_name("e2e-postgres.compose.yml")
DEFAULT_DOCKER_ARGV = "docker"
COMPOSE_PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SUPPORTED_SCENARIOS = frozenset({DEFAULT_SCENARIO, APP_LIFECYCLE_SCENARIO})
FIXTURE_MANIFEST_SCHEMA_VERSION = 1
CREDENTIAL_VARIABLES = (
    "AKB_E2E_SYSTEM_ADMIN_TOKEN",
    "AKB_E2E_TARGET_VAULT_ADMIN_TOKEN",
    "AKB_E2E_READER_TOKEN",
    "AKB_E2E_WRITER_TOKEN",
    "AKB_E2E_FOREIGN_VAULT_ADMIN_TOKEN",
    "AKB_E2E_PRIMARY_APP_CREDENTIAL",
    "AKB_E2E_PRIMARY_APP_TOKEN",
    "AKB_E2E_FOREIGN_APP_CREDENTIAL",
)


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class RuntimeSettings:
    database_url: str = field(repr=False)
    origin: str = DEFAULT_ORIGIN
    fixture_origin: str = DEFAULT_FIXTURE_ORIGIN
    scenario: str = DEFAULT_SCENARIO
    ready_file: Path = field(default_factory=lambda: Path(DEFAULT_READY_FILE))
    run_suites: bool = False
    manage_postgres: bool = False
    compose_project: str = ""
    docker_argv: tuple[str, ...] = ("docker",)
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_access_key: str = field(default="", repr=False)
    s3_secret_key: str = field(default="", repr=False)
    app_token_secret: str = field(default="", repr=False)


def parse_database_url(database_url: str) -> DatabaseSettings:
    """Extract only the fields needed by the existing app config."""

    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("invalid database URL")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise ValueError("invalid database URL") from exc
    name = parsed.path.lstrip("/")
    if not name or not parsed.username:
        raise ValueError("invalid database URL")
    return DatabaseSettings(
        host=parsed.hostname,
        port=port,
        name=unquote(name),
        user=unquote(parsed.username),
        password=unquote(parsed.password or ""),
    )


def _validate_compose_project(project: str) -> str:
    if not COMPOSE_PROJECT_PATTERN.fullmatch(project):
        raise ValueError("invalid compose project")
    return project


def _resolve_compose_project(value: str | None) -> str:
    project = value or f"akb-e2e-{os.getpid()}-{secrets.token_hex(4)}"
    return _validate_compose_project(project)


def _resolve_docker_argv(value: str | None) -> tuple[str, ...]:
    try:
        argv = tuple(shlex.split(value or DEFAULT_DOCKER_ARGV))
    except ValueError as exc:
        raise ValueError("invalid docker argv") from exc
    if not argv:
        raise ValueError("docker argv is empty")
    return argv


def _validate_managed_database(database: DatabaseSettings) -> None:
    if (
        database.host not in {"localhost", "127.0.0.1"}
        or database.name != "akb"
        or database.user != "akb"
        or database.password != "akb"  # pragma: allowlist secret
    ):
        raise ValueError("managed database must use the CI PostgreSQL settings")


def _resolve_s3_settings(environ: Mapping[str, str]) -> tuple[str, str, str, str]:
    endpoint = (environ.get("AKB_E2E_S3_ENDPOINT") or "").strip()
    bucket = (environ.get("AKB_E2E_S3_BUCKET") or "").strip()
    access_key = environ.get("AKB_E2E_S3_ACCESS_KEY") or ""
    secret_key = environ.get("AKB_E2E_S3_SECRET_KEY") or ""
    if not endpoint:
        if any((bucket, access_key, secret_key)):
            raise ValueError("S3 settings require an endpoint")
        return "", "", "", ""
    _validate_origin(endpoint)
    bucket = bucket or DEFAULT_S3_BUCKET
    if not bucket or any(char.isspace() for char in bucket):
        raise ValueError("invalid S3 bucket")
    if not access_key or not secret_key:
        raise ValueError("S3 credentials are incomplete")
    return endpoint, bucket, access_key, secret_key


def _yaml_scalar(value: object) -> str:
    text = str(value)
    if text and all(char.isalnum() or char in "._/-:" for char in text):
        return text
    return json.dumps(text)


def render_app_config(
    database: DatabaseSettings,
    origin: str,
    s3_endpoint: str = "",
    s3_bucket: str = "",
) -> str:
    """Render the same CI app settings that were previously inline in YAML."""

    lines = [
        f"db_host: {_yaml_scalar(database.host)}",
        f"db_port: {database.port}",
        f"db_name: {_yaml_scalar(database.name)}",
        f"db_user: {_yaml_scalar(database.user)}",
        f"public_base_url: {_yaml_scalar(origin)}",
        "git_storage_path: /tmp/akb-vaults",
        "vector_store_driver: pgvector",
        "embed_base_url: http://localhost:8888/v1",
        "embed_model: ci-embed-stub",
        "embed_dimensions: 1536",
        'llm_base_url: ""',
        'llm_model: ""',
        "rerank_enabled: false",
        f"s3_endpoint_url: {_yaml_scalar(s3_endpoint)}" if s3_endpoint else 's3_endpoint_url: ""',
    ]
    if s3_endpoint:
        lines.append(f"s3_bucket: {_yaml_scalar(s3_bucket)}")
    lines.append("")
    return "\n".join(lines)


def render_secret_config(
    database: DatabaseSettings,
    s3_access_key: str = "",
    s3_secret_key: str = "",
    app_token_secret: str = "",
) -> str:
    """Render the existing CI-only secrets without the source database URL."""

    lines = [
        f"db_password: {_yaml_scalar(database.password)}  # pragma: allowlist secret",
        "jwt_secret: ci-only-jwt-secret-not-for-prod-use  # pragma: allowlist secret",
        "embed_api_key: ci-stub-no-auth  # pragma: allowlist secret",
        f"app_token_secret: {_yaml_scalar(app_token_secret)}  # pragma: allowlist secret",
    ]
    if s3_access_key and s3_secret_key:
        lines.extend(
            [
                f"s3_access_key: {_yaml_scalar(s3_access_key)}  # pragma: allowlist secret",
                f"s3_secret_key: {_yaml_scalar(s3_secret_key)}  # pragma: allowlist secret",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_runtime_config(settings: RuntimeSettings, repo_root: Path = REPO_ROOT) -> None:
    database = parse_database_url(settings.database_url)
    config_dir = repo_root / "config"
    _atomic_write(
        config_dir / "app.yaml",
        render_app_config(database, settings.origin, settings.s3_endpoint, settings.s3_bucket),
        0o644,
    )
    _atomic_write(
        config_dir / "secret.yaml",
        render_secret_config(
            database,
            settings.s3_access_key,
            settings.s3_secret_key,
            settings.app_token_secret or secrets.token_urlsafe(32),
        ),
        0o600,
    )


def fixture_manifest_path(ready_file: Path) -> Path:
    return ready_file.with_name(f"{ready_file.stem}.fixtures.json")


def credential_profile_path(ready_file: Path) -> Path:
    return ready_file.with_name(f"{ready_file.stem}.credentials.env")


def fixture_artifact_paths(settings: RuntimeSettings) -> tuple[Path, Path]:
    return (
        fixture_manifest_path(settings.ready_file),
        credential_profile_path(settings.ready_file),
    )


def remove_fixture_artifacts(settings: RuntimeSettings) -> None:
    for path in fixture_artifact_paths(settings):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _fixture_material(prefix: str) -> tuple[str, str, str]:
    raw = prefix + secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest(), raw[:16]


async def _seed_app_lifecycle(
    database_url: str,
    *,
    origin: str,
    fixture_origin: str,
) -> tuple[dict[str, object], dict[str, str]]:
    """Seed the small public lifecycle matrix in one database transaction."""

    import asyncpg

    namespace = secrets.token_hex(8)
    connection = await asyncpg.connect(database_url, timeout=15)
    try:
        async with connection.transaction():
            async def create_user(label: str, *, is_admin: bool = False) -> dict[str, str]:
                user_id = uuid.uuid4()
                username = f"e2e-{label}-{namespace}"
                await connection.execute(
                    """
                    INSERT INTO users (
                        id, username, email, password_hash, display_name,
                        is_admin, account_status, account_kind
                    ) VALUES ($1, $2, $3, 'fixture-disabled', $4, $5, 'active', 'human')
                    """,
                    user_id,
                    username,
                    f"{username}@example.invalid",
                    label,
                    is_admin,
                )
                return {"id": str(user_id), "username": username}

            async def create_pat(
                user: dict[str, str],
                label: str,
                scopes: list[str],
            ) -> tuple[str, dict[str, str]]:
                raw, token_hash, token_prefix = _fixture_material("akb_")
                token_id = uuid.uuid4()
                await connection.execute(
                    """
                    INSERT INTO tokens (
                        id, user_id, name, token_hash, token_prefix,
                        scopes, vault_scope, key_class
                    ) VALUES ($1, $2, $3, $4, $5, $6, NULL, 'pat')
                    """,
                    token_id,
                    uuid.UUID(user["id"]),
                    f"e2e-{label}-{namespace}",
                    token_hash,
                    token_prefix,
                    scopes,
                )
                return raw, {"id": str(token_id), "name": label}

            async def create_vault(label: str, owner: dict[str, str]) -> dict[str, str]:
                vault_id = uuid.uuid4()
                name = f"e2e-{label}-{namespace}"
                await connection.execute(
                    """
                    INSERT INTO vaults (id, name, git_path, owner_id)
                    VALUES ($1, $2, $3, $4)
                    """,
                    vault_id,
                    name,
                    str(GIT_FIXTURE_ROOT / f"{name}.git"),
                    uuid.UUID(owner["id"]),
                )
                return {"id": str(vault_id), "name": name, "owner_id": owner["id"]}

            async def grant_vault(
                vault: dict[str, str],
                user: dict[str, str],
                role: str,
            ) -> None:
                await connection.execute(
                    """
                    INSERT INTO vault_access (vault_id, user_id, role, granted_by)
                    VALUES ($1, $2, $3, $2)
                    ON CONFLICT (vault_id, user_id) DO UPDATE SET role = EXCLUDED.role
                    """,
                    uuid.UUID(vault["id"]),
                    uuid.UUID(user["id"]),
                    role,
                )

            async def create_app(label: str) -> dict[str, str]:
                app_id = uuid.uuid4()
                app_key = f"e2e-{label}-{namespace}"
                await connection.execute(
                    """
                    INSERT INTO app_definitions (id, app_key, display_name, description)
                    VALUES ($1, $2, $3, $4)
                    """,
                    app_id,
                    app_key,
                    f"E2E {label}",
                    "Repository E2E lifecycle fixture",
                )
                return {"id": str(app_id), "app_key": app_key}

            async def create_release(
                app: dict[str, str],
                version: str,
                fingerprint: str,
            ) -> dict[str, str]:
                release_id = uuid.uuid4()
                manifest = json.dumps(
                    {
                        "steps": [{"id": "prepare"}],
                        "expected_schema_fingerprint": fingerprint,
                    },
                    separators=(",", ":"),
                )
                checksum = hashlib.sha256(manifest.encode()).hexdigest()
                await connection.execute(
                    """
                    INSERT INTO app_releases (
                        id, app_id, version, manifest, manifest_checksum
                    ) VALUES ($1, $2, $3, $4::jsonb, $5)
                    """,
                    release_id,
                    uuid.UUID(app["id"]),
                    version,
                    manifest,
                    checksum,
                )
                return {
                    "id": str(release_id),
                    "version": version,
                    "schema_fingerprint": fingerprint,
                }

            async def create_installation(
                app: dict[str, str],
                vault: dict[str, str],
                release: dict[str, str],
                *,
                lifecycle: str,
                resource: tuple[str, str] | None = None,
                observed: bool = False,
                observed_fingerprint: str | None = None,
                blocked_reason: str | None = None,
            ) -> dict[str, object]:
                installation_id = uuid.uuid4()
                desired_release_id = (
                    uuid.UUID(release["id"]) if lifecycle != "uninstalled" else None
                )
                current_release_id = uuid.UUID(release["id"])
                await connection.execute(
                    """
                    INSERT INTO vault_app_installations (
                        id, app_id, vault_id, desired_release_id,
                        current_release_id, lifecycle, blocked_reason,
                        grant_generation
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, 0)
                    """,
                    installation_id,
                    uuid.UUID(app["id"]),
                    uuid.UUID(vault["id"]),
                    desired_release_id,
                    current_release_id,
                    lifecycle,
                    blocked_reason,
                )
                grant_id = uuid.uuid4()
                await connection.execute(
                    """
                    INSERT INTO installation_grants (
                        id, installation_id, generation, status,
                        capabilities, issuer, provenance, revoked_at
                    ) VALUES (
                        $1, $2, 1, 'active', $3, 'fixture', '{}'::jsonb, NULL
                    )
                    """,
                    grant_id,
                    installation_id,
                    ["installation:read", "inventory:read"],
                )
                if lifecycle == "uninstalled":
                    await connection.execute(
                        """
                        UPDATE installation_grants
                           SET status = 'revoked', revoked_at = NOW()
                         WHERE id = $1
                        """,
                        grant_id,
                    )
                resource_entry: dict[str, str] | None = None
                if resource is not None:
                    resource_entry = {
                        "kind": resource[0],
                        "key": resource[1],
                        "status": "retained" if lifecycle == "uninstalled" else "owned",
                    }
                    await connection.execute(
                        """
                        INSERT INTO app_owned_resources (
                            installation_id, vault_id, resource_kind,
                            resource_key, status
                        ) VALUES ($1, $2, $3, $4, $5)
                        """,
                        installation_id,
                        uuid.UUID(vault["id"]),
                        resource_entry["kind"],
                        resource_entry["key"],
                        resource_entry["status"],
                    )
                if observed:
                    await connection.execute(
                        """
                        INSERT INTO app_installation_observed_states (
                            installation_id, app_id, vault_id, observed_generation,
                            observed_at, observed_release_id, observed_release_version,
                            schema_fingerprint, observed_grant_generation,
                            checkpoint, recent_error
                        ) VALUES (
                            $1, $2, $3, 1, NOW(), $4, $5, $6, 1,
                            '{"phase":"ready"}'::jsonb, NULL
                        )
                        """,
                        installation_id,
                        uuid.UUID(app["id"]),
                        uuid.UUID(vault["id"]),
                        uuid.UUID(release["id"]),
                        release["version"],
                        observed_fingerprint or release["schema_fingerprint"],
                    )
                return {
                    "id": str(installation_id),
                    "app_id": app["id"],
                    "vault_id": vault["id"],
                    "release_id": release["id"],
                    "lifecycle": lifecycle,
                    "resource": resource_entry,
                    "observed": observed,
                    "observed_fingerprint": (
                        observed_fingerprint or release["schema_fingerprint"]
                        if observed
                        else None
                    ),
                }

            async def create_app_credential(
                app: dict[str, str],
                deployment: str,
                *,
                status: str = "active",
            ) -> tuple[str | None, dict[str, str]]:
                raw, credential_hash, credential_prefix = _fixture_material("akb_app_")
                credential_id = uuid.uuid4()
                await connection.execute(
                    """
                    INSERT INTO app_credentials (
                        id, app_id, deployment, generation, credential_hash,
                        credential_prefix, status, overlap_until, revoked_at
                    ) VALUES (
                        $1, $2, $3, 1, $4, $5, $6,
                        CASE WHEN $6 = 'rotated' THEN NOW() - INTERVAL '1 second' ELSE NULL END,
                        CASE WHEN $6 = 'revoked' THEN NOW() ELSE NULL END
                    )
                    """,
                    credential_id,
                    uuid.UUID(app["id"]),
                    deployment,
                    credential_hash,
                    credential_prefix,
                    status,
                )
                metadata = {
                    "id": str(credential_id),
                    "app_id": app["id"],
                    "deployment": deployment,
                    "status": status,
                }
                return (raw if status == "active" else None), metadata

            system_admin = await create_user("system-admin", is_admin=True)
            vault_admin = await create_user("vault-admin")
            reader = await create_user("reader")
            writer = await create_user("writer")
            foreign_admin = await create_user("foreign-admin")

            system_token, system_token_meta = await create_pat(
                system_admin, "system-admin", ["read", "write", "admin"]
            )
            vault_token, vault_token_meta = await create_pat(
                vault_admin, "vault-admin", ["read", "write", "admin"]
            )
            reader_token, reader_token_meta = await create_pat(
                reader, "reader", ["read"]
            )
            writer_token, writer_token_meta = await create_pat(
                writer, "writer", ["read", "write"]
            )
            foreign_token, foreign_token_meta = await create_pat(
                foreign_admin, "foreign-admin", ["read", "write", "admin"]
            )

            target_active = await create_vault("target-active", vault_admin)
            target_install = await create_vault("target-install", vault_admin)
            target_blocked = await create_vault("target-blocked", vault_admin)
            target_restore_compatible = await create_vault("target-restore-compatible", vault_admin)
            target_restore_mismatch = await create_vault("target-restore-mismatch", vault_admin)
            target_restore_unknown = await create_vault("target-restore-unknown", vault_admin)
            target_fresh_collision = await create_vault("target-fresh-collision", vault_admin)
            target_fresh_empty = await create_vault("target-fresh-empty", vault_admin)
            foreign_vault = await create_vault("foreign", foreign_admin)
            for vault in (
                target_active,
                target_install,
                target_blocked,
                target_restore_compatible,
                target_restore_mismatch,
                target_restore_unknown,
                target_fresh_collision,
                target_fresh_empty,
            ):
                await grant_vault(vault, reader, "reader")
                await grant_vault(vault, writer, "writer")

            primary_app = await create_app("primary")
            foreign_app = await create_app("foreign")
            primary_release = await create_release(primary_app, "1.0.0", "a" * 64)
            primary_conflict_release = await create_release(primary_app, "2.0.0", "a" * 64)
            foreign_release = await create_release(foreign_app, "1.0.0", "b" * 64)

            installations = {
                "active": await create_installation(
                    primary_app,
                    target_active,
                    primary_release,
                    lifecycle="active",
                    resource=("managed_table", f"owned-{namespace}"),
                    observed=True,
                ),
                "blocked": await create_installation(
                    primary_app,
                    target_blocked,
                    primary_release,
                    lifecycle="blocked",
                    resource=("managed_table", f"blocked-{namespace}"),
                    observed=True,
                    blocked_reason="worker_timeout",
                ),
                "restore_compatible": await create_installation(
                    primary_app,
                    target_restore_compatible,
                    primary_release,
                    lifecycle="uninstalled",
                    resource=("managed_table", f"restore-compatible-{namespace}"),
                    observed=True,
                ),
                "restore_mismatch": await create_installation(
                    primary_app,
                    target_restore_mismatch,
                    primary_release,
                    lifecycle="uninstalled",
                    resource=("managed_table", f"restore-mismatch-{namespace}"),
                    observed=True,
                    observed_fingerprint="c" * 64,
                ),
                "restore_unknown": await create_installation(
                    primary_app,
                    target_restore_unknown,
                    primary_release,
                    lifecycle="uninstalled",
                    resource=("managed_table", f"restore-unknown-{namespace}"),
                ),
                "fresh_retained": await create_installation(
                    primary_app,
                    target_fresh_collision,
                    primary_release,
                    lifecycle="uninstalled",
                    resource=("managed_table", f"fresh-retained-{namespace}"),
                ),
                "fresh_empty": await create_installation(
                    primary_app,
                    target_fresh_empty,
                    primary_release,
                    lifecycle="uninstalled",
                ),
                "foreign_active": await create_installation(
                    foreign_app,
                    foreign_vault,
                    foreign_release,
                    lifecycle="active",
                    resource=("managed_table", f"foreign-{namespace}"),
                    observed=True,
                ),
            }

            primary_credential, primary_credential_meta = await create_app_credential(
                primary_app, "production"
            )
            foreign_credential, foreign_credential_meta = await create_app_credential(
                foreign_app, "production"
            )
            _, stale_credential_meta = await create_app_credential(
                primary_app, "stale", status="rotated"
            )
            _, revoked_credential_meta = await create_app_credential(
                primary_app, "revoked", status="revoked"
            )

        assert primary_credential is not None
        assert foreign_credential is not None
        primary_app_path = primary_app["id"]
        install_path = target_install["id"]
        active_path = target_active["id"]
        manifest: dict[str, object] = {
            "schema_version": FIXTURE_MANIFEST_SCHEMA_VERSION,
            "scenario": APP_LIFECYCLE_SCENARIO,
            "namespace": namespace,
            "origin": origin,
            "fixture_origin": fixture_origin,
            "reset_url": f"{fixture_origin}/__e2e/reset",
            "credential_variables": list(CREDENTIAL_VARIABLES),
            "actors": {
                "system_admin": {
                    **system_admin,
                    "token_env": CREDENTIAL_VARIABLES[0],
                    "roles": ["system_admin"],
                    "token_id": system_token_meta["id"],
                },
                "vault_admin": {
                    **vault_admin,
                    "token_env": CREDENTIAL_VARIABLES[1],
                    "roles": ["owner", "admin", "writer"],
                    "token_id": vault_token_meta["id"],
                    "vault_ids": [
                        target_active["id"],
                        target_install["id"],
                        target_blocked["id"],
                        target_restore_compatible["id"],
                        target_restore_mismatch["id"],
                        target_restore_unknown["id"],
                        target_fresh_collision["id"],
                        target_fresh_empty["id"],
                    ],
                },
                "reader": {
                    **reader,
                    "token_env": CREDENTIAL_VARIABLES[2],
                    "roles": ["reader"],
                    "token_id": reader_token_meta["id"],
                    "vault_ids": [target_active["id"], target_install["id"]],
                },
                "writer": {
                    **writer,
                    "token_env": CREDENTIAL_VARIABLES[3],
                    "roles": ["writer"],
                    "token_id": writer_token_meta["id"],
                    "vault_ids": [
                        target_active["id"],
                        target_install["id"],
                        target_blocked["id"],
                        target_restore_compatible["id"],
                        target_restore_mismatch["id"],
                        target_restore_unknown["id"],
                        target_fresh_collision["id"],
                        target_fresh_empty["id"],
                    ],
                },
                "foreign_admin": {
                    **foreign_admin,
                    "token_env": CREDENTIAL_VARIABLES[4],
                    "roles": ["owner", "admin", "writer"],
                    "token_id": foreign_token_meta["id"],
                    "vault_ids": [foreign_vault["id"]],
                },
            },
            "apps": {
                "primary": {
                    **primary_app,
                    "releases": {
                        "primary": primary_release,
                        "conflict": primary_conflict_release,
                    },
                    "credential_env": CREDENTIAL_VARIABLES[5],
                    "token_env": CREDENTIAL_VARIABLES[6],
                },
                "foreign": {
                    **foreign_app,
                    "releases": {"primary": foreign_release},
                    "credential_env": CREDENTIAL_VARIABLES[7],
                },
            },
            "vaults": {
                "active": target_active,
                "install": target_install,
                "blocked": target_blocked,
                "restore_compatible": target_restore_compatible,
                "restore_mismatch": target_restore_mismatch,
                "restore_unknown": target_restore_unknown,
                "fresh_retained": target_fresh_collision,
                "fresh_empty": target_fresh_empty,
                "foreign": foreign_vault,
            },
            "installations": installations,
            "credentials": {
                "primary": primary_credential_meta,
                "foreign": foreign_credential_meta,
                "stale": stale_credential_meta,
                "revoked": revoked_credential_meta,
            },
            "endpoint_tasks": {
                "install": {
                    "method": "PUT",
                    "path": f"/api/v1/apps/{primary_app_path}/installations/{install_path}",
                    "release_id": primary_release["id"],
                    "capabilities": ["installation:read"],
                },
                "replay": {
                    "method": "PUT",
                    "path": f"/api/v1/apps/{primary_app_path}/installations/{install_path}",
                    "release_id": primary_release["id"],
                    "capabilities": ["installation:read"],
                },
                "conflict": {
                    "method": "PUT",
                    "path": f"/api/v1/apps/{primary_app_path}/installations/{install_path}",
                    "release_id": primary_conflict_release["id"],
                    "capabilities": ["installation:read"],
                },
                "active_status": {
                    "method": "GET",
                    "path": f"/api/v1/apps/{primary_app_path}/installations/{active_path}",
                },
                "uninstall": {
                    "method": "DELETE",
                    "path": f"/api/v1/apps/{primary_app_path}/installations/{active_path}",
                },
                "restore_compatible": {
                    "method": "PUT",
                    "path": f"/api/v1/apps/{primary_app_path}/installations/{installations['restore_compatible']['vault_id']}",
                    "release_id": primary_release["id"],
                    "capabilities": ["installation:read"],
                    "mode": "restore",
                },
                "restore_mismatch": {
                    "method": "PUT",
                    "path": f"/api/v1/apps/{primary_app_path}/installations/{installations['restore_mismatch']['vault_id']}",
                    "release_id": primary_release["id"],
                    "capabilities": ["installation:read"],
                    "mode": "restore",
                },
                "restore_unknown": {
                    "method": "PUT",
                    "path": f"/api/v1/apps/{primary_app_path}/installations/{installations['restore_unknown']['vault_id']}",
                    "release_id": primary_release["id"],
                    "capabilities": ["installation:read"],
                    "mode": "restore",
                },
                "fresh_retained": {
                    "method": "PUT",
                    "path": f"/api/v1/apps/{primary_app_path}/installations/{installations['fresh_retained']['vault_id']}",
                    "release_id": primary_conflict_release["id"],
                    "capabilities": ["installation:read"],
                    "mode": "fresh",
                },
                "fresh_empty": {
                    "method": "PUT",
                    "path": f"/api/v1/apps/{primary_app_path}/installations/{installations['fresh_empty']['vault_id']}",
                    "release_id": primary_conflict_release["id"],
                    "capabilities": ["installation:read"],
                    "mode": "fresh",
                },
                "app_status": {
                    "method": "GET",
                    "path": f"/api/v1/app/installations/{active_path}",
                    "credential_env": CREDENTIAL_VARIABLES[6],
                },
                "foreign_app_status": {
                    "method": "GET",
                    "path": f"/api/v1/app/installations/{installations['foreign_active']['vault_id']}",
                    "credential_env": CREDENTIAL_VARIABLES[7],
                },
                "credential_exchange": {
                    "method": "POST",
                    "path": "/api/v1/auth/app-token",
                },
            },
        }
        credentials = {
            CREDENTIAL_VARIABLES[0]: system_token,
            CREDENTIAL_VARIABLES[1]: vault_token,
            CREDENTIAL_VARIABLES[2]: reader_token,
            CREDENTIAL_VARIABLES[3]: writer_token,
            CREDENTIAL_VARIABLES[4]: foreign_token,
            CREDENTIAL_VARIABLES[5]: primary_credential,
            CREDENTIAL_VARIABLES[7]: foreign_credential,
        }
        return manifest, credentials
    finally:
        await connection.close()


def _exchange_fixture_app_token(origin: str, credential: str) -> str:
    request = Request(
        f"{origin}/api/v1/auth/app-token",
        data=json.dumps({"credential": credential}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:  # nosec B310
            payload = json.loads(response.read(65536))
    except (HTTPError, OSError, URLError, ValueError) as exc:
        raise RuntimeError("fixture app token exchange failed") from exc
    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("fixture app token exchange returned no token")
    return access_token


def seed_scenario(settings: RuntimeSettings) -> tuple[dict[str, object] | None, dict[str, str] | None]:
    remove_fixture_artifacts(settings)
    if settings.scenario == DEFAULT_SCENARIO:
        return None, None
    if settings.scenario != APP_LIFECYCLE_SCENARIO:
        raise ValueError("unsupported E2E scenario")
    manifest, credentials = asyncio.run(
        _seed_app_lifecycle(
            settings.database_url,
            origin=settings.origin,
            fixture_origin=settings.fixture_origin,
        )
    )
    credentials[CREDENTIAL_VARIABLES[6]] = _exchange_fixture_app_token(
        settings.origin,
        credentials[CREDENTIAL_VARIABLES[5]],
    )
    return manifest, credentials


def write_fixture_artifacts(
    settings: RuntimeSettings,
    manifest: dict[str, object] | None,
    credentials: dict[str, str] | None,
) -> None:
    if manifest is None or credentials is None:
        return
    manifest_path, profile_path = fixture_artifact_paths(settings)
    manifest_content = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ) + "\n"
    profile_content = "".join(
        f"{name}={shlex.quote(credentials[name])}\n" for name in CREDENTIAL_VARIABLES
    )
    _atomic_write(manifest_path, manifest_content, 0o600)
    try:
        _atomic_write(profile_path, profile_content, 0o600)
    except BaseException:
        remove_fixture_artifacts(settings)
        raise


def ready_payload(settings: RuntimeSettings) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "ready",
        "origin": settings.origin,
        "fixture_origin": settings.fixture_origin,
        "reset_url": f"{settings.fixture_origin}/__e2e/reset",
        "scenario": settings.scenario,
    }
    if settings.scenario == APP_LIFECYCLE_SCENARIO:
        manifest_path, profile_path = fixture_artifact_paths(settings)
        payload.update(
            {
                "fixture_manifest": str(manifest_path),
                "credential_profile": str(profile_path),
                "credential_variables": list(CREDENTIAL_VARIABLES),
            }
        )
    return payload


def reset_payload(settings: RuntimeSettings) -> dict[str, object]:
    payload: dict[str, object] = {"ok": True, "scenario": settings.scenario}
    if settings.scenario == APP_LIFECYCLE_SCENARIO:
        manifest_path, profile_path = fixture_artifact_paths(settings)
        payload.update(
            {
                "fixture_manifest": str(manifest_path),
                "credential_profile": str(profile_path),
                "credential_variables": list(CREDENTIAL_VARIABLES),
            }
        )
    return payload


def write_ready_file(path: Path, payload: Mapping[str, object]) -> None:
    content = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n"
    _atomic_write(path, content, 0o600)


def remove_ready_file(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def render_database_reset_sql() -> str:
    """Return a schema-neutral reset for the externally provided CI database."""

    return """
DO $$
DECLARE
    table_name text;
BEGIN
    FOR table_name IN
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    LOOP
        EXECUTE format('TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE', table_name);
    END LOOP;
END
$$;
DROP SCHEMA IF EXISTS vector_index CASCADE;
""".strip()


async def _reset_database(database_url: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(database_url, timeout=10)
    try:
        await connection.execute(render_database_reset_sql())
    finally:
        await connection.close()


def reset_database(database_url: str) -> None:
    asyncio.run(_reset_database(database_url))


def clear_git_fixture_root(root: Path = GIT_FIXTURE_ROOT) -> None:
    """Empty only the runtime-owned fixture directory, preserving the root."""

    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    os.chmod(root, 0o700)


def _validate_origin(value: str, *, allow_ephemeral_port: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin must be a local HTTP origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin has an invalid port") from exc
    if port is None or (port == 0 and not allow_ephemeral_port):
        raise ValueError("origin must include a port")
    return value.rstrip("/")


def resolve_settings(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> RuntimeSettings:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=env.get("AKB_E2E_SCENARIO") or DEFAULT_SCENARIO)
    parser.add_argument("--ready-file", default=env.get("AKB_E2E_READY_FILE") or DEFAULT_READY_FILE)
    parser.add_argument(
        "--run-suites",
        action="store_true",
        help="run the repository-owned HTTP suite after readiness, then exit",
    )
    parser.add_argument(
        "--manage-postgres",
        action="store_true",
        help="own the repository's PostgreSQL-only Compose service for this run",
    )
    args = parser.parse_args(argv)

    scenario = args.scenario or DEFAULT_SCENARIO
    if scenario not in SUPPORTED_SCENARIOS:
        raise ValueError("unsupported E2E scenario")
    database_url = env.get("AKB_E2E_DATABASE_URL") or DEFAULT_DATABASE_URL
    database = parse_database_url(database_url)
    if args.manage_postgres:
        _validate_managed_database(database)
    s3_endpoint, s3_bucket, s3_access_key, s3_secret_key = _resolve_s3_settings(env)
    return RuntimeSettings(
        database_url=database_url,
        origin=_validate_origin(env.get("AKB_E2E_ORIGIN") or DEFAULT_ORIGIN),
        fixture_origin=_validate_origin(env.get("AKB_E2E_FIXTURE_ORIGIN") or DEFAULT_FIXTURE_ORIGIN),
        scenario=scenario,
        ready_file=Path(args.ready_file).expanduser().resolve(),
        run_suites=args.run_suites,
        manage_postgres=args.manage_postgres,
        compose_project=_resolve_compose_project(env.get("AKB_E2E_COMPOSE_PROJECT")),
        docker_argv=_resolve_docker_argv(env.get("AKB_E2E_DOCKER_ARGV")),
        s3_endpoint=s3_endpoint,
        s3_bucket=s3_bucket,
        s3_access_key=s3_access_key,
        s3_secret_key=s3_secret_key,
    )


def _http_get(url: str) -> tuple[int, bytes]:
    request = Request(url, method="GET")
    try:
        # Callers pass only fixed or _validate_origin-checked loopback URLs.
        with urlopen(request, timeout=2) as response:  # nosec B310
            return response.status, response.read(65536)
    except HTTPError as exc:
        return exc.code, b""
    except (OSError, URLError):
        return 0, b""


def _terminate_process_group(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    try:
        if process_group == os.getpgrp():
            process.terminate()
        else:
            os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if process_group == os.getpgrp():
                process.kill()
            else:
                os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=5)


class E2ERuntime:
    """Own the stub, backend, local fixture control plane, and their cleanup."""

    def __init__(self, settings: RuntimeSettings, repo_root: Path = REPO_ROOT):
        self.settings = settings
        self.repo_root = repo_root
        self.embed_process: subprocess.Popen[bytes] | None = None
        self.backend_process: subprocess.Popen[bytes] | None = None
        self.suite_process: subprocess.Popen[bytes] | None = None
        self.compose_started = False
        self.control_server: ThreadingHTTPServer | None = None
        self.control_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.reset_lock = threading.Lock()
        self.ready = False
        self.failed = False
        self.phase = "initialization"

    @property
    def backend_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            "backend",
            "--host",
            "127.0.0.1",
            "--port",
            str(BACKEND_PORT),
        ]

    @property
    def embed_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "scripts.ci.embed_stub:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(EMBED_PORT),
        ]

    def compose_argv(self, *arguments: str) -> list[str]:
        if not self.settings.manage_postgres:
            raise RuntimeError("PostgreSQL Compose is not enabled")
        project = _validate_compose_project(self.settings.compose_project)
        return [
            *self.settings.docker_argv,
            "compose",
            "--project-name",
            project,
            "--file",
            str(COMPOSE_FILE),
            *arguments,
        ]

    def _compose_environment(self) -> dict[str, str]:
        database = parse_database_url(self.settings.database_url)
        _validate_managed_database(database)
        environment = self._child_environment()
        environment["AKB_E2E_POSTGRES_PORT"] = str(database.port)
        return environment

    def _run_compose(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.compose_argv(*arguments),
            cwd=self.repo_root,
            env=self._compose_environment(),
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_docker(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.settings.docker_argv, *arguments],
            cwd=self.repo_root,
            env=self._compose_environment(),
            capture_output=True,
            text=True,
            check=False,
        )

    def _wait_for_postgres(self) -> None:
        for _ in range(60):
            if self.stop_event.is_set():
                raise RuntimeError("runtime stopped")
            containers = self._run_compose("ps", "-q", "postgres")
            container_id = containers.stdout.strip()
            if container_id:
                health = self._run_docker(
                    "inspect",
                    "--format",
                    "{{.State.Health.Status}}",
                    container_id,
                )
                if health.stdout.strip() == "healthy":
                    return
                if health.stdout.strip() in {"unhealthy", "exited", "dead"}:
                    break
            self.stop_event.wait(1)
        raise RuntimeError("PostgreSQL Compose service did not become healthy")

    def _compose_up(self) -> None:
        # Mark ownership before the command so partial `up` state is cleaned
        # by the same project-scoped `down` in the failure path.
        self.compose_started = True
        result = self._run_compose("up", "--detach")
        if result.returncode != 0:
            raise RuntimeError("PostgreSQL Compose startup failed")
        self._wait_for_postgres()

    def _compose_down(self) -> bool:
        if not self.compose_started:
            return True
        try:
            result = self._run_compose("down", "--volumes", "--remove-orphans")
        except Exception:
            self.compose_started = False
            return False
        self.compose_started = False
        return result.returncode == 0

    def _child_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key in (
            "AKB_E2E_DATABASE_URL",
            "AKB_E2E_S3_ACCESS_KEY",
            "AKB_E2E_S3_SECRET_KEY",
            *CREDENTIAL_VARIABLES,
        ):
            environment.pop(key, None)
        return environment

    def _start_process(self, command: list[str], cwd: Path, log_path: Path) -> subprocess.Popen[bytes]:
        with log_path.open("ab") as log:
            return subprocess.Popen(
                command,
                cwd=cwd,
                env=self._child_environment(),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _start_control_server(self) -> None:
        parsed = urlsplit(self.settings.fixture_origin)
        assert parsed.port is not None
        handler = type("E2EControlHandler", (_ControlHandler,), {"runtime": self})
        self.control_server = ThreadingHTTPServer(("127.0.0.1", parsed.port), handler)
        self.control_server.daemon_threads = True
        self.control_thread = threading.Thread(
            target=self.control_server.serve_forever,
            name="e2e-control",
            daemon=True,
        )
        self.control_thread.start()

    def _wait_for_embed(self) -> None:
        for _ in range(20):
            if self.stop_event.is_set():
                raise RuntimeError("runtime stopped")
            status, _ = _http_get("http://127.0.0.1:8888/healthz")
            if status == 200:
                return
            if self.embed_process is not None and self.embed_process.poll() is not None:
                break
            self.stop_event.wait(1)
        raise RuntimeError("embedding stub did not become ready")

    def _wait_for_backend(self) -> None:
        for _ in range(45):
            if self.stop_event.is_set():
                raise RuntimeError("runtime stopped")
            status, body = _http_get(f"{self.settings.origin}/readyz")
            if status == 200:
                try:
                    if isinstance(json.loads(body), dict) and "status" in json.loads(body):
                        return
                except (TypeError, ValueError):
                    pass
            if self.backend_process is not None and self.backend_process.poll() is not None:
                break
            self.stop_event.wait(2)
        raise RuntimeError("backend did not become ready")

    def _write_ready(self) -> None:
        write_ready_file(self.settings.ready_file, ready_payload(self.settings))
        self.ready = True

    def _start_backend(self) -> None:
        self.backend_process = self._start_process(self.backend_command, self.repo_root, BACKEND_LOG)
        self._wait_for_backend()

    def _stop_backend(self) -> None:
        self.ready = False
        remove_ready_file(self.settings.ready_file)
        _terminate_process_group(self.backend_process)
        self.backend_process = None

    def start(self) -> None:
        try:
            self.phase = "remove_stale_artifacts"
            remove_ready_file(self.settings.ready_file)
            remove_fixture_artifacts(self.settings)
            if self.settings.manage_postgres:
                self.phase = "postgres_start"
                self._compose_up()
            self.phase = "git_fixture_reset"
            clear_git_fixture_root()
            self.phase = "database_reset"
            reset_database(self.settings.database_url)
            self.phase = "config_write"
            write_runtime_config(self.settings, self.repo_root)
            self.phase = "control_plane_start"
            self._start_control_server()
            self.phase = "embedding_start"
            self.embed_process = self._start_process(self.embed_command, self.repo_root / "backend", EMBED_LOG)
            self.phase = "embedding_ready"
            self._wait_for_embed()
            self.phase = "backend_start"
            self._start_backend()
            self.phase = "seed_scenario"
            manifest, credentials = seed_scenario(self.settings)
            self.phase = "fixture_artifacts_write"
            write_fixture_artifacts(self.settings, manifest, credentials)
            self.phase = "ready_file_write"
            self._write_ready()
        except BaseException:
            self.shutdown()
            raise

    def reset(self) -> None:
        with self.reset_lock:
            self.phase = "backend_stop_for_reset"
            self._stop_backend()
            remove_fixture_artifacts(self.settings)
            try:
                self.phase = "database_reset"
                reset_database(self.settings.database_url)
                self.phase = "git_fixture_reset"
                clear_git_fixture_root()
                self.phase = "backend_restart"
                self._start_backend()
                self.phase = "seed_scenario"
                manifest, credentials = seed_scenario(self.settings)
                self.phase = "fixture_artifacts_write"
                write_fixture_artifacts(self.settings, manifest, credentials)
                self.phase = "ready_file_write"
                self._write_ready()
            except BaseException:
                self._stop_backend()
                raise

    def request_stop(self) -> None:
        self.stop_event.set()

    def suite_environment(self) -> dict[str, str]:
        database = parse_database_url(self.settings.database_url)
        environment = self._child_environment()
        if self.settings.manage_postgres:
            _validate_managed_database(database)
            pg_exec = shlex.join(self.compose_argv("exec", "-T", "postgres"))
            environment.pop("PGPASSWORD", None)
        else:
            pg_exec = f"env PGHOST={shlex.quote(database.host)} PGPORT={database.port}"
            environment["PGPASSWORD"] = database.password
        environment.update(
            {
                "AKB_URL": self.settings.origin,
                "AKB_PG_EXEC": pg_exec,
                "AKB_PG_USER": database.user,
                "AKB_PG_DB": database.name,
            }
        )
        return environment

    def run_suites(self) -> int:
        self.suite_process = subprocess.Popen(
            ["bash", str(self.repo_root / "backend/scripts/ci/run_e2e_suites.sh")],
            cwd=self.repo_root,
            env=self.suite_environment(),
            start_new_session=True,
        )
        try:
            while self.suite_process.poll() is None:
                if self.stop_event.wait(0.5):
                    _terminate_process_group(self.suite_process)
                    return 130
            return self.suite_process.returncode or 0
        finally:
            self.suite_process = None

    def wait(self) -> None:
        while not self.stop_event.wait(0.5):
            for process in (self.embed_process, self.backend_process):
                if process is not None and process.poll() is not None:
                    self.failed = True
                    self.stop_event.set()
                    break

    def shutdown(self) -> bool:
        cleanup_ok = True
        self.ready = False
        try:
            remove_ready_file(self.settings.ready_file)
            remove_fixture_artifacts(self.settings)
            if self.control_server is not None:
                self.control_server.shutdown()
                self.control_server.server_close()
                self.control_server = None
            if self.control_thread is not None:
                self.control_thread.join(timeout=5)
                self.control_thread = None
        except Exception:
            cleanup_ok = False
        for process in (self.suite_process, self.backend_process, self.embed_process):
            try:
                _terminate_process_group(process)
            except Exception:
                cleanup_ok = False
        self.backend_process = None
        self.embed_process = None
        if not self._compose_down():
            cleanup_ok = False
        return cleanup_ok

    def health_response(self) -> tuple[int, dict[str, object]]:
        healthy = self.ready and not self.stop_event.is_set()
        if self.backend_process is not None and self.backend_process.poll() is not None:
            healthy = False
        return (200 if healthy else 503), {"status": "ok" if healthy else "starting"}

    def ready_response(self) -> tuple[int, dict[str, object]]:
        if not self.ready:
            return 503, {"status": "starting"}
        return 200, ready_payload(self.settings)


class _ControlHandler(BaseHTTPRequestHandler):
    runtime: E2ERuntime

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: Mapping[str, object]) -> None:
        body = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/__e2e/health":
            status, payload = self.runtime.health_response()
            self._json(status, payload)
            return
        if path == "/__e2e/ready":
            status, payload = self.runtime.ready_response()
            self._json(status, payload)
            return
        self._json(404, {"status": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/__e2e/reset":
            try:
                self.runtime.reset()
            except BaseException:
                self._json(500, {"status": "reset_failed"})
                return
            self._json(200, reset_payload(self.runtime.settings))
            return
        if path == "/__e2e/stop":
            self.runtime.request_stop()
            self._json(202, {"status": "stopping"})
            return
        self._json(404, {"status": "not_found"})


_SAFE_FAILURE_LABEL = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")


def format_runtime_failure(phase: str, error: BaseException) -> str:
    """Return stable diagnostics without serializing exception messages."""

    safe_phase = phase if _SAFE_FAILURE_LABEL.fullmatch(phase) else "unknown"
    error_class = type(error).__name__
    safe_class = error_class if _SAFE_FAILURE_LABEL.fullmatch(error_class) else "Error"
    fields = [f"phase={safe_phase}", f"category={safe_class}"]
    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(sqlstate, str) and _SAFE_SQLSTATE.fullmatch(sqlstate):
        fields.append("source=postgres")
        fields.append(f"sqlstate={sqlstate}")
        for attribute, label in (
            ("constraint_name", "constraint"),
            ("table_name", "table"),
            ("column_name", "column"),
        ):
            value = getattr(error, attribute, None)
            if isinstance(value, str) and _SAFE_FAILURE_LABEL.fullmatch(value):
                fields.append(f"{label}={value}")
    return "e2e runtime failed " + " ".join(fields)


def main(argv: list[str] | None = None) -> int:
    try:
        settings = resolve_settings(argv)
        runtime = E2ERuntime(settings)
    except (ValueError, OSError):
        print("e2e runtime configuration failed", file=sys.stderr)
        return 2

    def handle_signal(_signum: int, _frame: Any) -> None:
        runtime.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    exit_code = 1
    try:
        runtime.start()
        if settings.run_suites:
            exit_code = runtime.run_suites()
        else:
            runtime.wait()
            exit_code = 1 if runtime.failed else 0
    except Exception as exc:
        print(format_runtime_failure(runtime.phase, exc), file=sys.stderr)
        exit_code = 1
    finally:
        if not runtime.shutdown() and exit_code == 0:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
