"""Pure contracts for the strict App Release Manifest v2 boundary."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api.control_plane_models import ReleaseManifest
from app.exceptions import ValidationError
from app.services import app_resource_service as resources
from app.services import app_rollout_service as rollout


def _checksum(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _manifest() -> dict:
    create_step = {
        "id": "create_orders",
        "phase": "expand",
        "operation": "create_table",
        "payload": {
            "table": "orders",
            "columns": [
                {"name": "amount", "type": "numeric"},
                {"name": "email", "type": "text", "required": True},
            ],
            "unique_keys": [{"columns": ["email"]}],
            "indexes": [{"columns": [{"name": "amount", "order": "asc"}]}],
        },
    }
    create_step["checksum"] = _checksum(create_step)
    return {
        "manifest_version": 2,
        "app_key": "example-app",
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "schema_version": 3,
        "schema": {
            "tables": [
                {
                    "name": "orders",
                    "columns": [
                        {"name": "amount", "type": "numeric"},
                        {"name": "email", "type": "text", "required": True},
                    ],
                    "unique_keys": [{"columns": ["email"]}],
                    "indexes": [{"columns": [{"name": "amount", "order": "asc"}]}],
                }
            ]
        },
        "transition_plans": [
            {"source": "fresh", "steps": [create_step]},
            {
                "source": {
                    "release_version": "1.0.0",
                    "schema_fingerprint": "c" * 64,
                },
                "steps": [],
            },
        ],
    }


def test_manifest_v2_covers_provenance_schema_and_transition_plans() -> None:
    manifest = _manifest()
    checksum = rollout.manifest_checksum(manifest)

    normalized = rollout.validate_manifest(manifest, checksum)

    assert normalized["manifest_version"] == 2
    assert normalized["schema"]["fingerprint"] == resources.canonical_table_fingerprint(
        manifest["schema"]["tables"]
    )
    assert normalized["transition_plans"][0]["source"] == "fresh"
    assert normalized["transition_plans"][0]["steps"][0]["operation"] == "create_table"

    changed = dict(manifest)
    changed["image_digest"] = "sha256:" + "d" * 64
    with pytest.raises(ValidationError):
        rollout.validate_manifest(changed, checksum)


def test_public_manifest_model_is_strict_and_v2_only() -> None:
    ReleaseManifest.model_validate(_manifest())
    with pytest.raises(PydanticValidationError):
        ReleaseManifest.model_validate({"manifest_version": 1, "steps": []})
    with pytest.raises(PydanticValidationError):
        ReleaseManifest.model_validate({**_manifest(), "unknown": True})
    with pytest.raises(PydanticValidationError):
        ReleaseManifest.model_validate(
            {
                **_manifest(),
                "schema_version": "3",
            }
        )
    with pytest.raises(PydanticValidationError):
        ReleaseManifest.model_validate(
            {key: value for key, value in _manifest().items() if key != "schema"}
            | {"schema_": _manifest()["schema"]}
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update({"manifest_version": 1, "steps": []}),
        lambda body: body.update({"unexpected": True}),
        lambda body: body["transition_plans"][0]["steps"][0]["payload"].update(
            {"raw_sql": "DROP TABLE orders"}
        ),
        lambda body: body["transition_plans"][0]["steps"][0].update(
            {"operation": "drop_table"}
        ),
    ],
)
def test_manifest_v2_rejects_v1_unknown_raw_sql_and_destructive_shapes(mutate) -> None:
    body = _manifest()
    mutate(body)
    with pytest.raises(ValidationError):
        rollout.validate_manifest(body, rollout.manifest_checksum(_manifest()))


def test_source_plan_selection_is_exact_and_fail_closed() -> None:
    normalized = rollout.validate_manifest(_manifest(), rollout.manifest_checksum(_manifest()))
    fingerprint = "c" * 64

    fresh = rollout.select_transition_plan(
        normalized,
        current_release_version=None,
        current_schema_fingerprint=None,
    )
    exact = rollout.select_transition_plan(
        normalized,
        current_release_version="1.0.0",
        current_schema_fingerprint=fingerprint,
    )
    assert fresh["source"] == "fresh"
    assert exact["source"]["release_version"] == "1.0.0"

    with pytest.raises(ValidationError, match="transition plan"):
        rollout.select_transition_plan(
            normalized,
            current_release_version="1.0.0",
            current_schema_fingerprint="e" * 64,
        )


def test_table_fingerprint_is_the_projection_fingerprint() -> None:
    manifest = _manifest()
    tables = manifest["schema"]["tables"]
    assert rollout.schema_projection_fingerprint(manifest["schema"]) == resources.canonical_table_fingerprint(
        reversed(tables)
    )


def test_table_fingerprint_normalizes_logical_aliases_and_physical_constraint_names() -> None:
    first = {
        "name": "orders",
        "columns": [
            {"name": "payload", "type": "jsonb", "required": False},
            {"name": "amount", "type": "numeric"},
        ],
        "unique_keys": [{"name": "physical_a", "columns": ["amount"]}],
        "indexes": [{"name": "physical_i", "columns": ["payload"]}],
    }
    second = {
        "name": "orders",
        "columns": [
            {"name": "amount", "type": "number"},
            {"name": "payload", "type": "json"},
        ],
        "unique_keys": [{"name": "physical_b", "columns": ["amount"]}],
        "indexes": [{"name": "physical_j", "columns": [{"name": "payload", "order": "asc"}]}],
    }
    assert resources.canonical_table_fingerprint([first]) == resources.canonical_table_fingerprint([second])


def test_table_fingerprint_treats_column_constraint_flags_as_metadata_shorthand() -> None:
    shorthand = {
        "name": "orders",
        "columns": [
            {"name": "email", "type": "text", "unique": True},
            {"name": "amount", "type": "numeric", "index": True},
        ],
        "unique_keys": [],
        "indexes": [],
    }
    expanded = {
        "name": "orders",
        "columns": [
            {"name": "email", "type": "text"},
            {"name": "amount", "type": "numeric"},
        ],
        "unique_keys": [{"columns": ["email"]}],
        "indexes": [{"columns": [{"name": "amount", "order": "asc"}]}],
    }
    assert resources.canonical_table_fingerprint([shorthand]) == resources.canonical_table_fingerprint(
        [expanded]
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update({"manifest_version": 2.0}),
        lambda body: body["schema"]["tables"][0].update(
            {"unique_keys": None}
        ),
        lambda body: body["schema"]["tables"][0]["columns"][0].update(
            {"check": {"op": "eq", "value": "x", "raw_sql": "TRUE"}}
        ),
        lambda body: body["transition_plans"][0]["steps"][0].update(
            {"phase": []}
        ),
    ],
)
def test_manifest_v2_rejects_non_strict_nested_shapes(mutate) -> None:
    body = _manifest()
    mutate(body)
    with pytest.raises(ValidationError):
        rollout.manifest_checksum(body)


def test_manifest_identity_requires_full_source_and_immutable_image_digest() -> None:
    body = _manifest()
    for field, value in (
        ("source_revision", "not-a-full-revision"),
        ("image_digest", "latest"),
    ):
        invalid = dict(body)
        invalid[field] = value
        with pytest.raises(ValidationError):
            rollout.validate_manifest(invalid, rollout.manifest_checksum(body))


def test_manifest_rejects_function_defaults_as_expressions() -> None:
    body = _manifest()
    body["schema"]["tables"][0]["columns"][0]["default"] = "now()"
    with pytest.raises(ValidationError, match="expressions"):
        rollout.manifest_checksum(body)


def test_manifest_checksum_is_stable_for_object_key_order() -> None:
    body = _manifest()
    reordered = json.loads(json.dumps(body, ensure_ascii=False))
    reordered["schema"] = {"tables": list(reversed(reordered["schema"]["tables"]))}
    assert rollout.manifest_checksum(body) == rollout.manifest_checksum(reordered)
    assert rollout.manifest_checksum(body, version="1.0.0") != rollout.manifest_checksum(
        body, version="2.0.0"
    )


def test_selector_rejects_ambiguous_duplicate_exact_sources() -> None:
    body = _manifest()
    body["transition_plans"].append(
        {
            "source": {
                "release_version": "1.0.0",
                "schema_fingerprint": "c" * 64,
            },
            "steps": [],
        }
    )
    with pytest.raises(ValidationError):
        rollout.validate_manifest(body, rollout.manifest_checksum(body))


def test_checksum_input_accepts_json_object_only() -> None:
    with pytest.raises(ValidationError):
        rollout.manifest_checksum(str(uuid.uuid4()))
