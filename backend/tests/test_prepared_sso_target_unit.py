"""Protected host input and active runtime gates, without a database."""

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx
import jwt

from app.services.standalone_sso_bootstrap import MANAGEMENT_REALM_ROLES, StandaloneSSOReadback
from app.services.standalone_sso_keycloak import KeycloakStandaloneSSOControl
from app.sso.prepare_target_host import ExistingTargetControl, HostPreparationInput, canonical, digest, protected_read, protected_write
from app.sso.adopt_prepared_target import PreparedTargetReceipt, validate_runtime_binding


def bootstrap_jwt(**changes):
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {"iat": now, "exp": now + 60, "typ": "Bearer", "iss": "https://identity.example/realms/master"}
    claims.update(changes)
    return jwt.encode(claims, secrets.token_bytes(32), algorithm="HS256")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["malformed", "long", "expired", "future", "zero", "wrong_type", "wrong_issuer", "missing_iat", "bool_exp"])
async def test_target_bootstrap_rejects_invalid_endpoint_tokens_before_admin_calls(case):
    now = int(datetime.now(timezone.utc).timestamp())
    tokens = {"malformed": "not-a-jwt", "long": bootstrap_jwt(exp=now + 3600),
              "expired": bootstrap_jwt(iat=now - 120, exp=now - 60),
              "future": bootstrap_jwt(iat=now + 60, exp=now + 120),
              "zero": bootstrap_jwt(exp=now), "wrong_type": bootstrap_jwt(typ="ID"),
              "wrong_issuer": bootstrap_jwt(iss="https://foreign.example/realms/master"),
              "missing_iat": bootstrap_jwt(iat=None), "bool_exp": bootstrap_jwt(exp=True)}
    p = preparation()
    requests = []

    def endpoint(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"access_token": tokens[case]})

    control = ExistingTargetControl(p)
    control._clients[p.internal_url] = httpx.AsyncClient(base_url=p.internal_url, transport=httpx.MockTransport(endpoint))
    try:
        with pytest.raises(RuntimeError, match="target_bootstrap_token_invalid"):
            await control.acquire_bootstrap(p.spec())
        assert requests == [("POST", "/realms/master/protocol/openid-connect/token")]
    finally:
        await control.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifetime", [60, 600, None])
async def test_target_bootstrap_accepts_bounded_endpoint_token_or_retired_authority(lifetime):
    p = preparation()
    now = int(datetime.now(timezone.utc).timestamp())
    token = bootstrap_jwt(iat=now, exp=now + lifetime) if lifetime is not None else None
    requests = []

    def endpoint(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"access_token": token}) if token else httpx.Response(401)

    control = ExistingTargetControl(p)
    control._clients[p.internal_url] = httpx.AsyncClient(base_url=p.internal_url, transport=httpx.MockTransport(endpoint))
    try:
        assert await control.acquire_bootstrap(p.spec()) == token
        assert requests == [("POST", "/realms/master/protocol/openid-connect/token")]
    finally:
        await control.aclose()


@pytest.mark.asyncio
async def test_default_standalone_bootstrap_token_policy_is_unchanged():
    p = preparation()
    token = bootstrap_jwt(exp=int(datetime.now(timezone.utc).timestamp()) + 3600)
    control = KeycloakStandaloneSSOControl()
    control._clients[p.internal_url] = httpx.AsyncClient(base_url=p.internal_url,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"access_token": token})))
    try:
        assert await control.acquire_bootstrap(p.spec()) == token
    finally:
        await control.aclose()


def preparation(**changes):
    values = dict(operation_id=uuid4(), org_id=uuid4(), target_id=uuid4(), realm_id=uuid4(), session_epoch=uuid4(),
                  realm_name="akb", issuer="https://identity.example/realms/akb", internal_url="http://keycloak:8080",
                  akb_public_url="https://workspace.example", backchannel_logout_uri="http://backend:8000/api/v1/auth/keycloak/backchannel-logout",
                  bootstrap_client_id="bootstrap-once", bootstrap_client_secret="host-bootstrap-unique-material",
                  management_client_id="akb-sso-manager", management_client_secret="manager-unique-material",
                  api_client_id="akb-web", api_client_secret="web-unique-material", admin_client_id="akb-admin",
                  admin_client_secret="admin-unique-material", browser_session_key="browser-unique-material-of-32-bytes-or-more",  # pragma: allowlist secret
                  product_admin_subject=uuid4(), product_admin_email="recovery@example.invalid",
                  product_admin_password="native-unique-credential", cohort=[])  # pragma: allowlist secret
    return HostPreparationInput(**(values | changes))


