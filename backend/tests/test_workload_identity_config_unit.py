"""Managed identity rejects secret fallback; standalone stays independent."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def managed_values(**overrides):
    return {
        "model_api_governance_mode": "platform_hard",
        "platform_gateway_base_url": "https://gateway.example/v1",
        "platform_gateway_token_file": "/var/run/identity/gateway/token",
        "embed_base_url": "https://gateway.example/v1",
        "s3_auth_mode": "default_chain",
        "s3_endpoint_url": "https://storage.example",
        "s3_sts_endpoint_url": "https://sts.example",
        "s3_role_arn": "arn:aws:iam:::role/tenant-files",
        "s3_web_identity_token_file": "/var/run/identity/rgw/token",
        **overrides,
    }


def test_managed_profile_accepts_keyless_configuration():
    configured = Settings(**managed_values())
    assert configured.s3_auth_mode == "default_chain"
    assert configured.object_storage_enabled


@pytest.mark.parametrize("name", [
    "platform_gateway_token_file", "s3_web_identity_token_file",
    "s3_role_arn", "s3_sts_endpoint_url", "s3_endpoint_url",
])
def test_managed_profile_requires_explicit_identity_inputs(name):
    with pytest.raises(ValidationError, match=name):
        Settings(**managed_values(**{name: ""}))


@pytest.mark.parametrize("name", [
    "embed_api_key", "llm_api_key", "rerank_api_key", "s3_access_key", "s3_secret_key",
])
def test_managed_profile_rejects_static_keys_even_on_disabled_routes(name):
    with pytest.raises(ValidationError, match=name):
        Settings(**managed_values(**{name: "forbidden-fixture"}))


@pytest.mark.parametrize("name", ["access_key", "secret_key"])
def test_managed_profile_rejects_dedicated_audit_keys(name):
    with pytest.raises(ValidationError, match=f"audit.{name}"):
        Settings(**managed_values(audit={name: "forbidden-fixture"}))


def test_managed_profile_rejects_static_auth_and_direct_routes():
    with pytest.raises(ValidationError, match="s3_auth_mode"):
        Settings(**managed_values(s3_auth_mode="static"))
    with pytest.raises(ValidationError, match="llm_base_url"):
        Settings(**managed_values(llm_base_url="https://provider.example/v1"))


def test_managed_profile_allows_disabled_embedding_route():
    assert Settings(**managed_values(embed_base_url="")).embed_base_url == ""


@pytest.mark.parametrize("name", ["platform_gateway_token_file", "s3_web_identity_token_file"])
def test_projected_token_paths_are_absolute(name):
    with pytest.raises(ValidationError, match=name):
        Settings(**managed_values(**{name: "relative/token"}))


def test_standalone_static_and_native_cloud_storage_are_explicit():
    disabled = Settings(embed_base_url="")
    assert disabled.s3_auth_mode == "static"
    assert not disabled.object_storage_enabled
    static = Settings(s3_endpoint_url="http://minio:9000", s3_access_key="local", s3_secret_key="fixture")
    assert static.object_storage_enabled
    cloud = Settings(s3_auth_mode="default_chain", s3_region="us-east-1")
    assert cloud.object_storage_enabled
    assert not cloud.platform_gateway_token_file
    with pytest.raises(ValidationError, match="s3_access_key"):
        Settings(s3_auth_mode="default_chain", s3_access_key="not-selected")


def test_explicit_standalone_rgw_identity_requires_complete_tuple():
    with pytest.raises(ValidationError, match="s3_role_arn"):
        Settings(s3_auth_mode="default_chain", s3_web_identity_token_file="/run/token")
