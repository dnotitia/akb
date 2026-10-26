"""Protected CLI input, fixed wire and generic HTTP issuer boundary."""
import base64
import json
import os
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest

from app import cli
from app.exceptions import AKBError
from app.services import platform_service_bootstrap as bootstrap


def wire():
    return {"schema_version": 1, "purpose": "platform-service-v1", "operation_id": str(uuid.uuid4()),
            "org_id": str(uuid.uuid4()), "tenant_slug": "workspace-test", "namespace": "akb-workspace-test",
            "owner_uid": str(uuid.uuid4()), "token_id": str(uuid.uuid4()),
            "credential": "akb_secret_" + base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")}


def test_valid_native_intent_hash_is_canonical_and_repr_secret_free():
    value = wire()
    parsed = bootstrap.parse_intent(value)
    assert parsed == bootstrap.parse_intent(dict(reversed(list(value.items()))))
    assert value["credential"] not in repr(parsed)
    assert parsed.credential_sha256 == bootstrap._digest(value["credential"])


@pytest.mark.parametrize("key,value", [("schema_version", True), ("purpose", "arbitrary"), ("tenant_slug", "Upper"),
    ("namespace", "foreign"), ("org_id", str(uuid.UUID(int=0))), ("owner_uid", "bad"),
    ("credential", "akb_secret_" + "a"*64), ("credential", "akb_" + "a"*43), ("credential", "akb_secret_" + "_"*43)])
def test_wire_rejects_noncanonical_input(key, value):
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.parse_intent({**wire(), key: value})


def test_wire_rejects_any_extra_authority_field():
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.parse_intent({**wire(), "is_admin": True})


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o777])
def test_intent_mode_rejected(tmp_path, mode):
    path = tmp_path / "intent.json"
    path.write_text(json.dumps(wire()))
    path.chmod(mode)
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.read_intent(str(path))


def test_atomic_secret_projection_is_readable_but_outside_symlink_refused(tmp_path):
    mount = tmp_path / "mount"
    version = mount / "..version"
    version.mkdir(parents=True)
    value = wire()
    path = version / "intent.json"
    path.write_text(json.dumps(value))
    path.chmod(0o400)
    (mount / "..data").symlink_to(version.name)
    (mount / "intent.json").symlink_to("..data/intent.json")
    assert bootstrap.read_intent(str(mount / "intent.json")) == bootstrap.parse_intent(value)
    outside = tmp_path / "outside"
    outside.write_text(json.dumps(value))
    outside.chmod(0o600)
    (mount / "escape").symlink_to(outside)
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.read_intent(str(mount / "escape"))


def test_duplicate_json_key_and_oversize_file_refused(tmp_path):
    path = tmp_path / "intent"
    path.write_text('{"schema_version":1,"schema_version":1}')
    path.chmod(0o600)
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.read_intent(str(path))
    path.write_bytes(b"x" * 16385)
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.read_intent(str(path))


def test_output_termination_file_existing_and_symlink_refusal(tmp_path):
    path = tmp_path / "termination-log"
    path.write_text("stale-long-value")
    bootstrap.write_receipt(str(path), {"ok": True})
    assert json.loads(path.read_text()) == {"ok": True}
    assert path.stat().st_mode & 0o777 == 0o600
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.write_receipt(str(link), {"overwrite": True})
    assert json.loads(path.read_text()) == {"ok": True}


def test_cli_no_raw_output_and_lost_receipt_retries_same_intent(tmp_path, monkeypatch, capsys):
    value = wire()
    intent = tmp_path / "intent"
    intent.write_text(json.dumps(value))
    intent.chmod(0o600)
    output = tmp_path / "receipt"
    calls = []
    async def initialize():
        pass
    async def close():
        pass
    async def provision(parsed):
        calls.append(parsed)
        return {"token_id": str(parsed.token_id), "status": "replayed" if len(calls)>1 else "issued"}
    monkeypatch.setattr(cli, "_initialize_operator_database", initialize)
    from app.db import postgres
    monkeypatch.setattr(postgres, "close_pool", close)
    monkeypatch.setattr(bootstrap, "provision", provision)
    original = bootstrap.write_receipt
    def failed(*_):
        raise bootstrap.BootstrapRefused("platform_bootstrap_receipt_file")
    monkeypatch.setattr(bootstrap, "write_receipt", failed)
    args = ["provision-platform-service-credential", "--intent-file", str(intent), "--output-file", str(output)]
    assert cli.main(args) == 1
    monkeypatch.setattr(bootstrap, "write_receipt", original)
    assert cli.main(args) == 0
    assert calls[0] == calls[1]
    assert json.loads(output.read_text())["status"] == "replayed"
    observed = capsys.readouterr()
    assert observed.out == "" and value["credential"] not in observed.err


