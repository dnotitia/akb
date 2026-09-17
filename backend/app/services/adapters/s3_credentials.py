"""S3 credential selection and atomic snapshots of refreshable credentials.

Botocore owns refresh serialization. Its expiration and frozen-credential
fields have no public atomic accessor; the small snapshot function below is
covered against the botocore version in uv.lock, including concurrent refresh.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.credentials import (
    CredentialProvider, CredentialResolver, Credentials,
    DeferredRefreshableCredentials, RefreshableCredentials,
)
from botocore.session import Session

from app.config import Settings
from app.services.workload_identity import WorkloadIdentityError, read_projected_token


class _WebIdentityProvider(CredentialProvider):
    METHOD = "assume-role-with-web-identity"

    def __init__(self, sts, token_path: str, role_arn: str):
        self._sts = sts
        self._token_path = token_path
        self._role_arn = role_arn
        self._session_name = "akb-" + uuid.uuid4().hex

    def load(self):
        credentials = DeferredRefreshableCredentials(
            refresh_using=self._refresh, method=self.METHOD,
        )
        credentials._advisory_refresh_timeout = 300
        credentials._mandatory_refresh_timeout = 60
        return credentials

    def _refresh(self):
        try:
            response = self._sts.assume_role_with_web_identity(
                RoleArn=self._role_arn,
                RoleSessionName=self._session_name,
                WebIdentityToken=read_projected_token(self._token_path),
            )["Credentials"]
            if not all(response.get(key) for key in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")):
                raise ValueError("Incomplete STS credentials")
            return {
                "access_key": response["AccessKeyId"],
                "secret_key": response["SecretAccessKey"],
                "token": response["SessionToken"],
                "expiry_time": response["Expiration"].isoformat(),
            }
        except Exception:
            # STS error text and validation errors can contain caller input.
            # Botocore logs refresh exceptions; expose only this safe diagnostic.
            raise WorkloadIdentityError("S3 workload identity refresh failed") from None


def build_session(settings: Settings) -> boto3.Session:
    core = Session()
    managed = settings.model_api_governance_mode == "platform_hard"
    if managed and (settings.s3_auth_mode != "default_chain" or settings.s3_access_key or settings.s3_secret_key):
        raise WorkloadIdentityError("Static S3 credentials are forbidden in platform_hard mode")
    if managed or settings.s3_web_identity_token_file:
        if not all((settings.s3_web_identity_token_file, settings.s3_role_arn, settings.s3_sts_endpoint_url)):
            raise WorkloadIdentityError("S3 workload identity configuration is incomplete")
        # Do not read shared profiles, credential_process, or ambient AWS role
        # selectors. ConfigValueStore overrides also suppress a set AWS_PROFILE
        # when the selected value is None (Session.set_config_variable does not).
        config_store = core.get_component("config_store")
        for name, value in (("profile", None), ("config_file", os.devnull), ("credentials_file", os.devnull)):
            config_store.set_config_variable(name, value)
        config_store.set_config_variable("region", settings.s3_region or "us-east-1")
        sts = core.create_client(
            "sts", endpoint_url=settings.s3_sts_endpoint_url,
            region_name=settings.s3_region or "us-east-1",
            config=Config(
                signature_version=UNSIGNED,
                ignore_configured_endpoint_urls=True,
                connect_timeout=settings.s3_connect_timeout_secs,
                read_timeout=settings.s3_read_timeout_secs,
                retries={"max_attempts": settings.s3_max_attempts, "mode": "standard"},
            ),
        )
        core.register_component("credential_provider", CredentialResolver([
            _WebIdentityProvider(sts, settings.s3_web_identity_token_file, settings.s3_role_arn),
        ]))
    elif settings.s3_auth_mode == "static":
        # Set even empty values explicitly. boto3.Session's constructor treats
        # empty strings as absent and would silently select ambient credentials.
        core.set_credentials(settings.s3_access_key, settings.s3_secret_key)
    return boto3.Session(botocore_session=core)


def frozen_snapshot(session: boto3.Session) -> tuple[Credentials, datetime | None]:
    """Freeze the signing keys and their expiration from the same generation."""
    try:
        credentials = session.get_credentials()
        if credentials is None:
            raise WorkloadIdentityError("S3 credentials are unavailable")
        frozen = credentials.get_frozen_credentials()
        expiry = None
        if isinstance(credentials, RefreshableCredentials):
            # Refresh can finish between get_frozen_credentials and this lock.
            # Read BOTH fields again under the provider's own refresh lock.
            with credentials._refresh_lock:
                frozen = credentials._frozen_credentials
                expiry = credentials._expiry_time
        return Credentials(frozen.access_key, frozen.secret_key, frozen.token), expiry
    except WorkloadIdentityError:
        raise
    except Exception:
        raise WorkloadIdentityError("S3 credentials are unavailable") from None
