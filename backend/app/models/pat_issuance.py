"""Strict, versioned human PAT issuance inputs and stable field errors."""
from datetime import datetime, timezone
import re
import uuid

from pydantic import AwareDatetime, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.exceptions import AKBError
from app.util.text import NFCModel

NAME_MAX_LENGTH = 255
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:[Zz]|[+-]\d{2}:\d{2})")


def field_error(field: str, code: str, message: str) -> AKBError:
    return AKBError("Token issuance validation failed", 422, code="token_issuance_validation",
                    details={"fields": [{"field": field, "code": code, "message": message}]})


class PATVaultScope(NFCModel):
    model_config = ConfigDict(extra="forbid")
    prefixes: list[StrictStr]
    extra_vaults: list[StrictStr]

    @model_validator(mode="after")
    def valid_names(self):
        for field in ("prefixes", "extra_vaults"):
            values = getattr(self, field)
            normalized = []
            for index, value in enumerate(values):
                value = value.strip()
                if _NAME.fullmatch(value) is None:
                    raise field_error(f"vault_scope.{field}.{index}", "invalid_prefix" if field == "prefixes" else "invalid_vault_name",
                                      "Use lowercase letters, numbers and hyphens, starting with a letter or number.")
                normalized.append(value)
            setattr(self, field, sorted(set(normalized)))
        if not self.prefixes and not self.extra_vaults:
            raise field_error("vault_scope", "empty_vault_scope", "Select at least one Vault name or prefix.")
        return self


class PATIssuanceRequest(NFCModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: StrictInt
    expected_user_id: uuid.UUID
    name: StrictStr
    scopes: list[StrictStr]
    vault_scope: PATVaultScope | None
    expires_days: StrictInt | None = Field(default=None, gt=0)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_nul_before_normalization(cls, data):
        # The shared NFC normalizer strips NULs for document ingestion. This
        # strict issuance contract must reject, never repair, authority inputs.
        def check(value, path):
            if isinstance(value, str) and "\x00" in value:
                raise field_error(path or "form", "invalid_nul", "NUL characters are not allowed.")
            if isinstance(value, dict):
                for key, item in value.items():
                    check(key, path)
                    check(item, f"{path}.{key}" if path else str(key))
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    check(item, f"{path}.{index}")
        check(data, "")
        return data

    @field_validator("contract_version")
    @classmethod
    def version(cls, value):
        if value != 1:
            raise PydanticCustomError("unsupported_contract_version", "Unsupported issuance contract version")
        return value

    @field_validator("name")
    @classmethod
    def name_bound(cls, value):
        value = value.strip()
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise PydanticCustomError("invalid_name", "Name must be valid Unicode text") from None
        if "\x00" in value:
            raise PydanticCustomError("invalid_name", "Name must not contain a NUL character")
        if not 1 <= len(value) <= NAME_MAX_LENGTH:
            raise PydanticCustomError("invalid_name_length", "Name must contain 1 to 255 characters")
        return value

    @field_validator("scopes")
    @classmethod
    def permission_preset(cls, value):
        value = sorted(set(value))
        if value not in (["read"], ["read", "write"]):
            raise PydanticCustomError("invalid_permission_preset", "Choose Read only or Read and write")
        return value

    @field_validator("expires_at", mode="before")
    @classmethod
    def absolute_string(cls, value):
        if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
            raise PydanticCustomError("invalid_expiration", "Expiration must be an aware RFC3339 date and time")
        return value

    @model_validator(mode="after")
    def expiration_shape(self):
        if "expires_days" in self.model_fields_set and self.expires_days is None:
            raise field_error("expires_days", "invalid_expiration", "Omit expiration fields for no expiration.")
        if "expires_days" in self.model_fields_set and "expires_at" in self.model_fields_set:
            raise field_error("expires_at", "expiration_conflict", "Choose either a duration or an absolute expiration.")
        if self.expires_at is not None:
            try:
                self.expires_at = self.expires_at.astimezone(timezone.utc)
            except OverflowError:
                raise field_error("expires_at", "expiration_overflow", "Expiration is beyond the supported date range.") from None
            if self.expires_at <= datetime.now(timezone.utc):
                raise field_error("expires_at", "expiration_in_past", "Expiration must be in the future.")
        return self
