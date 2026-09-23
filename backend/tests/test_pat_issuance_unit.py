"""Strict issuance inputs cannot silently replace restrictions with defaults."""
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from pydantic import ValidationError

from app.api.routes.auth import CreatePATRequest
from app.exceptions import AKBError
from app.models.pat_issuance import PATIssuanceRequest


def request(**changes):
    return {"contract_version": 1, "expected_user_id": str(uuid.uuid4()), "name": " Work laptop ",
            "scopes": ["read", "write"], "vault_scope": None, **changes}


@pytest.mark.parametrize("changes", [
    {"contract_version": True}, {"contract_version": 2}, {"unexpected": True}, {"name": " "},
    {"name": "x" * 256}, {"name": "nul\x00name"}, {"name": "bad\ud800"},
    {"scopes": ["re\x00ad"]}, {"expires_at": "2099-01-01T00:00:00Z\x00"},
    {"vault_scope": {"prefixes": ["team-\x00"], "extra_vaults": []}},
    {"scopes": []}, {"scopes": ["admin"]}, {"scopes": ["write"]}, {"scopes": ["delete"]},
    {"expires_days": None}, {"expires_days": 0}, {"expires_days": -1}, {"expires_days": True},
    {"expires_days": "30"}, {"expires_days": 1.5}, {"expires_at": None},
    {"expires_at": "2000000000"}, {"expires_at": 2000000000}, {"expires_at": "2099-01-01T00:00:00"},
    {"expires_at": "2000-01-01T00:00:00Z"}, {"expires_at": "2099-02-30T00:00:00Z"},
    {"expires_days": 30, "expires_at": "2099-01-01T00:00:00Z"},
    {"vault_scope": {}}, {"vault_scope": {"prefixes": [], "extra_vaults": []}},
    {"vault_scope": {"prefixes": ["team-*"], "extra_vaults": []}},
    {"vault_scope": {"prefixes": [], "extra_vaults": ["../vault"]}},
    {"vault_scope": {"prefixes": ["team-"], "extra_vaults": [], "ignored": []}},
])
def test_invalid_options_fail_instead_of_defaulting(changes):
    with pytest.raises((ValidationError, AKBError)):
        PATIssuanceRequest(**request(**changes))


@pytest.mark.parametrize("field", ["contract_version", "expected_user_id", "scopes", "vault_scope", "name"])
def test_required_v1_fields(field):
    body = request()
    del body[field]
    with pytest.raises(ValidationError):
        PATIssuanceRequest(**body)


def test_normalization_preserves_concrete_scope_for_read_only():
    parsed = PATIssuanceRequest(**request(name=" cafe\u0301 ", scopes=["read", "read"],
        vault_scope={"prefixes": [" team-", "team-"], "extra_vaults": ["z", "a", "z"]}))
    assert parsed.name == "caf\u00e9"
    assert parsed.scopes == ["read"]
    assert parsed.vault_scope.model_dump() == {"prefixes": ["team-"], "extra_vaults": ["a", "z"]}
    assert parsed.expires_at is None and parsed.expires_days is None


def test_aware_expiry_normalizes_without_rounding_to_days():
    instant = datetime.now(timezone.utc) + timedelta(hours=3, microseconds=123456)
    parsed = PATIssuanceRequest(**request(expires_at=instant.astimezone(timezone(timedelta(hours=9))).isoformat()))
    assert parsed.expires_at == instant
    assert parsed.expires_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("value", [None, 0, 30])
def test_legacy_unlimited_and_positive_days_preserved(value):
    assert CreatePATRequest(name="fixture", expires_days=value).expires_days == value


@pytest.mark.parametrize("value", [-1, True, "30", 1.5])
def test_legacy_noncanonical_expiration_fails(value):
    with pytest.raises(ValidationError):
        CreatePATRequest(name="fixture", expires_days=value)


@pytest.mark.parametrize("instant", ["9999-12-31T23:59:59-01:00", "0001-01-01T00:00:00+01:00"])
def test_absolute_utc_overflow_is_a_field_error(instant):
    with pytest.raises(AKBError) as exc:
        PATIssuanceRequest(**request(expires_at=instant))
    assert exc.value.status_code == 422
    assert exc.value.details["fields"][0]["field"] == "expires_at"
    assert exc.value.details["fields"][0]["code"] == "expiration_overflow"
