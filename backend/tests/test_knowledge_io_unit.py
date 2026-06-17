"""Unit tests for the knowledge-bundle export/import dispatch layer.

Covers the pure pieces of `app.services.knowledge_io` — zip (de)serialisation
round-trip, the runaway-archive guards, and the format registry — without a DB
(export_vault / import_bundle need one and are exercised by the e2e suite
`test_okf_export_import_e2e.sh`).
"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.services import knowledge_io


class TestZipRoundTrip:
    def test_bundle_to_zip_and_back(self):
        bundle = {
            "index.md": "---\nokf_version: '0.1'\n---\n# x\n",
            "specs/api.md": "---\ntype: spec\n---\n# API\n",
        }
        data = knowledge_io.bundle_to_zip(bundle)
        assert data[:2] == b"PK"  # zip magic
        out = knowledge_io.zip_to_bundle(data)
        assert out == bundle

    def test_zip_is_deterministic(self):
        bundle = {"b.md": "B", "a.md": "A"}
        assert knowledge_io.bundle_to_zip(bundle) == knowledge_io.bundle_to_zip(bundle)

    def test_zip_skips_directories_and_traversal(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("sub/", "")          # directory entry
            zf.writestr("../evil.md", "x")   # traversal
            zf.writestr("ok.md", "y")
        out = knowledge_io.zip_to_bundle(buf.getvalue())
        assert out == {"ok.md": "y"}

    def test_too_many_files_rejected(self, monkeypatch):
        monkeypatch.setattr(knowledge_io, "_MAX_BUNDLE_FILES", 2)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(3):
                zf.writestr(f"f{i}.md", "x")
        with pytest.raises(ValueError, match="too many"):
            knowledge_io.zip_to_bundle(buf.getvalue())

    def test_oversize_rejected(self, monkeypatch):
        monkeypatch.setattr(knowledge_io, "_MAX_BUNDLE_BYTES", 4)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("big.md", "way more than four bytes")
        with pytest.raises(ValueError, match="maximum uncompressed size"):
            knowledge_io.zip_to_bundle(buf.getvalue())


class TestFormatRegistry:
    def test_unsupported_format_rejected(self):
        with pytest.raises(ValueError, match="Unsupported format"):
            knowledge_io._require_format("rdf")

    def test_okf_supported(self):
        assert knowledge_io._require_format("okf") == "okf"
