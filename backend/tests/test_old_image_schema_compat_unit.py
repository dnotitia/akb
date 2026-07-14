from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _compat_module():
    path = Path(__file__).with_name("old_image_schema_compat.py")
    spec = importlib.util.spec_from_file_location("old_image_schema_compat", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_old_backend_activation_isolates_relative_config(tmp_path, monkeypatch):
    current = tmp_path / "current"
    archived = tmp_path / "archived" / "backend"
    (current / "config").mkdir(parents=True)
    (archived / "config").mkdir(parents=True)
    (current / "config" / "app.yaml").write_text("source: current\n")
    (archived / "config" / "app.yaml").write_text("source: archived\n")
    (archived / "config" / "secret.yaml").write_text("secret: archived\n")
    monkeypatch.chdir(current)
    monkeypatch.setattr(sys, "path", sys.path.copy())

    module = _compat_module()
    module._activate_old_backend(archived)

    assert Path.cwd() == archived.resolve()
    assert Path("config/app.yaml").read_text() == "source: archived\n"


def test_old_backend_activation_materializes_archive_examples(tmp_path, monkeypatch):
    current = tmp_path / "current"
    archived = tmp_path / "archive" / "backend"
    examples = archived.parent / "config"
    current.mkdir()
    archived.mkdir(parents=True)
    examples.mkdir()
    (examples / "app.yaml.example").write_text("source: archived-example\n")
    (examples / "secret.yaml.example").write_text("secret: test-only\n")
    monkeypatch.chdir(current)
    monkeypatch.setattr(sys, "path", sys.path.copy())

    module = _compat_module()
    module._activate_old_backend(archived)

    assert Path("config/app.yaml").read_text() == "source: archived-example\n"
    assert Path("config/secret.yaml").read_text() == "secret: test-only\n"
