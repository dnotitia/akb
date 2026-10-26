"""Installation-owned Keycloak management targets. Never authentication issuers.

HTTP callers select only an opaque installed UUID. Loading this file does not
change settings, token verification, login discovery or the browser runtime.
"""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr

from app.config import settings
from app.sso.keycloak_admin import KeycloakAdminConfig, KeycloakProviderControl, get_keycloak_provider_control
from app.sso.runtime_identity import source_tuple


class InstalledTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target_id: uuid.UUID
    realm_id: str
    realm_name: str
    issuer: str
    internal_url: str
    management_client_id: str
    management_client_secret: SecretStr
    session_epoch: uuid.UUID
    verify_ssl: bool = True


@dataclass(frozen=True)
class Target:
    target_id: uuid.UUID
    fingerprint: str
    issuer: str
    realm_id: str | None
    session_epoch: uuid.UUID | None
    control: KeycloakProviderControl = field(repr=False, compare=False)


def _fail():
    from app.sso.identity_operations import _error
    return _error("sso_management_target_unavailable")


_controls: dict[str, KeycloakProviderControl] = {}


def installed_targets() -> list[Target]:
    path = settings.sso_management_targets_file
    if not path:
        return []
    try:
        location = Path(path)
        info = location.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in {0, os.geteuid()}:
            raise ValueError()
        raw = location.read_bytes()
        if len(raw) > 65536:
            raise ValueError()
        document = json.loads(raw)
        if set(document) != {"schema_version", "targets"} or document["schema_version"] != 1:
            raise ValueError()
        entries = [InstalledTarget.model_validate(item) for item in document["targets"]]
        if len(entries) > 16 or len({entry.target_id for entry in entries}) != len(entries) or len({entry.issuer for entry in entries}) != len(entries):
            raise ValueError()
        targets = []
        for entry in entries:
            # Derive the public root only from a canonical /realms/name issuer;
            # no request-supplied endpoints or discovery aliases are accepted.
            suffix = f"/realms/{entry.realm_name}"
            if (not entry.realm_id or not entry.realm_name or "/" in entry.realm_name
                or not entry.issuer.endswith(suffix) or not entry.management_client_id
                or not entry.management_client_secret.get_secret_value()):
                raise ValueError()
            public = entry.issuer[:-len(suffix)]
            for endpoint in (public, entry.internal_url):
                parsed = urlsplit(endpoint)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError()
            canonical = entry.model_dump(mode="json")
            canonical["management_client_secret"] = entry.management_client_secret.get_secret_value()
            fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if fingerprint not in _controls:
                _controls[fingerprint] = KeycloakProviderControl(KeycloakAdminConfig(
                    internal_base_url=entry.internal_url, public_base_url=public, realm=entry.realm_name,
                    management_client_id=entry.management_client_id,
                    management_client_secret=entry.management_client_secret.get_secret_value(), verify_ssl=entry.verify_ssl))
            targets.append(Target(entry.target_id, fingerprint, entry.issuer, entry.realm_id, entry.session_epoch, _controls[fingerprint]))
        return targets
    except (OSError, ValueError, TypeError, KeyError):
        raise _fail() from None


async def resolve(conn, target_id: uuid.UUID | None = None) -> Target:
    installation = await conn.fetchrow("SELECT * FROM sso_operation_installation WHERE singleton")
    active = source_tuple()
    # Preserve the pre-target ownership namespace, including across restart.
    # Installing a different UUID for this same issuer must not hide its rows.
    targets = installed_targets()
    for target in targets:
        await conn.execute("INSERT INTO sso_management_target_pins(target_id,issuer) VALUES($1,$2) ON CONFLICT DO NOTHING", target.target_id, target.issuer)
        pinned = await conn.fetchrow("SELECT * FROM sso_management_target_pins WHERE target_id=$1 OR issuer=$2", target.target_id, target.issuer)
        if pinned is None or pinned["target_id"] != target.target_id or pinned["issuer"] != target.issuer:
            raise _fail()
    known_active = await conn.fetchval("SELECT target_id FROM sso_management_target_pins WHERE issuer=$1", active["issuer"])
    if active["issuer"] and installation["legacy_issuer"] is None and not known_active and not any(t.issuer == active["issuer"] for t in targets):
        await conn.execute("UPDATE sso_operation_installation SET legacy_issuer=$1 WHERE singleton AND legacy_issuer IS NULL", active["issuer"])
        installation = await conn.fetchrow("SELECT * FROM sso_operation_installation WHERE singleton")
    for target in targets:
        if target.issuer == installation["legacy_issuer"] and target.target_id != installation["legacy_target_id"]:
            raise _fail()
    matching = [item for item in targets if item.target_id == target_id] if target_id else [item for item in targets if item.issuer == active["issuer"]]
    if len(matching) == 1:
        if target_id is None and settings.sso_prepared_target_id and str(matching[0].target_id) != settings.sso_prepared_target_id:
            raise _fail()
        return matching[0]
    if target_id is None and (known_active or settings.sso_prepared_target_id):
        raise _fail()
    if target_id is not None and target_id != installation["legacy_target_id"]:
        raise _fail()
    if active["auth_mode"] != "sso" or active["issuer"] != installation["legacy_issuer"]:
        raise _fail()
    control = get_keycloak_provider_control()
    # Include the active management configuration in the pin, without exposing
    # its secret or using the changing session epoch as the ownership key.
    config = getattr(control, "config", None)
    fingerprint = hashlib.sha256(repr(config).encode()).hexdigest()
    if config is not None:
        fingerprint = hashlib.sha256((fingerprint + config.management_client_secret).encode()).hexdigest()
    return Target(installation["legacy_target_id"], fingerprint, active["issuer"], None,
                  uuid.UUID(active["sso_session_epoch"]), control)


async def read_realm(target: Target) -> dict:
    realm = await target.control.read_realm_identity()
    if realm["issuer"] != target.issuer or (target.realm_id is not None and realm["realm_id"] != target.realm_id):
        raise _fail()
    return realm
