"""Unit tests for TemplateRegistry."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Point the registry at a temp directory and re-scan."""
    from app.services import template_registry as tr
    monkeypatch.setattr(tr, "_TEMPLATES_DIR", tmp_path)
    tr._scan()
    return tr


def _write(tmp_path: Path, name: str, payload: dict) -> None:
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def test_list_summaries_returns_sorted_by_display_name(isolated_registry, tmp_path):
    _write(tmp_path, "z", {"name": "z", "display_name": "Zeta",
                            "description": "", "collections": [{"path": "a"}]})
    _write(tmp_path, "a", {"name": "a", "display_name": "Alpha",
                            "description": "", "collections": [{"path": "x"}]})
    isolated_registry._scan()
    names = [s.display_name for s in isolated_registry.list_summaries()]
    assert names == ["Alpha", "Zeta"]


def test_get_returns_full_payload_with_guide(isolated_registry, tmp_path):
    _write(tmp_path, "eng", {
        "name": "eng", "display_name": "Engineering", "description": "",
        "collections": [{"path": "specs", "guide": "spec guide"}],
    })
    isolated_registry._scan()
    payload = isolated_registry.get("eng")
    assert payload is not None
    assert payload["collections"][0]["guide"] == "spec guide"


def test_malformed_yaml_is_skipped_not_raised(isolated_registry, tmp_path):
    (tmp_path / "bad.yaml").write_text(":\n- this is\nnot: valid: yaml\n", encoding="utf-8")
    _write(tmp_path, "good", {"name": "good", "display_name": "Good",
                                "description": "", "collections": [{"path": "x"}]})
    isolated_registry._scan()
    assert isolated_registry.list_names() == ["good"]


def test_missing_collections_field_is_skipped(isolated_registry, tmp_path):
    _write(tmp_path, "empty", {"name": "empty", "display_name": "Empty",
                                 "description": "no collections", "collections": []})
    _write(tmp_path, "good", {"name": "good", "display_name": "Good",
                                "description": "", "collections": [{"path": "x"}]})
    isolated_registry._scan()
    assert isolated_registry.list_names() == ["good"]


def test_empty_dir_yields_empty_lists(isolated_registry):
    assert isolated_registry.list_summaries() == []
    assert isolated_registry.list_names() == []
    assert isolated_registry.get("anything") is None


def test_missing_dir_does_not_raise(tmp_path, monkeypatch):
    from app.services import template_registry as tr
    monkeypatch.setattr(tr, "_TEMPLATES_DIR", tmp_path / "does-not-exist")
    tr._scan()  # must not raise
    assert tr.list_names() == []


def test_collection_summary_falls_back_to_path(isolated_registry, tmp_path):
    """When collection entry omits 'name', summary uses 'path' as fallback."""
    _write(tmp_path, "x", {"name": "x", "display_name": "X",
                             "description": "", "collections": [{"path": "specs"}]})
    isolated_registry._scan()
    summary = isolated_registry.list_summaries()[0]
    assert summary.collections[0].path == "specs"
    assert summary.collections[0].name == "specs"