def test_cli_generic_driver_exception_never_prints_secret(tmp_path, monkeypatch, capsys):
    value = wire()
    intent = tmp_path / "intent"
    intent.write_text(json.dumps(value))
    intent.chmod(0o600)
    async def initialize():
        raise RuntimeError(value["credential"])
    async def close():
        pass
    monkeypatch.setattr(cli, "_initialize_operator_database", initialize)
    from app.db import postgres
    monkeypatch.setattr(postgres, "close_pool", close)
    assert cli.main(["provision-platform-service-credential", "--intent-file", str(intent), "--output-file", str(tmp_path/"out")]) == 1
    observed = capsys.readouterr()
    assert observed.out == "" and observed.err == "platform_bootstrap_failed\n"
    assert not (tmp_path/"out").exists()


@pytest.mark.asyncio
async def test_actual_generic_http_mint_still_rejects_management_oauth():
    from app.api.routes import access
    from app.api.deps import get_current_user
    from app.services.auth_service import AuthenticatedUser
    app = FastAPI()
    app.include_router(access.router, prefix="/api/v1")
    principal = AuthenticatedUser(user_id=str(uuid.uuid4()), username="service-manager", email="service-manager@service.invalid", display_name=None,
        is_admin=True, auth_method="oauth", account_kind="service")
    app.dependency_overrides[get_current_user] = lambda: principal
    @app.exception_handler(AKBError)
    async def failure(_, exc):
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://workspace.example") as client:
        response = await client.post(f"/api/v1/admin/users/{uuid.uuid4()}/tokens", json={"name":"platform","key_class":"service","scopes":["read","write","admin"]})
    assert response.status_code == 403
    assert response.json()["code"] == "human_token_issuer_required"


def test_runtime_guards_config_before_database(monkeypatch):
    from app.config import settings
    from app.sso.runtime_identity import BASE_CAPABILITIES
    assert bootstrap.CAPABILITY in BASE_CAPABILITIES
    for key, value in {"auth_mode":"sso", "keycloak_enabled":True, "keycloak_server_url":"https://identity.example",
        "keycloak_realm":"akb", "keycloak_service_admin_client_id":"akb-sso-manager", "auth_runtime_generation":1,
        "sso_session_epoch":uuid.uuid4()}.items():
        monkeypatch.setattr(settings, key, value)
    assert bootstrap.runtime_identity()["auth_mode"] == "sso"
    for field, value in (("sso_session_epoch",None), ("auth_runtime_generation",0), ("keycloak_service_admin_client_id",""),
                         ("keycloak_server_url","http://identity.example"), ("keycloak_server_url","https://name:secret@identity.example")):  # pragma: allowlist secret -- intentionally rejected example URL
        with monkeypatch.context() as changed:
            changed.setattr(settings, field, value)
            with pytest.raises(bootstrap.BootstrapRefused):
                bootstrap.runtime_identity()


@pytest.mark.asyncio
async def test_semantic_probe_cleanup_failure_is_not_success(tmp_path, monkeypatch, capsys):
    from app.services import platform_service_bootstrap_probe as probe_module
    from types import SimpleNamespace
    async def complete(*_):
        return {"status":"verified"}
    async def failed_close():
        raise RuntimeError("sensitive driver detail")
    monkeypatch.setattr(probe_module, "probe", complete)
    monkeypatch.setattr(probe_module, "close_pool", failed_close)
    output = tmp_path/"proof"
    assert await probe_module._run(SimpleNamespace(intent_file="unused", receipt_file="unused", output_file=str(output))) == 1
    assert not output.exists()
    observed = capsys.readouterr()
    assert observed.out == "" and observed.err == "platform_bootstrap_probe_failed\n"
