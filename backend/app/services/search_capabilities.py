"""Shared configuration gates for search preparation consumers.

These describe intended configuration, not whether a worker process is alive.
Keep raw Native selection separate from the canonical startup backend: the
measurement path intentionally has its own execution gates.
"""

from __future__ import annotations

from app.config import NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME, Settings


def metadata_enabled(config: Settings, selected_backend: str | None) -> bool:
    return bool(
        selected_backend == "bare_git"
        and config.external_git_enabled
        and config.llm_base_url
        and (config.llm_api_key or config.model_api_governance_mode == "platform_hard")
    )


def file_projection_enabled(config: Settings) -> bool:
    return config.document_revision_backend == "postgres_native"


def native_derived_enabled(config: Settings) -> bool:
    return config.document_revision_backend in {"postgres_native", "native_ledger_m1"} or (
        config.native_revision_m1_file_driver != "s3_current"
        and config.native_revision_m1_measurement_only
        and config.db_name == NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME
    )
