from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from app import cli
from app.services.revision_install_config import (
    NativeInstallConfigError,
    prepare_native_config,
    preserve_revision_config,
)


DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def request_config(tmp_path):
    source, secret = tmp_path / "source.yaml", tmp_path / "secret.yaml"
    source.write_text("db_name: fresh\ndocument_revision_backend: bare_git\ncustom: {enabled: true}\n")
    secret.write_text("db_password: do-not-print-this-password\n")
    return {
        "source": source,
        "secret": secret,
        "output": tmp_path / "native.yaml",
        "tenant_id": "tenant-example",
        "namespace": "tenant-example.test",
        "image_digest": DIGEST,
    }


def test_prepare_is_explicit_private_and_repeatable(request_config):
    source_bytes = request_config["source"].read_bytes()
    secret_bytes = request_config["secret"].read_bytes()
    first = prepare_native_config(**request_config)
    output = request_config["output"]
    output_bytes = output.read_bytes()
    data = yaml.safe_load(output_bytes)
    assert data["document_revision_backend"] == "postgres_native"
    assert data["document_revision_tenant_id"] == "tenant-example"
    assert data["document_revision_namespace"] == "tenant-example.test"
    assert data["document_revision_runtime_image_digest"] == DIGEST
    assert data["custom"] == {"enabled": True}
    assert "db_password" not in data
    assert uuid.UUID(data["document_revision_database_id"]).version == 4
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert first == {"status": "prepared", "database_id": data["document_revision_database_id"]}
    assert prepare_native_config(**request_config) == {**first, "status": "reused"}
    assert output.read_bytes() == output_bytes
    assert request_config["source"].read_bytes() == source_bytes
    assert request_config["secret"].read_bytes() == secret_bytes


def test_reuse_compares_mapping_not_yaml_anchor_presentation(request_config):
    request_config["source"].write_text("first: &values [one, two]\nsecond: *values\n")
    first = prepare_native_config(**request_config)
    data = yaml.safe_load(request_config["output"].read_text())
    request_config["output"].write_text(json.dumps(data))
    before = request_config["output"].read_bytes()
    assert prepare_native_config(**request_config) == {**first, "status": "reused"}
    assert request_config["output"].read_bytes() == before