def bound_fixture():
    p = preparation()
    rb = StandaloneSSOReadback(str(p.realm_id), str(p.product_admin_subject), "admin", "manager", "web", "kid", 3072, 1,
                              MANAGEMENT_REALM_ROLES, MANAGEMENT_REALM_ROLES, "pwd", 0)
    target = dict(target_id=str(p.target_id), realm_id=str(p.realm_id), realm_name=p.realm_name, issuer=p.issuer,
                  internal_url=p.internal_url, management_client_id=p.management_client_id,
                  management_client_secret=p.management_client_secret.get_secret_value(), session_epoch=str(p.session_epoch),
                  verify_ssl=True)
    descriptor = canonical({"schema_version": 1, "targets": [target]})
    values = p.model_dump(mode="json", exclude={"verify_ssl", "bootstrap_client_secret", "management_client_secret", "api_client_secret",
        "admin_client_secret", "browser_session_key", "product_admin_password", "product_admin_subject"})
    values.update(schema_version=1, product_admin_username=p.product_admin_username, config_sha256=digest(descriptor),
                  active_credentials_sha256=digest(canonical({"api_client_secret": p.api_client_secret.get_secret_value(),
                      "admin_client_secret": p.admin_client_secret.get_secret_value(),
                      "management_client_secret": p.management_client_secret.get_secret_value(),
                      "browser_session_key": p.browser_session_key.get_secret_value()})),
                  input_binding_sha256="a" * 64, cohort_sha256=digest(canonical([])), profile="bundled-keycloak-v6",
                  readback=asdict(rb), prepared_at=datetime.now(timezone.utc), authority_retired_at=datetime.now(timezone.utc),
                  retirement_evidence={"new_token_denied": True, "prior_token_master_call_denied": True})
    receipt = PreparedTargetReceipt(**values)
    settings = SimpleNamespace(require_auth_mode=lambda: "sso", keycloak_enabled=True, keycloak_public_client=False,
        sso_prepared_target_id=str(p.target_id), sso_session_epoch=p.session_epoch, keycloak_server_url="https://identity.example",
        keycloak_internal_url=p.internal_url, keycloak_realm=p.realm_name, keycloak_client_id=p.api_client_id,
        keycloak_admin_client_id=p.admin_client_id, keycloak_management_client_id=p.management_client_id,
        keycloak_client_secret=p.api_client_secret.get_secret_value(), keycloak_admin_client_secret=p.admin_client_secret.get_secret_value(),
        keycloak_management_client_secret=p.management_client_secret.get_secret_value(), keycloak_verify_ssl=True,
        sso_browser_session_encryption_key=p.browser_session_key.get_secret_value(), public_base_url=p.akb_public_url,
        keycloak_backchannel_logout_uri_effective=p.backchannel_logout_uri, sso_local_realm_self_registration=False)
    return receipt, descriptor, settings


def test_runtime_accepts_exact_binding_with_no_bootstrap_material():
    receipt, descriptor, settings = bound_fixture()
    spec = validate_runtime_binding(receipt, descriptor, settings)
    assert spec.bootstrap_client_secret == spec.product_admin_password == ""


@pytest.mark.parametrize("field,value", [("sso_prepared_target_id", str(uuid4())), ("keycloak_server_url", "https://source.example"),
    ("keycloak_internal_url", "http://source:8080"), ("keycloak_client_id", "other"), ("keycloak_management_client_secret", "wrong"),
    ("sso_browser_session_encryption_key", "wrong"), ("keycloak_enabled", False), ("sso_session_epoch", uuid4())])
def test_runtime_refuses_unswitched_or_changed_runtime(field, value):
    receipt, descriptor, settings = bound_fixture()
    setattr(settings, field, value)
    with pytest.raises(RuntimeError):
        validate_runtime_binding(receipt, descriptor, settings)


@pytest.mark.parametrize("change", [{"realm_id": uuid4()}, {"profile": "bundled-keycloak-v5"},
                                  {"retirement_evidence": {"new_token_denied": True}}, {"cohort_sha256": "0" * 64}])
def test_runtime_refuses_receipt_mismatch(change):
    receipt, descriptor, settings = bound_fixture()
    with pytest.raises(RuntimeError):
        validate_runtime_binding(receipt.model_copy(update=change), descriptor, settings)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o666])
