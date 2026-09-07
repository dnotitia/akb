"""Render demo configuration without changing AKB's YAML runtime contract."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import shlex

import yaml


def initialize_state(path: Path, env: dict[str, str]) -> None:
    """First-install inputs are persisted; restarts never regenerate credentials."""
    defaults = {
        "DB_PASSWORD": secrets.token_urlsafe(24),
        "SYSTEM_HMAC_SECRET": secrets.token_hex(32),
        "S3_ACCESS_KEY": "akb-allinone",
        "S3_SECRET_KEY": secrets.token_urlsafe(24),
        "DEMO_USERNAME": "demo",
        "DEMO_EMAIL": "demo@akb.local",
        "DEMO_PASSWORD": secrets.token_urlsafe(16),
        "DEMO_VAULT": "demo",
        "DEMO_PAT": "akb_" + secrets.token_urlsafe(32),
    }
    # Infrastructure credentials are generated, not changed through demo ENV.
    for key in defaults:
        if key.startswith("DEMO_") and env.get(key):
            defaults[key] = env[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as stream:
        stream.write(
            "".join(f"{key}={shlex.quote(value)}\n" for key, value in defaults.items())
        )


def load_mapping(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        raise ValueError(f"Invalid YAML in {path.name}; values omitted") from None
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise ValueError(f"{path.name} must contain a mapping with string keys")
    return data


def render(defaults: Path, overrides: Path, output: Path, env: dict[str, str]) -> None:
    app = load_mapping(defaults)
    private = {
        "db_password": env["DB_PASSWORD"],
        "system_hmac_secret": env["SYSTEM_HMAC_SECRET"],
        "s3_access_key": env["S3_ACCESS_KEY"],
        "s3_secret_key": env["S3_SECRET_KEY"],
    }
    # Preserve the existing docker-run interface; mounted YAML takes precedence.
    for field in (
        "embed_base_url",
        "embed_model",
        "embed_dimensions",
        "llm_base_url",
        "llm_model",
        "public_base_url",
    ):
        value = env.get(field.upper(), "")
        if value:
            if field == "embed_dimensions":
                try:
                    value = int(value)
                except ValueError:
                    raise ValueError("EMBED_DIMENSIONS must be an integer") from None
            app[field] = value
    for field in ("embed_api_key", "llm_api_key", "rerank_api_key", "vector_api_key"):
        private[field] = env.get(field.upper(), "")

    # This package owns local infrastructure and the demo identity. External
    # services/SSO belong in Compose or Kubernetes, not a partial demo override.
    fixed = {
        key: app[key]
        for key in (
            "db_host",
            "db_port",
            "db_name",
            "db_user",
            "git_storage_path",
            "auth_mode",
            "jwt_algorithm",
            "local_session_private_key_path",
            "local_session_jwks_path",
            "s3_endpoint_url",
            "s3_bucket",
            "redis_url",
        )
    } | {
        key: private[key]
        for key in (
            "db_password",
            "system_hmac_secret",
            "s3_access_key",
            "s3_secret_key",
        )
    }
    for name, target in (("app.yaml", app), ("secret.yaml", private)):
        path = overrides / name
        if not path.exists():
            continue
        values = load_mapping(path)
        for key, value in values.items():
            if key in fixed and value != fixed[key]:
                raise ValueError(f"{key} is owned by the demo bootstrap")
        target.update(values)
    if not app.get("s3_public_url"):
        app["s3_public_url"] = app["public_base_url"]
    output.mkdir(parents=True, exist_ok=True)
    # No regex/substitution: quotes, newlines and backslashes in credentials
    # retain their exact value. Open secret output with private permissions.
    for name, data in (("app.yaml", app), ("secret.yaml", private)):
        fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            yaml.safe_dump(data, stream, sort_keys=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("state", "render"))
    args = parser.parse_args()
    try:
        if args.action == "state":
            initialize_state(Path("/var/lib/akb/state.env"), dict(os.environ))
        else:
            render(
                Path("/opt/akb/app.yaml"),
                Path("/etc/akb-overrides"),
                Path("/etc/akb"),
                dict(os.environ),
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
