"""Per-Vault interpretation preserves queue units, absence, and execution gates."""

from __future__ import annotations

import itertools
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME
from app.services import embed_worker, health, native_derived_worker, native_file_projection, search_update_status
from app.services.search_capabilities import metadata_enabled


def _config(**overrides):
    values = {
        "document_revision_backend": "postgres_native",
        "native_revision_m1_file_driver": "s3_current",
        "native_revision_m1_measurement_only": False,
        "db_name": "ordinary",
        "external_git_enabled": True,
        "llm_base_url": "https://llm.invalid/private-path",
        "llm_api_key": "private-fixture-value",  # pragma: allowlist secret
        "model_api_governance_mode": "external_metering",
        "embed_base_url": "",
    }
    return SimpleNamespace(**(values | overrides))


def _counts(**overrides):
    return {"pending": 0, "retrying": 0, "exhausted": 0, "abandoned": 0} | overrides


@pytest.fixture
def observed(monkeypatch):
    config = _config()
    monkeypatch.setattr(search_update_status, "settings", config)
    monkeypatch.setattr(search_update_status, "selected_document_revision_backend", lambda: "postgres_native")
    raw = {
        "vector_store": {"backfill": {"upsert": _counts(pending=3, retrying=1) | {"indexed": 42}}},
        "metadata_backfill": {"pending": 2, "retrying": 1, "abandoned": 0},
        "native_file_projection": _counts(),
        "native_derived": _counts(abandoned=8) | {"applied": 99, "status": "degraded"},
    }
    for module, data in (
        (embed_worker, raw["vector_store"]["backfill"]),
        (health.metadata_worker, raw["metadata_backfill"]),
        (native_file_projection, raw["native_file_projection"]),
        (native_derived_worker, raw["native_derived"]),
    ):
        monkeypatch.setattr(module, "pending_stats", AsyncMock(return_value=data))
    current = AsyncMock(return_value=_counts())
    monkeypatch.setattr(native_derived_worker, "current_head_stats", current)
    return config, raw, current


async def test_envelope_preserves_legacy_fields_and_scopes_each_unit(observed):
    _, raw, current = observed
    vault_id = uuid.uuid4()
    result = await health.vault_health(vault_id)
    assert {key: result[key] for key in raw} == raw
    current.assert_awaited_once_with(vault_id)
    for worker in (embed_worker, health.metadata_worker, native_file_projection, native_derived_worker):
        worker.pending_stats.assert_awaited_once_with(vault_id)
    envelope = result["search_update_status"]
    assert envelope["version"] == 1
    assert datetime.fromisoformat(envelope["observed_at"]).utcoffset().total_seconds() == 0
    assert envelope["stages"] == {
        "search_index": {
            "mode": "enabled", "unit": "chunks", "scope": "stored_chunks",
            "pending": 3, "retrying": 1, "abandoned": 0,
        },
        "file_projection": {
            "mode": "enabled", "unit": "file_updates", "scope": "latest_file_intents", **_counts(),
        },
        "content_preparation": {
            "mode": "enabled", "unit": "revision_updates", "scope": "current_heads", **_counts(),
        },
        "metadata": {
            "mode": "disabled", "unit": "documents", "scope": "external_git_documents",
            "pending": 2, "retrying": 1, "abandoned": 0,
        },
    }


async def test_optional_current_head_failure_keeps_legacy_and_unknown_stage(observed, caplog):
    _, raw, current = observed
    current.side_effect = RuntimeError("secret database connection value")
    result = await health.vault_health(uuid.uuid4())
    assert {key: result[key] for key in raw} == raw
    assert result["search_update_status"]["stages"]["content_preparation"] == {
        "mode": "enabled", "unit": "revision_updates", "scope": "current_heads",
    }
    assert "secret database connection value" not in caplog.text


async def test_unexpected_envelope_error_does_not_break_legacy(observed, monkeypatch, caplog):
    _, raw, _ = observed

    def fail(*args):
        raise ValueError("secret settings value")

    monkeypatch.setattr(search_update_status, "build", fail)
    assert await health.vault_health(uuid.uuid4()) == raw
    assert "secret settings value" not in caplog.text


async def test_original_stats_failure_keeps_existing_failure_behavior(observed, monkeypatch):
    monkeypatch.setattr(embed_worker, "pending_stats", AsyncMock(side_effect=RuntimeError("unavailable")))
    with pytest.raises(RuntimeError, match="unavailable"):
        await health.vault_health(uuid.uuid4())