def test_protected_input_requires_exact_private_permissions(tmp_path, mode):
    path = tmp_path / "input.json"
    path.write_text("{}")
    path.chmod(mode)
    with pytest.raises(RuntimeError):
        protected_read(path)


def test_atomic_private_output_and_symlink_refusal(tmp_path):
    path = tmp_path / "input.json"
    protected_write(path, {"a": 1})
    assert json.loads(protected_read(path)) == {"a": 1}
    assert path.stat().st_mode & 0o777 == 0o600
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        protected_read(alias)


def test_cohort_duplicate_or_large_or_recovery_collision_refused():
    member = dict(akb_user_id=uuid4(), broker_subject=uuid4(), broker_username="user", provider_alias="platform",
                  upstream_issuer="https://upstream.example/realms/platform", upstream_subject="opaque")
    with pytest.raises(ValueError):
        preparation(cohort=[member, member])
    with pytest.raises(ValueError):
        preparation(cohort=[member] * 26)
    with pytest.raises(ValueError):
        preparation(realm_name="master")


def test_host_leaf_imports_no_database_or_active_settings():
    path = Path(__file__).resolve().parents[1] / "app/sso/prepare_target_host.py"
    source = path.read_text()
    assert "from app.db" not in source and "from app.config" not in source


@pytest.mark.asyncio
async def test_cleanup_rechecks_each_user_after_cohort_preflight(tmp_path, monkeypatch):
    from app.sso import cleanup_prepared_target as cleanup

    receipt, _, _ = bound_fixture()
    p = preparation(operation_id=receipt.operation_id, target_id=receipt.target_id, realm_id=receipt.realm_id,
                    org_id=receipt.org_id, source_guard={"auth_mode": "local", "runtime_generation": 1,
                                                       "source_revision": "a" * 40})
    subjects = [str(uuid4()), str(uuid4())]
    assets = [{"subject": subject, "owned": True, "fingerprint_sha256": "original"} for subject in subjects]
    receipt = receipt.model_copy(update={"source_guard": p.source_guard, "owned_assets": assets})
    state = {asset["subject"]: dict(asset) for asset in assets}
    deleted = []

    class Control:
        def __init__(self, _preparation):
            pass

        async def acquire_bootstrap(self, _spec):
            return "fixture-token"

        async def require_realm(self, _spec, **_kwargs):
            pass

        async def user_fingerprint(self, _spec, subject, **_kwargs):
            # A credential/action changes while another user's preflight yields.
            if subject == subjects[1] and subjects[0] in state:
                state[subjects[0]]["fingerprint_sha256"] = "changed"
            return dict(state[subject]) if subject in state else None

        async def _request(self, _spec, method, path, **_kwargs):
            assert method == "DELETE"
            subject = path.rsplit("/", 1)[-1]
            deleted.append(subject)
            del state[subject]

        async def retire_bootstrap(self, _spec, **_kwargs):
            pass

        async def assert_bootstrap_retired(self, _spec, **_kwargs):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(cleanup, "ExistingTargetControl", Control)
    authority = cleanup.CleanupAuthority(operation_id=p.operation_id, target_id=p.target_id, realm_id=p.realm_id,
                                         client_id="cleanup-once", client_secret="fixture-cleanup-material")
    with pytest.raises(RuntimeError, match="target_cleanup_owned_user_changed"):
        await cleanup.cleanup_target(p, receipt, authority, tmp_path / "compensation.json")
    assert deleted == []
    assert not (tmp_path / "compensation.json").exists()


