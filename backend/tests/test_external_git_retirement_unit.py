"""Exact frozen Collector v1 contract tests for external-Git retirement."""

from __future__ import annotations

import copy

import pytest

from app.services.external_git_retirement import (
    ExternalGitRetirementError,
    parse_adoption_manifest,
)


_REF = "a" * 40


def _manifest() -> dict:
    """The exact JSON shape emitted by the frozen Collector v1 contract."""
    return {
        "schema": "akb-collector.git-adoption-manifest",
        "version": 1,
        "purpose": "legacy-external-git-retirement",
        "binding": {
            "name": "git-fixture",
            "source_scope": "fixture/repository",
            "target_vault": "collector-adoption",
            "target_collection": "collector-control",
        },
        "source": {
            "remote_url": "https://git.example.invalid/acme/knowledge.git",
            "branch": "main",
            "snapshot_commit": _REF,
            "path_prefix": None,
        },
        "documents": [
            {
                "origin_key": "git://fixture/repository/overview.md",
                "path": "overview.md",
                "resource_uri": "akb://collector-adoption/doc/overview.md",
                "source_version": "b" * 40,
                "blob_sha": "b" * 40,
                "akb_content_sha256": "c" * 64,
                "akb_current_version": "d" * 40,
                "managed_metadata": {
                    "managed": True,
                    "title": "Overview",
                    "type": "note",
                    "tags": ["collector"],
                    "summary": "Adopted source",
                    "domain": "operations",
                },
            },
            {
                "origin_key": "git://fixture/repository/specs/contract.txt",
                "path": "specs/contract.txt",
                "resource_uri": "akb://collector-adoption/coll/specs/doc/contract.txt",
                "source_version": "e" * 40,
                "blob_sha": "e" * 40,
                "akb_content_sha256": "f" * 64,
                "akb_current_version": _REF,
                "managed_metadata": {
                    "managed": False,
                    "title": "contract",
                    "type": "reference",
                    "tags": [],
                    "summary": "",
                    "domain": "",
                },
            },
        ],
    }


def test_collector_v1_manifest_is_strict_body_free_and_order_independent() -> None:
    manifest = _manifest()
    reordered = copy.deepcopy(manifest)
    reordered["documents"].reverse()

    parsed = parse_adoption_manifest(manifest)
    assert parsed.digest == parse_adoption_manifest(reordered).digest
    assert parsed.target_vault == "collector-adoption"
    assert parsed.target_collection == "collector-control"
    assert parsed.source_scope == "fixture/repository"
    assert parsed.path_prefix is None
    assert [document.path for document in parsed.documents] == [
        "overview.md",
        "specs/contract.txt",
    ]
    filtered = copy.deepcopy(manifest)
    filtered["source"]["path_prefix"] = "specs"
    filtered_parsed = parse_adoption_manifest(filtered)
    assert filtered_parsed.path_prefix == "specs"
    assert filtered_parsed.digest != parsed.digest

    with_body = copy.deepcopy(manifest)
    with_body["documents"][0]["body"] = "never persist document bodies"
    with pytest.raises(ExternalGitRetirementError, match="manifest shape"):
        parse_adoption_manifest(with_body)

    with_token = copy.deepcopy(manifest)
    with_token["auth_token"] = "must-never-enter-akb"
    with pytest.raises(ExternalGitRetirementError, match="manifest shape"):
        parse_adoption_manifest(with_token)

    without_prefix = copy.deepcopy(manifest)
    del without_prefix["source"]["path_prefix"]
    with pytest.raises(ExternalGitRetirementError, match="manifest shape"):
        parse_adoption_manifest(without_prefix)


def test_collector_v1_path_prefix_is_only_a_canonical_receipt_fact() -> None:
    manifest = _manifest()
    # Collector canonicalizes this field by trimming only leading/trailing
    # slashes.  It is a binding fact, never an AKB live-inventory selector.
    manifest["source"]["path_prefix"] = "docs//generated"
    parsed = parse_adoption_manifest(manifest)
    assert parsed.path_prefix == "docs//generated"

    noncanonical = _manifest()
    noncanonical["source"]["path_prefix"] = "/docs/"
    with pytest.raises(ExternalGitRetirementError, match="manifest shape"):
        parse_adoption_manifest(noncanonical)


def test_collector_v1_manifest_rejects_ambiguous_or_noncanonical_proofs() -> None:
    duplicate = _manifest()
    duplicate["documents"].append(copy.deepcopy(duplicate["documents"][0]))
    with pytest.raises(ExternalGitRetirementError, match="duplicate"):
        parse_adoption_manifest(duplicate)

    mismatched_uri = _manifest()
    mismatched_uri["documents"][0]["resource_uri"] = "akb://collector-adoption/doc/other.md"
    with pytest.raises(ExternalGitRetirementError, match="URI"):
        parse_adoption_manifest(mismatched_uri)

    mismatched_source_identity = _manifest()
    mismatched_source_identity["documents"][0]["source_version"] = "f" * 40
    with pytest.raises(ExternalGitRetirementError, match="source identity"):
        parse_adoption_manifest(mismatched_source_identity)

    unsafe_path = _manifest()
    unsafe_path["documents"][0]["path"] = "../overview.md"
    unsafe_path["documents"][0]["resource_uri"] = "akb://collector-adoption/coll/../doc/overview.md"
    unsafe_path["documents"][0]["origin_key"] = "git://fixture/repository/../overview.md"
    with pytest.raises(ExternalGitRetirementError, match="path"):
        parse_adoption_manifest(unsafe_path)

    nondefault_unmanaged = _manifest()
    nondefault_unmanaged["documents"][1]["managed_metadata"]["type"] = "note"
    with pytest.raises(ExternalGitRetirementError, match="unmanaged metadata"):
        parse_adoption_manifest(nondefault_unmanaged)