@pytest.mark.parametrize("value", [None, -1, 1.5, True, "0", 2**53])
def test_missing_or_invalid_count_is_absent_not_zero(observed, value):
    envelope = search_update_status.build(
        {"upsert": {"pending": value, "retrying": 0}}, None, None, None,
    )
    stage = envelope["stages"]["search_index"]
    assert "pending" not in stage
    assert "abandoned" not in stage
    assert stage["retrying"] == 0
    assert set(envelope["stages"]["metadata"]) == {"mode", "unit", "scope"}


@pytest.mark.parametrize("counts", [_counts(pending=1, retrying=2), _counts(pending=1, exhausted=2),
                                    _counts(pending=2, retrying=1, exhausted=2)])
def test_conflicting_subset_counts_do_not_claim_clear(observed, counts):
    stage = search_update_status.build({}, {}, {}, counts)["stages"]["content_preparation"]
    assert set(stage) == {"mode", "unit", "scope"}


def test_retained_disabled_native_work_is_paused_not_inapplicable(observed):
    config, _, _ = observed
    config.document_revision_backend = "bare_git"
    stages = search_update_status.build({}, {}, _counts(), _counts(abandoned=1))["stages"]
    assert stages["file_projection"]["mode"] == "not_applicable"
    assert stages["content_preparation"]["mode"] == "disabled"
    assert stages["content_preparation"]["abandoned"] == 1
    stages = search_update_status.build({}, {}, None, None)["stages"]
    assert stages["file_projection"]["mode"] == "disabled"
    assert stages["content_preparation"]["mode"] == "disabled"


def test_uncomposed_metadata_mode_stays_unknown(observed, monkeypatch):
    monkeypatch.setattr(search_update_status, "selected_document_revision_backend", lambda: None)
    assert search_update_status.build({}, {}, {}, {})["stages"]["metadata"]["mode"] == "unknown"


@pytest.mark.parametrize(
    "selected,external,url,key,governance",
    itertools.product(("bare_git", "postgres_native", None), (False, True), ("", "url"),
                      ("", "key"), ("external_metering", "platform_hard")),
)
def test_metadata_modes_match_full_startup_gate(monkeypatch, selected, external, url, key, governance):
    config = _config(external_git_enabled=external, llm_base_url=url, llm_api_key=key,
                     model_api_governance_mode=governance)
    monkeypatch.setattr(search_update_status, "settings", config)
    monkeypatch.setattr(search_update_status, "selected_document_revision_backend", lambda: selected)
    enabled = selected == "bare_git" and external and bool(url) and bool(key or governance == "platform_hard")
    assert metadata_enabled(config, selected) is enabled
    expected = "unknown" if selected is None else "enabled" if enabled else "disabled"
    assert search_update_status.build({}, {}, {}, {})["stages"]["metadata"]["mode"] == expected


class _EmptyPool:
    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield


@pytest.mark.parametrize(
    "backend,driver,measurement,database",
    itertools.product(("bare_git", "postgres_native", "native_ledger_m1"), ("s3_current", "fscas", "s3cas"),
                      (False, True), ("ordinary", NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME)),
)
async def test_native_modes_match_actual_worker_execution(monkeypatch, backend, driver, measurement, database):
    config = _config(document_revision_backend=backend, native_revision_m1_file_driver=driver,
                     native_revision_m1_measurement_only=measurement, db_name=database)
    monkeypatch.setattr(embed_worker, "settings", config)
    monkeypatch.setattr(search_update_status, "settings", config)
    monkeypatch.setattr(embed_worker, "get_pool", AsyncMock(return_value=_EmptyPool()))
    monkeypatch.setattr(embed_worker, "_claim_batch", AsyncMock(return_value=[]))
    projection = AsyncMock(return_value=1)
    derived = AsyncMock(return_value=1)
    monkeypatch.setattr(native_file_projection, "NativeFileProjectionWorker",
                        lambda pool: SimpleNamespace(process_once=projection))
    monkeypatch.setattr(native_derived_worker, "NativeDerivedWorker",
                        lambda pool: SimpleNamespace(process_once=derived))
    projection_enabled = backend == "postgres_native"
    derived_enabled = backend in {"postgres_native", "native_ledger_m1"} or (
        driver != "s3_current" and measurement and database == NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME
    )
    assert await embed_worker._process_once() == int(projection_enabled) + int(derived_enabled)
    assert projection.await_count == int(projection_enabled)
    assert derived.await_count == int(derived_enabled)
    stages = search_update_status.build({}, {}, _counts(), _counts())["stages"]
    assert stages["file_projection"]["mode"] == ("enabled" if projection_enabled else "not_applicable")
    assert stages["content_preparation"]["mode"] == ("enabled" if derived_enabled else "not_applicable")
