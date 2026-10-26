"""Secret-free runtime corroboration, never artifact provenance authority."""

from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict

from app.config import settings


SOURCE_REVISION_FILE = Path(__file__).resolve().parents[2] / ".akb-source-revision"
BASE_CAPABILITIES = [
    "auth.public-config.v2", "auth.single-human-issuer.v1", "auth.runtime-generation.v1",
    "pat.authority.v1", "storage.postgres-native.v1", "auth.bootstrap.v6",
    "sso.provider-security-profile.v1", "sso.browser-theme.v1",
    "sso.provider-ownership.v1", "sso.identity-operation.v1", "sso.enabled-exact-admission.v1",
    "sso.preparation-target.v1", "auth.callback-login-evidence.v1",
    "sso.operation-attempts.v1",
    "auth.prepared-target-adoption.v1",
    "auth.platform-service-bootstrap.v1",
]


def source_revision() -> str | None:
    try:
        value = SOURCE_REVISION_FILE.read_text().strip()
    except OSError:
        return None
    return value if re.fullmatch(r"[a-f0-9]{40}", value) else None


def source_tuple() -> dict:
    mode = settings.require_auth_mode()
    return {"auth_mode": mode, "issuer": settings.keycloak_issuer if mode == "sso" else None,
            "runtime_generation": settings.auth_runtime_generation,
            "sso_session_epoch": str(settings.sso_session_epoch) if mode == "sso" else None,
            "source_revision": source_revision()}


async def lock_source(conn, expected: dict) -> None:
    from app.sso.identity_operations import _error

    if expected != source_tuple():
        raise _error("sso_operation_runtime_conflict")
    active = await conn.fetchval("""SELECT TRUE FROM auth_runtime_state WHERE singleton
        AND runtime_generation=$1 AND auth_mode=$2 AND sso_session_epoch IS NOT DISTINCT FROM $3::uuid
        FOR SHARE""", expected["runtime_generation"], expected["auth_mode"], expected["sso_session_epoch"])
    if active is not True:
        raise _error("sso_operation_runtime_conflict")


class IdentityCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int
    source_revision: str | None
    capabilities: list[str]
    auth_mode: str
    runtime_generation: int
    sso_session_epoch: str | None
    issuer: str | None