@pytest.mark.parametrize("command", ["prepare-native-config", "preserve-revision-config"])
def test_cli_works_without_config_database_or_network(request_config, tmp_path, command):
    cwd = tmp_path / "configless"
    cwd.mkdir()
    args = [command]
    fields = ["source", "secret", "output"]
    if command == "prepare-native-config":
        fields += ["tenant_id", "namespace", "image_digest"]
    for field in fields:
        args.extend(["--" + field.replace("_", "-"), str(request_config[field])])
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    # Tripwires prove offline dispatch never tries to import Settings or a DB
    # module, even if the calling pytest session already loaded either one.
    code = """
import importlib.abc
import runpy
import sys
class NoApplicationSettings(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'app.config' or fullname.startswith('app.db'):
            raise AssertionError('offline command imported settings/database')
sys.meta_path.insert(0, NoApplicationSettings())
def audit(event, args):
    if event.startswith('socket.connect'):
        raise AssertionError('offline command connected to network')
sys.addaudithook(audit)
sys.argv = ['app.cli', *sys.argv[1:]]
runpy.run_module('app.cli', run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-c", code, *args], cwd=cwd, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "prepared"
    assert "do-not-print-this-password" not in result.stdout + result.stderr
    assert not (cwd / "config").exists()


@pytest.mark.parametrize(("field", "value"), [
    ("tenant_id", ""), ("tenant_id", " tenant"), ("tenant_id", "tenant\nvalue"),
    ("namespace", "Bad_namespace"), ("namespace", "a" * 64), ("namespace", "a..b"),
    ("namespace", "-bad"), ("namespace", "bad-"), ("namespace", "a."),
    ("image_digest", "latest"), ("image_digest", "sha256:" + "A" * 64),
])
def test_invalid_identity_does_not_write(request_config, field, value):
    with pytest.raises(NativeInstallConfigError):
        prepare_native_config(**{**request_config, field: value})
    assert not request_config["output"].exists()


@pytest.mark.parametrize("text", [
    "secret: [", "[]", "null", "key: first\nkey: secret\n", "nested: {key: 1, key: 2}",
    "value: &loop [*loop]", "value: .nan", "value: !!python/object:bad {}", "{1: value}",
])
@pytest.mark.parametrize("field", ["source", "secret"])
def test_malformed_yaml_fails_without_exposing_values(request_config, text, field):
    request_config[field].write_text(text)
    with pytest.raises(NativeInstallConfigError) as error:
        prepare_native_config(**request_config)
    assert text not in str(error.value)
    assert not request_config["output"].exists()


@pytest.mark.parametrize("source", [
    {"document_revision_backend": "native_ledger_m1"},
    {"document_revision_backend": "unknown"},
    {"db_name": "akb_revision_m1_measurement"},
    {"native_revision_m1_measurement_only": True},
    {"native_revision_m1_measurement_only": "false"},
    {"native_revision_m1_file_driver": "fscas"},
    {"native_revision_m1_file_fscas_root": "/measurement"},
    {"document_revision_backend": "postgres_native", "document_revision_database_id": str(uuid.uuid4())},
    {"document_revision_tenant_id": "existing"},
    {"document_revision_namespace": "existing"},
    {"document_revision_runtime_image_digest": DIGEST},
])
def test_preexisting_identity_or_measurement_is_not_repurposed(request_config, source):
    request_config["source"].write_text(yaml.safe_dump(source))
    with pytest.raises(NativeInstallConfigError):
        prepare_native_config(**request_config)
    assert not request_config["output"].exists()


@pytest.mark.parametrize("key", [
    "document_revision_backend", "document_revision_database_id", "document_revision_runtime_image_digest",
    "document_revision_tenant_id", "document_revision_namespace", "native_revision_m1_measurement_only",
    "native_revision_m1_file_driver",
])
@pytest.mark.parametrize("native", [True, False])
def test_secret_revision_controls_are_rejected(request_config, key, native):
    request_config["secret"].write_text(yaml.safe_dump({key: "do-not-print-this-password"}))
    with pytest.raises(NativeInstallConfigError, match="must not override"):
        if native:
            prepare_native_config(**request_config)
        else:
            preserve_revision_config(**{k: request_config[k] for k in ("source", "secret", "output")})
    assert not request_config["output"].exists()


def test_effective_secret_measurement_database_rejected(request_config):
    request_config["secret"].write_text("db_name: akb_revision_m1_measurement\n")
    with pytest.raises(NativeInstallConfigError, match="measurement"):
        prepare_native_config(**request_config)
    assert not request_config["output"].exists()


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink", "secret"])
def test_output_cannot_alias_input(request_config, alias):
    source_bytes = request_config["source"].read_bytes()
    secret_bytes = request_config["secret"].read_bytes()
    if alias == "same":
        request_config["output"] = request_config["source"]
    elif alias == "secret":
        request_config["output"] = request_config["secret"]
    elif alias == "symlink":
        request_config["output"].symlink_to(request_config["source"])
    else:
        os.link(request_config["source"], request_config["output"])
    with pytest.raises(NativeInstallConfigError, match="alias"):
        prepare_native_config(**request_config)
    assert request_config["source"].read_bytes() == source_bytes
    assert request_config["secret"].read_bytes() == secret_bytes


@pytest.mark.parametrize("change", ["tenant", "image", "source", "output", "output-type"])
def test_mismatch_preserves_existing_output(request_config, change):
    prepare_native_config(**request_config)
    if change == "tenant":
        request_config["tenant_id"] = "other"
    elif change == "image":
        request_config["image_digest"] = "sha256:" + "b" * 64
    elif change == "source":
        request_config["source"].write_text("db_name: changed\n")
    elif change == "output":
        request_config["output"].write_text("document_revision_database_id: invalid\n")
    else:
        data = yaml.safe_load(request_config["output"].read_text())
        data["custom"]["enabled"] = 1
        request_config["output"].write_text(yaml.safe_dump(data))
    before = request_config["output"].read_bytes()
    with pytest.raises(NativeInstallConfigError):
        prepare_native_config(**request_config)
    assert request_config["output"].read_bytes() == before


@pytest.mark.parametrize("different", [False, True])
def test_competing_writes_publish_one_complete_config(request_config, monkeypatch, different):
    link = os.link
    barrier = threading.Barrier(2)

    def race(source, destination):
        barrier.wait(timeout=10)
        return link(source, destination)

    monkeypatch.setattr(os, "link", race)

    def prepare(tenant):
        try:
            return prepare_native_config(**{**request_config, "tenant_id": tenant})
        except NativeInstallConfigError:
            return {"status": "rejected"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(prepare, "first"), executor.submit(prepare, "second" if different else "first")]
        results = [future.result() for future in futures]
    assert sorted(r["status"] for r in results) == ["prepared", "rejected" if different else "reused"]
    data = yaml.safe_load(request_config["output"].read_text())
    assert data["document_revision_backend"] == "postgres_native"
    winner = next(r for r in results if r["status"] == "prepared")
    assert data["document_revision_database_id"] == winner["database_id"]
    if not different:
        assert results[0]["database_id"] == results[1]["database_id"]
    assert not list(request_config["output"].parent.glob(".revision-config-*"))


@pytest.mark.parametrize("backend", [None, "bare_git", "bare_git_current", "postgres_native", "native_ledger_m1"])
def test_preserve_pins_omission_and_keeps_explicit_identity(request_config, backend):
    source = {
        "db_name": "existing",
        "document_revision_database_id": "11111111-1111-4111-8111-111111111111",
        "document_revision_runtime_image_digest": DIGEST,
        "document_revision_tenant_id": "existing",
        "document_revision_namespace": "existing",
    }
    if backend is not None:
        source["document_revision_backend"] = backend
    request_config["source"].write_text(yaml.safe_dump(source))
    args = {k: request_config[k] for k in ("source", "secret", "output")}
    assert preserve_revision_config(**args) == {"status": "prepared"}
    before = request_config["output"].read_bytes()
    assert yaml.safe_load(before) == {**source, "document_revision_backend": backend or "bare_git"}
    assert preserve_revision_config(**args) == {"status": "reused"}
    assert request_config["output"].read_bytes() == before
    request_config["source"].write_text("document_revision_backend: unknown\n")
    with pytest.raises(NativeInstallConfigError):
        preserve_revision_config(**args)
    assert request_config["output"].read_bytes() == before


def test_cli_errors_do_not_echo_yaml_or_unrecognized_argument(request_config, capsys):
    args = ["prepare-native-config"]
    for key, value in request_config.items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    request_config["source"].write_text("db_password: [do-not-print-this-password\n")
    assert cli.main(args) == 1
    assert "do-not-print-this-password" not in capsys.readouterr().err
    assert cli.main([*args, "--do-not-print-this-password"]) == 2
    assert "do-not-print-this-password" not in capsys.readouterr().err


def _isolated_selection(tmp_path, values):
    """Load real Settings in a fresh process, outside pytest's legacy config."""
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "app.yaml").write_text(yaml.safe_dump({"auth_mode": "local", **values}))
    code = """
import json
from app.config import settings
from app.services import revision_backend
from app.services.native_document_service import NativeDocumentService

def no_git(*args, **kwargs):
    raise AssertionError("default-selected Native must never fall back to Git")

revision_backend.GitService = no_git
revision_backend.LegacyRevisionBackend = no_git
service = revision_backend.get_document_service()
assert isinstance(service, NativeDocumentService)
assert revision_backend.get_document_service() is service
print(json.dumps({"backend": settings.document_revision_backend,
                  "database_id": str(settings.document_revision_database_id)}))
"""
    return subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True, text=True, timeout=30,
    )


