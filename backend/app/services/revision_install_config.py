"""Offline revision configuration preparation for installations and upgrades.

This module deliberately does not import app.config: preparation precedes a
loadable application configuration. It neither claims nor checks a database;
initialize-postgres-native must independently prove the database is never used.
"""

from __future__ import annotations

import math
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml


class NativeInstallConfigError(ValueError):
    """A value-free error safe to display even when configuration has secrets."""


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise NativeInstallConfigError("Configuration keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


_IDENTITY_FIELDS = (
    "document_revision_tenant_id",
    "document_revision_namespace",
    "document_revision_database_id",
    "document_revision_runtime_image_digest",
)


def _check_values(value: Any, ancestors: frozenset[int] = frozenset()) -> None:
    if type(value) in (str, bool, int, type(None)):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) not in (dict, list) or id(value) in ancestors:
        raise NativeInstallConfigError("Configuration must contain finite, non-recursive YAML values")
    descendants = ancestors | {id(value)}
    for child in (value.values() if isinstance(value, dict) else value):
        _check_values(child, descendants)


def _read_mapping(path: Path, *, output: bool = False) -> dict[str, Any]:
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK
        if output:
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise NativeInstallConfigError("Configuration must be a regular file")
            loader = _UniqueKeyLoader(stream)
            try:
                value = loader.get_single_data()
            finally:
                loader.dispose()
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as from_error:
        raise NativeInstallConfigError("Cannot read valid configuration YAML") from from_error
    if not isinstance(value, dict):
        raise NativeInstallConfigError("Configuration must be a YAML mapping")
    _check_values(value)
    return value


def _validate_template(source: dict[str, Any], secret: dict[str, Any]) -> None:
    if source.get("document_revision_backend", "bare_git") not in (
        "bare_git", "bare_git_current", "postgres_native"
    ):
        raise NativeInstallConfigError("Source must be a fresh-install Bare Git or Native template")
    if any(source.get(field) not in (None, "") for field in _IDENTITY_FIELDS):
        raise NativeInstallConfigError("Source already contains revision identity; use the original fresh-install template")
    if (
        secret.get("db_name", source.get("db_name")) == "akb_revision_m1_measurement"
        or source.get("native_revision_m1_measurement_only", False) is not False
        or source.get("native_revision_m1_file_driver", "s3_current") != "s3_current"
        or source.get("native_revision_m1_file_fscas_root", "") not in (None, "")
    ):
        raise NativeInstallConfigError("Native installation rejects measurement configuration")


def _same_mapping(left: Any, right: Any) -> bool:
    # Ignore YAML anchor presentation, but preserve scalar types: True != 1.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_mapping(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_mapping(a, b) for a, b in zip(left, right))
    return bool(left == right)


def _reuse(output: Path, desired: dict[str, Any], *, generate_identity: bool) -> dict[str, str]:
    existing = _read_mapping(output, output=True)
    expected = desired
    report = {"status": "reused"}
    if generate_identity:
        database_id = existing.get("document_revision_database_id")
        try:
            if not isinstance(database_id, str) or str(uuid.UUID(database_id)) != database_id:
                raise ValueError()
        except ValueError as exc:
            raise NativeInstallConfigError("Existing output has no valid database identity") from exc
        expected = {**desired, "document_revision_database_id": database_id}
        report["database_id"] = database_id
    if not _same_mapping(existing, expected):
        raise NativeInstallConfigError("Existing output differs from the requested configuration; it was not modified")
    return report


def _read_inputs(source: str | Path, secret: str | Path, output: str | Path) -> tuple[dict, dict, Path]:
    output_path = Path(output)
    try:
        for input_path in (Path(source), Path(secret)):
            if input_path.resolve() == output_path.resolve() or (
                output_path.exists() and os.path.samefile(input_path, output_path)
            ):
                raise NativeInstallConfigError("Source, secret, and output must not alias the output file")
        source_data, secret_data = _read_mapping(Path(source)), _read_mapping(Path(secret))
    except (OSError, RuntimeError) as exc:
        raise NativeInstallConfigError("Cannot read source and secret configuration") from exc
    if any(key.startswith(("document_revision_", "native_revision_m1_")) for key in secret_data):
        raise NativeInstallConfigError("Secret configuration must not override revision controls")
    return source_data, secret_data, output_path


def _publish(output: Path, desired: dict[str, Any], *, generate_identity: bool = False) -> dict[str, str]:
    try:
        if output.exists() or output.is_symlink():
            return _reuse(output, desired, generate_identity=generate_identity)
        prepared = dict(desired)
        report = {"status": "prepared"}
        if generate_identity:
            database_id = str(uuid.uuid4())
            prepared["document_revision_database_id"] = database_id
            report["database_id"] = database_id
        descriptor, temporary = tempfile.mkstemp(prefix=".revision-config-", dir=output.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                yaml.safe_dump(prepared, stream, sort_keys=False, allow_unicode=True)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # link is exclusive, and readers only see the completed file.
                # A competing writer's identity wins; never replace its output.
                os.link(temporary, output)
            except FileExistsError:
                return _reuse(output, desired, generate_identity=generate_identity)
        finally:
            os.unlink(temporary)
    except (OSError, RuntimeError) as exc:
        raise NativeInstallConfigError("Cannot prepare configuration; source and output were not overwritten") from exc
    return report


def prepare_native_config(
    *, source: str | Path, secret: str | Path, output: str | Path, tenant_id: str, namespace: str, image_digest: str
) -> dict[str, str]:
    """Publish a new private config atomically, or reuse an identical request."""
    if not tenant_id.strip() or tenant_id != tenant_id.strip() or not tenant_id.isprintable():
        raise NativeInstallConfigError("Tenant ID must be non-empty and have no surrounding whitespace or controls")
    # Keep in sync with app.config.is_dns1123_namespace without importing its
    # process-global settings loader before a config exists.
    if not namespace or len(namespace) > 253 or any(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
        for label in namespace.split(".")
    ):
        raise NativeInstallConfigError("Namespace must use DNS-1123 form")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None:
        raise NativeInstallConfigError("Image digest must use sha256:<64 lowercase hex> form")
    template, secret_data, output_path = _read_inputs(source, secret, output)
    _validate_template(template, secret_data)
    desired = {
        **template,
        "document_revision_backend": "postgres_native",
        "document_revision_tenant_id": tenant_id,
        "document_revision_namespace": namespace,
        "document_revision_runtime_image_digest": image_digest,
    }
    return _publish(output_path, desired, generate_identity=True)


def preserve_revision_config(*, source: str | Path, secret: str | Path, output: str | Path) -> dict[str, str]:
    """Pin an omitted legacy selector without changing explicit identities."""
    desired, _secret_data, output_path = _read_inputs(source, secret, output)
    desired.setdefault("document_revision_backend", "bare_git")
    if desired["document_revision_backend"] not in (
        "bare_git", "bare_git_current", "postgres_native", "native_ledger_m1"
    ):
        raise NativeInstallConfigError("Source must use a supported revision backend")
    return _publish(output_path, desired)