class InterruptedAllocationControl(ExistingTargetControl):
    """Stateful transport fixture; real allocation/census/compensation methods run."""

    def __init__(self, p, state):
        super().__init__(p)
        self.state = state

    async def acquire_bootstrap(self, _spec):
        return "fixture-token"

    async def require_realm(self, _spec, **_kwargs):
        pass

    async def reconcile(self, _spec, **_kwargs):
        return StandaloneSSOReadback(str(self.preparation.realm_id), str(self.preparation.product_admin_subject),
                                    "admin", "manager", "web", "kid", 3072, 1,
                                    MANAGEMENT_REALM_ROLES, MANAGEMENT_REALM_ROLES, "pwd", 0)

    async def configure_approved_provider(self, _spec):
        pass

    async def _json(self, spec, path, **kwargs):
        if path.endswith("/client-secret"):
            client = path.split("/")[-2]
            return {"value": {"web": spec.api_client_secret, "admin": spec.admin_client_secret,
                               "manager": spec.management_client_secret}[client]}
        if "/identity-provider/instances/" in path:
            return {"config": {"issuer": self.preparation.cohort[0].upstream_issuer}}
        if path.endswith("/users"):
            return list(self.state["users"].values())
        if path.endswith("/federated-identity"):
            return self.state["links"].get(path.split("/")[-2], [])
        if path.endswith("/credentials"):
            return []
        if "/roles/" in path:
            return {"id": str(uuid4()), "name": "fixture-guard"}
        raise AssertionError(path)

    async def _request(self, _spec, method, path, **kwargs):
        import httpx
        body = kwargs.get("json_body")
        if method == "POST" and path.endswith("/users"):
            subject = str(uuid4())
            self.state["users"][subject] = dict(body, id=subject)
            return httpx.Response(201)
        if method == "POST" and "/federated-identity/" in path:
            self.state["links"][path.split("/")[-3]] = [body]
            return httpx.Response(204)
        if method == "POST" and path.endswith("/role-mappings/realm"):
            raise RuntimeError("fixture_interrupted_after_link")
        subject = path.rsplit("/", 1)[-1]
        if method == "GET":
            user = self.state["users"].get(subject)
            return httpx.Response(200, json=user) if user else httpx.Response(404)
        if method == "DELETE":
            del self.state["users"][subject]
            self.state["deleted"].append(subject)
            self.state["links"].pop(subject, None)
            return httpx.Response(204)
        raise AssertionError((method, path))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_checkpoint", [False, True])
async def test_interrupted_allocated_cohort_journals_actual_uuid_and_compensates_after_restart(tmp_path, missing_checkpoint):
    from app.sso.prepare_target_host import prepare_target

    member = dict(akb_user_id=uuid4(), broker_subject=None, broker_username="approved-person", provider_alias="platform",
                  upstream_issuer="https://upstream.example/realms/platform", upstream_subject="opaque")
    p = preparation(cohort=[member])
    state = {"users": {}, "links": {}, "deleted": []}
    control = InterruptedAllocationControl(p, state)
    try:
        with pytest.raises(RuntimeError, match="fixture_interrupted_after_link"):
            await prepare_target(p, tmp_path, control=control)
    finally:
        await control.aclose()
    subject = next(iter(state["users"]))
    journal = json.loads(protected_read(tmp_path / "host-authority-journal.json"))
    assert journal["phase"] == "preparing"
    assert journal["cohort"][0]["broker_subject"] == subject
    assert state["links"][subject][0]["userId"] == "opaque"
    if missing_checkpoint:
        # Old/incomplete journal, including a crash after a successful POST but
        # before readback/checkpoint: exact marked username recovers the UUID.
        journal.pop("cohort")
        protected_write(tmp_path / "host-authority-journal.json", journal)
    restarted = InterruptedAllocationControl(p, state)
    try:
        result = await prepare_target(p, tmp_path, control=restarted, compensate_only=True)
        assert result == {"compensated": True, "deleted_owned_users": 1}
        assert state["deleted"] == [subject] and state["users"] == {}
        durable = json.loads(protected_read(tmp_path / "host-authority-journal.json"))
        assert durable["cohort"][0]["broker_subject"] == subject
        # A later same-name object must never replace the pinned UUID.
        replacement = str(uuid4())
        state["users"][replacement] = {"id": replacement, "username": "approved-person", "enabled": True,
            "attributes": {"akb.preparation.operation": [str(p.operation_id)]}}
        assert await prepare_target(p, tmp_path, control=restarted, compensate_only=True) == {
            "compensated": True, "deleted_owned_users": 0}
        assert list(state["users"]) == [replacement] and state["deleted"] == [subject]
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("broker_username", "other"), ("upstream_subject", "other"),
                                         ("akb_user_id", str(uuid4()))])
async def test_recovered_cohort_cannot_widen_original_intent(field, value):
    member = dict(akb_user_id=uuid4(), broker_subject=None, broker_username="approved-person", provider_alias="platform",
                  upstream_issuer="https://upstream.example/realms/platform", upstream_subject="opaque")
    p = preparation(cohort=[member])
    control = ExistingTargetControl(p)
    try:
        changed = p.cohort[0].model_dump(mode="json") | {"broker_subject": str(uuid4()), field: value}
        with pytest.raises(RuntimeError, match="target_cohort_journal_mismatch"):
            control.restore_cohort([changed])
    finally:
        await control.aclose()