def test_prepared_example_and_omitted_selector_choose_native_in_real_process(tmp_path):
    source = Path(__file__).resolve().parents[2] / "config" / "app.yaml.example"
    raw = yaml.safe_load(source.read_text())
    assert raw["document_revision_backend"] == "postgres_native"
    assert not raw.get("document_revision_database_id")
    secret = tmp_path / "secret.yaml"
    secret.write_text("{}\n")
    output = tmp_path / "prepared.yaml"
    receipt = prepare_native_config(
        source=source, secret=secret, output=output,
        tenant_id="fresh", namespace="fresh", image_digest=DIGEST,
    )
    values = yaml.safe_load(output.read_text())
    # No fixture or monkeypatch supplies the selector in this process.
    values.pop("document_revision_backend")
    first = _isolated_selection(tmp_path, values)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {
        "backend": "postgres_native", "database_id": receipt["database_id"],
    }
    second = _isolated_selection(tmp_path, values)
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout


def test_unprepared_omitted_selector_fails_before_backend_composition(tmp_path):
    result = _isolated_selection(tmp_path, {})
    assert result.returncode != 0
    assert "postgres_native requires document_revision_tenant_id" in result.stderr
    assert "default-selected Native must never fall back" not in result.stderr


def test_preserved_omitted_selector_loads_as_legacy_after_default_change(tmp_path):
    from app.config import Settings

    source, secret, output = (tmp_path / name for name in ("old.yaml", "secret.yaml", "preserved.yaml"))
    source.write_text("auth_mode: local\ndb_name: existing\n")
    secret.write_text("{}\n")
    preserve_revision_config(source=source, secret=secret, output=output)
    configured = Settings.model_validate(yaml.safe_load(output.read_text()))
    assert configured.document_revision_backend == "bare_git"
    assert configured.document_revision_database_id is None
    assert source.read_text() == "auth_mode: local\ndb_name: existing\n"
