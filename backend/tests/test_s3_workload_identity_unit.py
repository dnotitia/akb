"""Real botocore STS serialization and SigV4 signing against an offline RGW."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError
from botocore.httpsession import URLLib3Session

from app.config import Settings
from app.exceptions import AKBError
from app.services import audit_log
from app.services.adapters import s3_adapter
from tests.test_workload_identity_config_unit import managed_values


class _Body:
    def __init__(self, body):
        self.body = body

    def stream(self, **_kwargs):
        yield self.body


@pytest.fixture
def storage(monkeypatch, tmp_path):
    token = tmp_path / "rgw-token"
    token.write_text("projected-first\n")
    configured = Settings(**managed_values(
        s3_web_identity_token_file=str(token),
        s3_region="us-east-1", s3_public_url="https://public.example",
        s3_bucket="tenant-files",
    ))
    monkeypatch.setattr(s3_adapter, "settings", configured)
    monkeypatch.setattr(audit_log, "settings", configured)
    for name in ("_internal_client", "_presign_client", "_session"):
        monkeypatch.setattr(s3_adapter, name, None, raising=False)
    monkeypatch.setattr(s3_adapter, "_bucket_verified", set())
    monkeypatch.setattr(audit_log, "_s3", None)
    # Deliberately hostile ambient values must never affect managed identity.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-session")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam:::role/wrong-tenant")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("AWS_PROFILE", "untrusted-missing-profile")
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", "https://untrusted.example")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://untrusted.example")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    class Storage:
        settings = configured
        token_path = token
        requests = []
        exchanges = []
        duration = 3600
        reject = False

    state = Storage()

    def send(_self, request):
        state.requests.append(request)
        if urlsplit(request.url).hostname == "sts.example":
            assert "Authorization" not in request.headers
            params = parse_qs(request.body.decode() if isinstance(request.body, bytes) else request.body)
            assert params["Action"] == ["AssumeRoleWithWebIdentity"]
            assert params["RoleArn"] == [configured.s3_role_arn]
            state.exchanges.append(params)
            if state.reject:
                return AWSResponse(request.url, 403, {}, _Body(
                    b"<ErrorResponse><Error><Code>AccessDenied</Code><Message>rejected</Message></Error></ErrorResponse>"
                ))
            generation = len(state.exchanges)
            expiry = (datetime.now(timezone.utc) + timedelta(seconds=state.duration)).isoformat()
            payload = (
                '<AssumeRoleWithWebIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
                '<AssumeRoleWithWebIdentityResult><Credentials>'
                f'<AccessKeyId>ASIAFIXTURE{generation:06}</AccessKeyId>'
                '<SecretAccessKey>offline-fixture-secret</SecretAccessKey>'
                f'<SessionToken>temporary-session-{generation}</SessionToken><Expiration>{expiry}</Expiration>'
                '</Credentials></AssumeRoleWithWebIdentityResult></AssumeRoleWithWebIdentityResponse>'
            ).encode()
            return AWSResponse(request.url, 200, {}, _Body(payload))
        assert urlsplit(request.url).hostname == "storage.example"
        return AWSResponse(request.url, 200, {}, _Body(b""))

    monkeypatch.setattr(URLLib3Session, "send", send)
    return state


def _query(signed):
    return parse_qs(urlsplit(signed.url).query)


def test_managed_server_and_public_signer_share_refreshable_session(storage):
    private = s3_adapter.client()
    public = s3_adapter.presign_client()
    assert private._request_signer._credentials is public._request_signer._credentials
    s3_adapter.head("hello.bin")
    assert storage.exchanges[0]["WebIdentityToken"] == ["projected-first"]
    request = storage.requests[-1]
    assert b"ASIAFIXTURE000001" in request.headers["Authorization"]
    assert request.headers["X-Amz-Security-Token"] == b"temporary-session-1"
    signed = s3_adapter.presign_put("hello.bin")
    assert _query(signed)["X-Amz-Security-Token"] == ["temporary-session-1"]
    assert urlsplit(signed.url).path == "/tenant-files/hello.bin"
    assert urlsplit(signed.url).hostname == "public.example"
    assert len(storage.exchanges) == 1


def test_rotated_token_refreshes_both_existing_clients(storage):
    s3_adapter.presign_get("before")
    credentials = s3_adapter.client()._request_signer._credentials
    storage.token_path.write_text("projected-second\n")
    credentials._expiry_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    signed = s3_adapter.presign_put("after")
    s3_adapter.head("after")
    assert len(storage.exchanges) == 2
    assert storage.exchanges[1]["WebIdentityToken"] == ["projected-second"]
    assert _query(signed)["X-Amz-Security-Token"] == ["temporary-session-2"]
    assert storage.requests[-1].headers["X-Amz-Security-Token"] == b"temporary-session-2"


def test_presign_clamps_ttl_and_returns_actual_expiry(storage):
    signed = s3_adapter.presign_get("download", ttl=7200)
    assert 3500 <= signed.expires_in <= 3540
    assert _query(signed)["X-Amz-Expires"] == [str(signed.expires_in)]
    assert s3_adapter.presign_put("upload", ttl=30).expires_in == 30


def test_signing_keeps_exact_snapshot_when_another_request_refreshes(storage):
    public = s3_adapter.presign_client()
    s3_adapter.presign_get("seed")
    credentials = public._request_signer._credentials

    def refresh_shared_generation(**_kwargs):
        storage.token_path.write_text("projected-second")
        credentials._expiry_time = datetime.now(timezone.utc) - timedelta(seconds=1)
        credentials.get_frozen_credentials()

    public.meta.events.register_first("before-sign.s3.GetObject", refresh_shared_generation)
    signed = s3_adapter.presign_get("snapshot", ttl=7200)
    assert len(storage.exchanges) == 2
    assert _query(signed)["X-Amz-Security-Token"] == ["temporary-session-1"]
    assert 3500 <= signed.expires_in <= 3540
    assert credentials.token == "temporary-session-2"


def test_concurrent_first_signers_share_one_exchange(storage):
    with ThreadPoolExecutor(max_workers=8) as pool:
        signed = list(pool.map(lambda i: s3_adapter.presign_get(f"object-{i}"), range(16)))
    assert len(storage.exchanges) == 1
    assert all(_query(url)["X-Amz-Security-Token"] == ["temporary-session-1"] for url in signed)


@pytest.mark.parametrize("failure", ["missing-token", "sts-denied", "near-expiry", "expired"])
def test_unusable_identity_never_falls_back_to_ambient_keys(storage, failure):
    if failure == "missing-token":
        storage.token_path.unlink()
    elif failure == "sts-denied":
        storage.reject = True
    else:
        storage.duration = 30 if failure == "near-expiry" else -1
    with pytest.raises(AKBError):
        s3_adapter.presign_get("denied")


def test_managed_audit_upload_uses_the_same_session(storage):
    storage.settings.audit.bucket = "audit-files"
    audit_client = audit_log._s3_client()
    assert audit_client._request_signer._credentials is s3_adapter.client()._request_signer._credentials
    audit_client.put_object(Bucket="audit-files", Key="audit/day.json", Body=b"{}")
    assert storage.requests[-1].headers["X-Amz-Security-Token"] == b"temporary-session-1"


def test_managed_static_client_factory_is_not_an_escape(storage):
    with pytest.raises(AKBError):
        s3_adapter.make_client("https://storage.example", "static", "static")


@pytest.mark.parametrize("mode,managed,creates", [
    ("static", False, True), ("default_chain", False, False), ("default_chain", True, False),
])
def test_bucket_creation_only_exists_for_standalone_static(monkeypatch, mode, managed, creates):
    values = managed_values() if managed else {}
    monkeypatch.setattr(s3_adapter, "settings", Settings(**{**values, "s3_auth_mode": mode}))
    monkeypatch.setattr(s3_adapter, "_bucket_verified", set())

    class Client:
        created = []

        def head_bucket(self, **kwargs):
            raise ClientError({"Error": {"Code": "NoSuchBucket"}}, "HeadBucket")

        def create_bucket(self, **kwargs):
            self.created.append(kwargs["Bucket"])

    client = Client()
    monkeypatch.setattr(s3_adapter, "client", lambda: client)
    if creates:
        s3_adapter.ensure_bucket("tenant")
        assert client.created == ["tenant"]
    else:
        with pytest.raises(AKBError):
            s3_adapter.ensure_bucket("tenant")
        assert client.created == []


def test_bucket_verification_cache_is_per_bucket(monkeypatch):
    seen = []

    class Client:
        def head_bucket(self, **kwargs):
            seen.append(kwargs["Bucket"])

    monkeypatch.setattr(s3_adapter, "_bucket_verified", set())
    monkeypatch.setattr(s3_adapter, "client", lambda: Client())
    for bucket in ("first", "second", "first"):
        s3_adapter.ensure_bucket(bucket)
    assert seen == ["first", "second"]


def test_standalone_native_chain_uses_standard_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-credentials"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "cloud-fixture")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-fixture-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "cloud-session")
    monkeypatch.setattr(s3_adapter, "settings", Settings(s3_auth_mode="default_chain", s3_region="us-east-1"))
    for name in ("_internal_client", "_presign_client", "_session"):
        monkeypatch.setattr(s3_adapter, name, None, raising=False)
    signed = s3_adapter.presign_get("native-cloud")
    assert _query(signed)["X-Amz-Security-Token"] == ["cloud-session"]
    assert signed.expires_in == 3600


def test_standalone_static_presign_keeps_existing_keys(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setattr(s3_adapter, "settings", Settings(
        s3_endpoint_url="http://minio:9000", s3_access_key="local-key", s3_secret_key="local-fixture",
    ))
    for name in ("_internal_client", "_presign_client", "_session"):
        monkeypatch.setattr(s3_adapter, name, None, raising=False)
    signed = s3_adapter.presign_put("static")
    assert _query(signed)["X-Amz-Credential"][0].startswith("local-key/")
    assert "X-Amz-Security-Token" not in _query(signed)
    assert signed.expires_in == 3600


def test_standalone_rgw_identity_needs_no_model_gateway(storage):
    storage.settings.model_api_governance_mode = "external_metering"
    storage.settings.platform_gateway_base_url = ""
    storage.settings.platform_gateway_token_file = ""
    signed = s3_adapter.presign_put("standalone-rgw")
    assert _query(signed)["X-Amz-Security-Token"] == ["temporary-session-1"]


def test_standalone_native_webidentity_provider_is_preserved(storage, monkeypatch, tmp_path):
    monkeypatch.delenv("AWS_PROFILE")
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-credentials"))
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", str(storage.token_path))
    monkeypatch.setenv("AWS_ROLE_ARN", storage.settings.s3_role_arn)
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", "https://sts.example")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://storage.example")
    monkeypatch.setattr(s3_adapter, "settings", Settings(s3_auth_mode="default_chain", s3_region="us-east-1"))
    signed = s3_adapter.presign_get("native-webidentity")
    assert _query(signed)["X-Amz-Security-Token"] == ["temporary-session-1"]
    assert 3500 <= signed.expires_in <= 3540


def test_failed_mandatory_refresh_does_not_use_expired_session(storage):
    s3_adapter.presign_get("before")
    credentials = s3_adapter.client()._request_signer._credentials
    credentials._expiry_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    storage.reject = True
    with pytest.raises(AKBError):
        s3_adapter.head("after")
    assert all(urlsplit(request.url).hostname == "sts.example" for request in storage.requests)


def test_standalone_audit_credentials_remain_isolated(monkeypatch, tmp_path):
    configured = Settings(
        s3_endpoint_url="http://minio:9000", s3_access_key="file-key", s3_secret_key="file-fixture",
        audit={"access_key": "audit-key", "secret_key": "audit-fixture"},
    )
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-config"))
    monkeypatch.setattr(s3_adapter, "settings", configured)
    monkeypatch.setattr(audit_log, "settings", configured)
    monkeypatch.setattr(audit_log, "_s3", None)
    credentials = audit_log._s3_client()._request_signer._credentials.get_frozen_credentials()
    assert credentials.access_key == "audit-key"
