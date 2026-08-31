"""Pure contract tests for offline external-Git retirement manifests."""

from __future__ import annotations

import copy

import pytest

from app.services.external_git_retirement import (
    ExternalGitRetirementError,
    parse_adoption_manifest,
)


_VAULT_ID = "11111111-1111-1111-1111-111111111111"
_REF = "a" * 40


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "vault_id": _VAULT_ID,
        "vault_name": "collector-adoption",
        "remote_url": "https://git.example.invalid/acme/knowledge.git",
        "remote_branch": "main",
        "last_synced_sha": _REF,
        "documents": [
            {
                "uri": "akb://collector-adoption/doc/overview.md",
                "path": "overview.md",
                "content_hash": "b" * 64,
                "managed_metadata": {
                    "title": "Overview",
                    "type": "note",
                    "status": "active",
                    "tags": ["collector"],
                    "domain": "operations",
                    "summary": "Adopted source",
                    "metadata": {"external_path": "overview.md", "topic": "adoption"},
                },
            },
            {
                "uri": "akb://collector-adoption/coll/specs/doc/contract.md",
                "path": "specs/contract.md",
                "content_hash": "c" * 64,
                "managed_metadata": {
                    "title": "Contract",
                    "type": "spec",
                    "status": "active",
                    "tags": [],
                    "domain": "",
                    "summary": "",
                    "metadata": {"external_path": "specs/contract.md"},
                },
            },
        ],
    }


def test_adoption_manifest_is_body_free_and_order_independent() -> None:
    manifest = _manifest()
    reordered = copy.deepcopy(manifest)
    reordered["documents"].reverse()

    parsed = parse_adoption_manifest(manifest)
    assert parsed.digest == parse_adoption_manifest(reordered).digest
    assert [document.path for document in parsed.documents] == [
        "overview.md",
        "specs/contract.md",
    ]

    with_body = copy.deepcopy(manifest)
    with_body["documents"][0]["body"] = "never persist document bodies"
    with pytest.raises(ExternalGitRetirementError, match="manifest shape"):
        parse_adoption_manifest(with_body)

    with_token = copy.deepcopy(manifest)
    with_token["auth_token"] = "must-never-enter-akb"
    with pytest.raises(ExternalGitRetirementError, match="manifest shape"):
        parse_adoption_manifest(with_token)


def test_adoption_manifest_rejects_duplicate_or_mismatched_uri_entries() -> None:
    duplicate = _manifest()
    duplicate["documents"].append(copy.deepcopy(duplicate["documents"][0]))
    with pytest.raises(ExternalGitRetirementError, match="duplicate"):
        parse_adoption_manifest(duplicate)

    mismatched_uri = _manifest()
    mismatched_uri["documents"][0]["uri"] = "akb://collector-adoption/doc/other.md"
    with pytest.raises(ExternalGitRetirementError, match="URI"):
        parse_adoption_manifest(mismatched_uri)
