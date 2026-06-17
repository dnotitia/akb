"""Unit tests for the OKF (Open Knowledge Format) interop module.

Covers both halves of `app.services.okf` with no DB/git/network:
  * export — AKB document/table/file records → OKF concept docs + the reserved
    index.md / log.md, plus the round-trip invariant (anything we export must
    pass our own conformance checker).
  * conformance — the three OKF v0.1 MUST rules, and the *permissive* clauses
    (unknown type, unknown keys, broken links, missing index.md never fail).

Imports from `app.services.okf` only — pure, runs as a fast unit test.
"""
from __future__ import annotations

from app.services.okf import (
    OKF_VERSION,
    build_bundle,
    build_log,
    check_bundle,
    concept_from_document,
    concept_from_file,
    concept_from_table,
    okf_frontmatter,
    split_frontmatter,
)

# A document record shaped like akb_get's response.
DOC = {
    "uri": "akb://product/coll/akb/design/doc/api-v2.md",
    "path": "akb/design/api-v2.md",
    "title": "API v2 Design",
    "type": "spec",
    "status": "active",
    "summary": "The v2 surface.",
    "domain": "product",
    "created_at": "2026-06-12 01:36:48.823107+00:00",
    "updated_at": "2026-06-13 09:00:00+00:00",
    "tags": ["api", "design"],
    "content": "# API v2\n\nBody here.",
}


class TestFrontmatterMapping:
    def test_required_type_always_present(self):
        meta = okf_frontmatter(type_="")
        assert meta["type"] == "note"  # empty → fallback, never absent

    def test_akb_keys_remapped_to_okf_names(self):
        path, md = concept_from_document(DOC)
        meta, body, had = split_frontmatter(md)
        assert had and meta is not None
        # OKF recommended names:
        assert meta["type"] == "spec"
        assert meta["title"] == "API v2 Design"
        assert meta["description"] == "The v2 surface."   # was AKB `summary`
        assert meta["resource"] == DOC["uri"]
        assert meta["tags"] == ["api", "design"]
        assert meta["timestamp"] == "2026-06-13T09:00:00+00:00"  # was `updated_at`
        # AKB extras preserved as additional keys (consumer must not reject):
        assert meta["status"] == "active"
        assert meta["domain"] == "product"
        assert meta["akb_uri"] == DOC["uri"]
        assert body.strip() == "# API v2\n\nBody here."

    def test_key_order_is_spec_order(self):
        _, md = concept_from_document(DOC)
        meta, _, _ = split_frontmatter(md)
        keys = list(meta)
        assert keys[:6] == ["type", "title", "description", "resource", "tags", "timestamp"]

    def test_concept_id_is_path_minus_md(self):
        path, _ = concept_from_document(DOC)
        assert path == "akb/design/api-v2.md"  # concept ID == "akb/design/api-v2"


class TestTableAndFileConcepts:
    def test_table_renders_schema_not_rows(self):
        rec = {
            "uri": "akb://product/coll/akb/table/orders",
            "path": "akb/orders.md",
            "name": "orders",
            "sql_name": "vt_orders",
            "row_count": 1200,
            "columns": [
                {"name": "id", "type": "uuid", "description": "PK"},
                {"name": "total", "type": "numeric", "description": ""},
            ],
        }
        _, md = concept_from_table(rec)
        meta, body, _ = split_frontmatter(md)
        assert meta["type"] == "table"
        assert meta["resource"] == rec["uri"]
        assert "# Schema" in body
        assert "| id | uuid | PK |" in body
        assert "1200 rows" in body  # row count noted, data not embedded

    def test_file_concept_references_asset(self):
        rec = {
            "uri": "akb://product/file/reports/q2.pdf",
            "path": "reports/q2.pdf",
            "name": "q2.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 4096,
        }
        path, md = concept_from_file(rec)
        meta, body, _ = split_frontmatter(md)
        assert path.endswith(".md")  # binary asset → .md concept doc
        assert meta["type"] == "file"
        assert meta["resource"] == rec["uri"]
        assert meta["mime_type"] == "application/pdf"
        assert rec["uri"] in body


class TestBundleAndReservedFiles:
    def test_bundle_has_root_index_with_version(self):
        bundle = build_bundle(documents=[DOC])
        assert "index.md" in bundle
        meta, _, had = split_frontmatter(bundle["index.md"])
        assert had and meta["okf_version"] == OKF_VERSION

    def test_index_links_are_absolute(self):
        bundle = build_bundle(documents=[DOC])
        assert "(/akb/design/api-v2.md)" in bundle["index.md"]

    def test_log_dates_are_iso(self):
        bundle = build_bundle(documents=[DOC])
        assert "## 2026-06-13" in bundle["log.md"]


class TestConformance:
    def test_exported_bundle_is_conformant(self):
        rec_file = {"uri": "akb://v/file/a.bin", "path": "a.bin", "name": "a.bin"}
        rec_table = {"uri": "akb://v/table/t", "path": "t.md", "name": "t",
                     "columns": [{"name": "x", "type": "int"}]}
        bundle = build_bundle(documents=[DOC], tables=[rec_table], files=[rec_file])
        report = check_bundle(bundle)
        assert report.ok, "\n".join(str(f) for f in report.findings)

    def test_missing_frontmatter_is_error(self):
        report = check_bundle({"a.md": "# No frontmatter here\n"})
        assert not report.ok
        assert any(f.code == "okf.frontmatter.missing" for f in report.findings)

    def test_empty_type_is_error(self):
        report = check_bundle({"a.md": "---\ntitle: x\ntype: ''\n---\nbody\n"})
        assert not report.ok
        assert any(f.code == "okf.type.missing" for f in report.findings)

    def test_unparseable_frontmatter_is_error(self):
        report = check_bundle({"a.md": "---\n: : :\nbad yaml\n---\nbody\n"})
        assert not report.ok

    def test_nonroot_index_frontmatter_is_error(self):
        report = check_bundle({"sub/index.md": "---\nokf_version: '0.1'\n---\n# S\n"})
        assert any(f.code == "okf.index.frontmatter" for f in report.findings)

    def test_root_index_frontmatter_is_allowed(self):
        report = check_bundle({"index.md": "---\nokf_version: '0.1'\n---\n# S\n"})
        assert report.ok

    def test_log_bad_date_is_error(self):
        report = check_bundle({"log.md": "# Log\n\n## 2026/06/13\n* x\n"})
        assert any(f.code == "okf.log.date" for f in report.findings)

    def test_permissive_unknown_type_and_keys_ok(self):
        # Unknown type value + unknown extra key + broken link → still conformant.
        md = "---\ntype: WhateverCustomType\nweird_key: 1\n---\nSee [x](/nope.md).\n"
        report = check_bundle({"a.md": md})
        assert report.ok

    def test_permissive_no_index_ok(self):
        report = check_bundle({"a.md": "---\ntype: note\n---\nbody\n"})
        assert report.ok  # missing index.md is never a failure


class TestLogGrouping:
    def test_newest_first(self):
        from app.services.okf import _Entry  # noqa: PLC2701 — testing internal grouping
        entries = [
            _Entry(path="a.md", title="A", description="", timestamp="2026-01-01T00:00:00+00:00"),
            _Entry(path="b.md", title="B", description="", timestamp="2026-03-01T00:00:00+00:00"),
        ]
        log = build_log(entries)
        assert log.index("2026-03-01") < log.index("2026-01-01")
