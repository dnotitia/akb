"""Opt-in real Keycloak 26.7/PostgreSQL16 host preparation and adoption proof.

AKB_PREPARED_TARGET_REMOTE=1 uses only the explicitly owned private Docker
fixture host. No ambient product/provider credentials, Kubernetes or images
are built. All secret values cross SSH through stdin/private env files.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import asyncpg
import httpx
import jwt
import pytest

from app.sso.prepare_target_host import ApprovedProviderBootstrap, ExactCohortMember, ExistingTargetControl, prepare_target
from app.sso.adopt_prepared_target import PreparedTargetReceipt, fresh_target_readback, load_target_adoption, record_target_adoption
from app.services.standalone_sso_bootstrap import StandaloneSSOBootstrapError
from tests.test_prepared_sso_target_unit import preparation


IMAGE = "quay.io/keycloak/keycloak:26.7.0@sha256:0f198be292568439d700cdbfb893e69a6009bb43a94a06a945b1d3d506c76b13"
REMOTE_HOST = os.environ.get("AKB_PREPARED_TARGET_DOCKER_HOST", "")
PG_PORT = int(os.environ.get("AKB_PREPARED_TARGET_PG_PORT", "55476"))
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.environ.get("AKB_PREPARED_TARGET_REMOTE") != "1",
                                                   reason="requires explicit owned private Docker fixture")]


def remote(*args, data=None):
    if not REMOTE_HOST:
        raise RuntimeError("explicit private Docker host required")
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", REMOTE_HOST, shlex.join(args)],
                            input=data, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("private fixture command failed")
    return result.stdout.decode().strip()


@asynccontextmanager
async def fixture():
    prefix = "idp221-host-" + uuid4().hex[:12]
    private_dir = "/tmp/" + prefix
    root_password, db_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    remote("mkdir", "-m", "700", private_dir)
    tunnel = None
    resources = []
    network_created = False
    try:
        upstream_host = prefix + "-upstream"
        upstream_issuer = "https://" + upstream_host + ":8443/realms/platform"
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, upstream_host)])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(upstream_host)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).sign(key, hashes.SHA256()))
        tls_files = {"tls.key": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()),
                     "tls.crt": cert.public_bytes(serialization.Encoding.PEM)}
        discovery = {"issuer": upstream_issuer, "authorization_endpoint": upstream_issuer + "/protocol/openid-connect/auth",
                     "token_endpoint": upstream_issuer + "/protocol/openid-connect/token",
                     "jwks_uri": upstream_issuer + "/protocol/openid-connect/certs",
                     "userinfo_endpoint": upstream_issuer + "/protocol/openid-connect/userinfo",
                     "end_session_endpoint": upstream_issuer + "/protocol/openid-connect/logout",
                     "introspection_endpoint": upstream_issuer + "/protocol/openid-connect/token/introspect",
                     "response_types_supported": ["code"], "subject_types_supported": ["public"],
                     "id_token_signing_alg_values_supported": ["RS256"]}
        tls_files["upstream.py"] = ("import http.server,ssl,json\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            " def log_message(self,*args): pass\n"
            " def do_GET(self):\n"
            "  body=" + repr(json.dumps(discovery).encode()) + "\n"
            "  self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(body)\n"
            "server=http.server.HTTPServer(('0.0.0.0',8443),Handler)\n"
            "ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain('/fixture/tls.crt','/fixture/tls.key')\n"
            "server.socket=ctx.wrap_socket(server.socket,server_side=True); server.serve_forever()\n").encode()
        for filename, content in tls_files.items():
            remote("sh", "-c", f"umask 077; cat > {shlex.quote(private_dir + '/' + filename)}", data=content)
        # Keycloak's non-root process can read the public fixture CA only.
        remote("chmod", "644", private_dir + "/tls.crt")
        for filename, values in {
            "pg.env": {"POSTGRES_USER": "fixture", "POSTGRES_PASSWORD": db_password, "POSTGRES_DB": "kc"},
            "kc.env": {"KC_DB": "postgres", "KC_DB_URL": f"jdbc:postgresql://{prefix}-pg:5432/kc",
                       "KC_DB_USERNAME": "fixture", "KC_DB_PASSWORD": db_password,
                       "KC_TRUSTSTORE_PATHS": "/fixture/tls.crt",
                       "KC_BOOTSTRAP_ADMIN_USERNAME": "fixture-admin", "KC_BOOTSTRAP_ADMIN_PASSWORD": root_password},
        }.items():
            remote("sh", "-c", f"umask 077; cat > {shlex.quote(private_dir + '/' + filename)}",
                   data="".join(key + "=" + value + "\n" for key, value in values.items()).encode())
        remote("docker", "network", "create", prefix)
        network_created = True
        resources.append(upstream_host)
        remote("docker", "run", "-d", "--name", upstream_host, "--network", prefix,
               "-v", private_dir + ":/fixture:ro", "python:3.14-alpine", "python", "/fixture/upstream.py")
        resources.append(prefix + "-pg")
        remote("docker", "run", "-d", "--name", prefix + "-pg", "--network", prefix,
               "--env-file", private_dir + "/pg.env", "-p", f"127.0.0.1:{PG_PORT}:5432", "postgres:16")
        for _ in range(60):
            try:
                remote("docker", "exec", prefix + "-pg", "pg_isready", "-U", "fixture")
                break
            except RuntimeError:
                await __import__("asyncio").sleep(1)
        resources.append(prefix + "-kc")
        remote("docker", "run", "-d", "--name", prefix + "-kc", "--network", prefix,
               "-v", private_dir + "/tls.crt:/fixture/tls.crt:ro",
               "--env-file", private_dir + "/kc.env", "-p", "127.0.0.1:55890:8080", IMAGE,
               "start-dev", "--hostname", "http://127.0.0.1:55890")
        tunnel = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-N",
            "-L", "127.0.0.1:55890:127.0.0.1:55890", "-L", f"127.0.0.1:{PG_PORT}:127.0.0.1:{PG_PORT}", REMOTE_HOST],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        async with httpx.AsyncClient(base_url="http://127.0.0.1:55890", timeout=20) as client:
            for _ in range(180):
                try:
                    if (await client.get("/realms/master/.well-known/openid-configuration")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if tunnel.poll() is not None:
                    raise RuntimeError("private fixture tunnel failed")
                await __import__("asyncio").sleep(1)
            response = await client.post("/realms/master/protocol/openid-connect/token", data={
                "grant_type": "password", "client_id": "admin-cli", "username": "fixture-admin", "password": root_password})
            assert response.status_code == 200
            admin = response.json()["access_token"]
            client.headers["Authorization"] = "Bearer " + admin
            yield client, f"postgresql://fixture:{db_password}@127.0.0.1:{PG_PORT}/kc", upstream_issuer
    finally:
        if tunnel is not None:
            tunnel.terminate()
            tunnel.wait(timeout=10)
        for name in reversed(resources):
            remote("docker", "rm", "-f", "-v", name)
        if network_created:
            remote("docker", "network", "rm", prefix)
        remote("rm", "-rf", private_dir)
        remaining = remote("docker", "ps", "-a", "--filter", "name=" + prefix, "--format", "{{.Names}}")
        assert remaining == ""


async def api(client, method, path, body=None, status=200):
    response = await client.request(method, "/admin/realms/" + path, json=body)
    assert response.status_code == status, f"fixture API {method} status={response.status_code}"
    return response.json() if response.content else None


async def setup_target(client, upstream_issuer):
    p = preparation(realm_name="akb", issuer="http://127.0.0.1:55890/realms/akb", internal_url="http://127.0.0.1:55890",
                    verify_ssl=False, bootstrap_client_secret=secrets.token_urlsafe(32),
                    management_client_secret=secrets.token_urlsafe(32), api_client_secret=secrets.token_urlsafe(32),
                    admin_client_secret=secrets.token_urlsafe(32), product_admin_password=secrets.token_urlsafe(32),
                    browser_session_key=secrets.token_urlsafe(32), product_admin_subject=None, cohort=[dict(akb_user_id=uuid4(), broker_subject=None,
                        broker_username="exact-approved-person", provider_alias="platform",
                        upstream_issuer=upstream_issuer, upstream_subject="opaque-subject")],
                    source_guard={"auth_mode": "local", "runtime_generation": 1, "source_revision": "a" * 40})
    await api(client, "POST", "", {"id": str(p.realm_id), "realm": "akb", "enabled": True, "sslRequired": "none"}, 201)
    await api(client, "POST", "akb/users", {"username": "existing-realm-admin", "enabled": True,
        "credentials": [{"type": "password", "value": secrets.token_urlsafe(32), "temporary": False}]}, 201)
    native_admin = (await api(client, "GET", "akb/users?username=existing-realm-admin&exact=true"))[0]
    realm_management = (await api(client, "GET", "akb/clients?clientId=realm-management"))[0]
    full_admin_role = await api(client, "GET", "akb/clients/" + realm_management["id"] + "/roles/realm-admin")
    await api(client, "POST", "akb/users/" + native_admin["id"] + "/role-mappings/clients/" + realm_management["id"],
              [full_admin_role], 204)
    await api(client, "POST", "master/clients", {"clientId": p.bootstrap_client_id, "secret": p.bootstrap_client_secret.get_secret_value(),
        "enabled": True, "publicClient": False, "serviceAccountsEnabled": True, "protocol": "openid-connect"}, 201)
    bootstrap = (await api(client, "GET", "master/clients?clientId=" + p.bootstrap_client_id))[0]
    sa = await api(client, "GET", "master/clients/" + bootstrap["id"] + "/service-account-user")
    role = await api(client, "GET", "master/roles/admin")
    await api(client, "POST", "master/users/" + sa["id"] + "/role-mappings/realm", [role], 204)
    await api(client, "POST", "akb/identity-provider/instances", {"alias": "platform", "providerId": "oidc", "enabled": False,
        "config": {"issuer": p.cohort[0].upstream_issuer, "clientId": "upstream-client", "clientSecret": secrets.token_urlsafe(24),
                   "authorizationUrl": "https://platform.example/auth", "tokenUrl": "https://platform.example/token"}}, 201)
    return p.model_copy(update={"provider": ApprovedProviderBootstrap(display_name="Platform", issuer=upstream_issuer,
        discovery_url=upstream_issuer + "/.well-known/openid-configuration", client_id="upstream-client",
        client_secret=secrets.token_urlsafe(32), callback=p.issuer + "/broker/platform/endpoint", broker_secret_version="fixture-v1")})


async def test_real_keycloak_target_prepare_retirement_replay_and_pg_adoption(tmp_path, monkeypatch):
    async with fixture() as (client, dsn, upstream_issuer):
        p = await setup_target(client, upstream_issuer)
        async with source_database(dsn, monkeypatch) as pg_source:
            # Prove the HOST bound with a genuinely minted, signed Keycloak
            # token, before any target client/profile mutation. Default OSS
            # standalone bootstrap retains its independent token policy.
            bootstrap = (await api(client, "GET", "master/clients?clientId=" + p.bootstrap_client_id))[0]
            bootstrap["attributes"] = {**bootstrap.get("attributes", {}), "access.token.lifespan": "3600"}
            await api(client, "PUT", "master/clients/" + bootstrap["id"], bootstrap, 204)
            minted = await client.post("/realms/master/protocol/openid-connect/token", data={
                "grant_type": "client_credentials", "client_id": p.bootstrap_client_id,
                "client_secret": p.bootstrap_client_secret.get_secret_value()})
            assert minted.status_code == 200
            claims = jwt.decode(minted.json()["access_token"], options={"verify_signature": False})
            assert claims["exp"] - claims["iat"] == 3600
            with pytest.raises(RuntimeError, match="target_bootstrap_token_invalid"):
                await prepare_target(p, tmp_path / "overlong")
            assert await api(client, "GET", "akb/clients?clientId=" + p.api_client_id) == []
            bootstrap["attributes"]["access.token.lifespan"] = "60"
            await api(client, "PUT", "master/clients/" + bootstrap["id"], bootstrap, 204)
            # Refuse actual UUID mismatch before any profile/client/user mutation.
            wrong = p.model_copy(update={"realm_id": uuid4()})
            with pytest.raises(RuntimeError, match="target_realm_identity_mismatch"):
                await prepare_target(wrong, tmp_path / "wrong")
            assert await api(client, "GET", "akb/clients?clientId=" + p.api_client_id) == []
            await negative_cohorts(client, p)
            await api(client, "DELETE", "akb/identity-provider/instances/platform", status=204)
            output = tmp_path / "prepared"
            class LostResponseControl(ExistingTargetControl):
                native_posts = 0
                cohort_posts = 0
                retirement_deletes = 0

                async def _request(self, spec, method, path, **kwargs):
                    response = await super()._request(spec, method, path, **kwargs)
                    body = kwargs.get("json_body")
                    if method == "POST" and path.endswith("/users") and isinstance(body, dict):
                        if body.get("username") == p.product_admin_username:
                            self.native_posts += 1
                            if self.native_posts == 1:
                                raise StandaloneSSOBootstrapError("fixture_lost_native_create_response")
                        if body.get("username") == p.cohort[0].broker_username:
                            self.cohort_posts += 1
                            raise StandaloneSSOBootstrapError("fixture_lost_cohort_create_response")
                    if method == "DELETE" and "/admin/realms/master/clients/" in path:
                        self.retirement_deletes += 1
                        raise StandaloneSSOBootstrapError("fixture_lost_retirement_response")
                    return response

            lossy = LostResponseControl(p)
            try:
                with pytest.raises(RuntimeError, match="fixture_lost_native_create_response"):
                    await prepare_target(p, output, control=lossy)
                await prove_partial_allocation(client, p, tmp_path / "partial")
                receipt = await prepare_target(p, output, control=lossy)
                assert (lossy.native_posts, lossy.cohort_posts, lossy.retirement_deletes) == (1, 1, 1)
            finally:
                await lossy.aclose()
            assert receipt["retirement_evidence"] == {"new_token_denied": True, "prior_token_master_call_denied": True}
            assert not (output / "host-authority-journal.json").exists()
            assert p.bootstrap_client_secret.get_secret_value() not in (output / "target-preparation.json").read_text()
            assert p.product_admin_password.get_secret_value() not in (output / "target-preparation.json").read_text()
            manager = ExistingTargetControl(p)
            try:
                assert await manager.acquire_bootstrap(p.spec()) is None
                token = await manager.acquire_management(p.spec())
                denied = await manager._request(p.spec(), "POST", "/admin/realms/akb/users", token=token,
                                               json_body={"username": "forbidden"}, expected=frozenset({403}))
                assert denied.status_code == 403
                denied = await manager._request(p.spec(), "POST", "/admin/realms/akb/clients", token=token,
                                               json_body={"clientId": "forbidden"}, expected=frozenset({403}))
                assert denied.status_code == 403
                assert await prepare_target(p, output) == receipt
                parsed = PreparedTargetReceipt(**receipt)
                await fresh_target_readback(parsed, replace(p.spec(), bootstrap_client_secret="", product_admin_password=""), manager)
                with pytest.raises(RuntimeError, match="prepared_target_client_credential_mismatch"):
                    await fresh_target_readback(parsed, replace(p.spec(), bootstrap_client_secret="", product_admin_password="",
                                                               api_client_secret=secrets.token_urlsafe(32)), manager)
                # Replay/restarts use management-only current readback and preserve
                # the same target receipt and exact broker UUID/link.
                member = ExactCohortMember(**receipt["cohort"][0])
                user, links = await manager.cohort_readback(p.spec(), member, token=token)
                assert user["id"] == str(member.broker_subject) and links[0]["userId"] == member.upstream_subject
                assert sorted(receipt["readback"]["management_roles"]) == [
                    "manage-identity-providers", "query-clients", "query-users", "view-clients", "view-realm", "view-users"]
                provider = await api(client, "GET", "akb/identity-provider/instances/platform")
                assert provider["enabled"] is False and provider["config"]["issuer"] == upstream_issuer
            finally:
                await manager.aclose()
            await prove_pg_adoption(pg_source, parsed, replace(p.spec(), bootstrap_client_secret="", product_admin_password=""), (output / "target-preparation.json").read_bytes(), monkeypatch)
            await prove_cleanup(client, p, parsed, output, monkeypatch)


async def prove_partial_allocation(client, p, output):
    member = p.cohort[0].model_copy(update={"broker_username": "interrupted-approved-person",
                                         "upstream_subject": "interrupted-opaque"})
    partial = p.model_copy(update={"cohort": [member]})

    class InterruptedControl(ExistingTargetControl):
        async def _request(self, spec, method, path, **kwargs):
            response = await super()._request(spec, method, path, **kwargs)
            if method == "POST" and path.endswith("/role-mappings/realm"):
                raise RuntimeError("fixture_interrupted_after_link")
            return response

    control = InterruptedControl(partial)
    try:
        with pytest.raises(RuntimeError, match="fixture_interrupted_after_link"):
            await prepare_target(partial, output, control=control)
    finally:
        await control.aclose()
    journal = json.loads((output / "host-authority-journal.json").read_bytes())
    subject = journal["cohort"][0]["broker_subject"]
    actual = (await api(client, "GET", "akb/users?username=interrupted-approved-person&exact=true"))[0]
    assert actual["id"] == subject and journal["phase"] == "preparing"
    assert (await api(client, "GET", f"akb/users/{subject}/federated-identity"))[0]["userId"] == "interrupted-opaque"
    # Fresh process/control uses the durable UUID, not the original null input.
    assert await prepare_target(partial, output, compensate_only=True) == {"compensated": True, "deleted_owned_users": 1}
    assert await api(client, "GET", "akb/users?username=interrupted-approved-person&exact=true") == []
    assert await prepare_target(partial, output, compensate_only=True) == {"compensated": True, "deleted_owned_users": 0}


async def negative_cohorts(client, p):
    control = ExistingTargetControl(p)
    try:
        token = await control.acquire_bootstrap(p.spec())
        member = p.cohort[0]
        with pytest.raises(RuntimeError, match="target_cohort_upstream_issuer_mismatch"):
            await control.cohort_readback(p.spec(), member.model_copy(update={"upstream_issuer": "https://foreign.example"}),
                                         token=token, allow_missing=True)
        await api(client, "POST", "akb/users", {"username": "negative-cohort", "enabled": True,
                                                "requiredActions": ["UPDATE_PASSWORD"]}, 201)
        user = (await api(client, "GET", "akb/users?username=negative-cohort&exact=true"))[0]
        actual = member.model_copy(update={"broker_subject": __import__("uuid").UUID(user["id"]), "broker_username": "negative-cohort"})
        with pytest.raises(RuntimeError, match="target_cohort_user_conflict"):
            await control.cohort_readback(p.spec(), actual, token=token, allow_missing=True)
        await api(client, "PUT", "akb/users/" + user["id"], {"requiredActions": []}, 204)
        await api(client, "PUT", "akb/users/" + user["id"] + "/reset-password",
                  {"type": "password", "value": secrets.token_urlsafe(24), "temporary": False}, 204)
        with pytest.raises(RuntimeError, match="target_cohort_local_credential"):
            await control.cohort_readback(p.spec(), actual, token=token, allow_missing=True)
        credentials = await api(client, "GET", "akb/users/" + user["id"] + "/credentials")
        await api(client, "DELETE", "akb/users/" + user["id"] + "/credentials/" + credentials[0]["id"], status=204)
        await api(client, "POST", "akb/users/" + user["id"] + "/federated-identity/platform",
                  {"identityProvider": "platform", "userId": "foreign-subject", "userName": "negative-cohort"}, 204)
        with pytest.raises(RuntimeError, match="target_cohort_federation_conflict"):
            await control.cohort_readback(p.spec(), actual, token=token, allow_missing=True)
        with pytest.raises(RuntimeError, match="target_cohort_multi_owner"):
            await control.cohort_readback(p.spec(), member.model_copy(update={"upstream_subject": "foreign-subject"}),
                                         token=token, allow_missing=True)
        await api(client, "DELETE", "akb/users/" + user["id"], status=204)
    finally:
        await control.aclose()


async def prove_cleanup(client, p, receipt, output, monkeypatch):
    from app.sso.cleanup_prepared_target import CleanupAuthority, cleanup_target, ids_hash

    cid = "cleanup-once"
    secret = secrets.token_urlsafe(32)
    await api(client, "POST", "master/clients", {"clientId": cid, "secret": secret,
        "enabled": True, "publicClient": False, "serviceAccountsEnabled": True, "protocol": "openid-connect"}, 201)
    bootstrap = (await api(client, "GET", "master/clients?clientId=" + cid))[0]
    sa = await api(client, "GET", "master/clients/" + bootstrap["id"] + "/service-account-user")
    role = await api(client, "GET", "master/roles/admin")
    await api(client, "POST", "master/users/" + sa["id"] + "/role-mappings/realm", [role], 204)
    authority = CleanupAuthority(operation_id=p.operation_id, target_id=p.target_id, realm_id=p.realm_id,
                                 client_id=cid, client_secret=secret)
    # A concurrent credential/action is protected: compensate nothing.
    subject = receipt.cohort[0].broker_subject
    await api(client, "PUT", f"akb/users/{subject}", {"requiredActions": ["CONFIGURE_TOTP"]}, 204)
    with pytest.raises(RuntimeError, match="target_cleanup_owned_user_changed"):
        await cleanup_target(p, receipt, authority, output / "cleanup-compensation.json")
    await api(client, "PUT", f"akb/users/{subject}", {"requiredActions": []}, 204)
    from app.sso import cleanup_prepared_target as cleanup
    first, second = [str(asset["subject"]) for asset in receipt.owned_assets]

    class ChangedDuringPreflight(ExistingTargetControl):
        changed = False

        async def user_fingerprint(self, spec, current, **kwargs):
            if current == second and not self.changed:
                self.changed = True
                await api(client, "PUT", f"akb/users/{first}", {"requiredActions": ["CONFIGURE_TOTP"]}, 204)
            return await super().user_fingerprint(spec, current, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(cleanup, "ExistingTargetControl", ChangedDuringPreflight)
        with pytest.raises(RuntimeError, match="target_cleanup_owned_user_changed"):
            await cleanup_target(p, receipt, authority, output / "cleanup-compensation.json")
    assert (await api(client, "GET", f"akb/users/{first}"))["requiredActions"] == ["CONFIGURE_TOTP"]
    assert (await api(client, "GET", f"akb/users/{second}"))["id"] == second
    assert not (output / "cleanup-compensation.json").exists()
    await api(client, "PUT", f"akb/users/{first}", {"requiredActions": []}, 204)
    result = await cleanup_target(p, receipt, authority, output / "cleanup-compensation.json")
    assert result["remaining_owned_ids"] == [] and len(result["deleted_owned_ids"]) == 2
    assert result["preserved_existing_ids_sha256"] == ids_hash([m.akb_user_id for m in p.cohort])
    assert await api(client, "GET", "master/clients?clientId=" + cid) == []
    assert await cleanup_target(p, receipt, authority, output / "cleanup-compensation.json") == result
    native_admin = (await api(client, "GET", "akb/users?username=existing-realm-admin&exact=true"))[0]
    mappings = await api(client, "GET", "akb/users/" + native_admin["id"] + "/role-mappings")
    assert native_admin["enabled"] is True
    assert "realm-admin" in [role["name"] for role in mappings["clientMappings"]["realm-management"]["mappings"]]


@asynccontextmanager
async def source_database(dsn, monkeypatch):
    from app.db import postgres
    from app.services import recovery_admin_service as recovery
    from app.services import standalone_sso_receipt as source_receipts
    from app.services.standalone_sso_bootstrap import StandaloneSSORetirementReceipt
    from app.config import settings

    database = "akb_prepared_" + uuid4().hex[:12]
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE DATABASE "{database}"')
    pool = await asyncpg.create_pool(dsn.rsplit("/", 1)[0] + "/" + database, min_size=1, max_size=3)
    try:
        async with pool.acquire() as conn:
            await conn.execute((Path(__file__).resolve().parents[1] / "app/db/init.sql").read_text())
            from app.db.postgres import _load_migration

            await _load_migration("015_events_outbox.py").migrate(conn=conn)
            await _load_migration("124_prepared_sso_targets.py").migrate(conn=conn)

        async def get_pool():
            return pool

        class RoleSync:
            async def on_user_create(self, _user_id):
                return None

        monkeypatch.setattr(postgres, "get_pool", get_pool)
        monkeypatch.setattr(recovery, "get_pool", get_pool)
        monkeypatch.setattr(source_receipts, "get_pool", get_pool)
        monkeypatch.setattr(recovery, "get_role_sync", lambda: RoleSync())
        monkeypatch.setattr(settings, "auth_mode", "sso")
        source = await recovery.provision_sso_recovery_admin(username="original-source-recovery", email="source@example.invalid",
            issuer="https://source.example/realms/source", subject=str(uuid4()))
        old = StandaloneSSORetirementReceipt("bundled-keycloak-v6", "https://source.example/realms/source", str(uuid4()),
            "source-bootstrap", str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4()), source["user_id"],
            "http://source:8000/api/v1/auth/keycloak/backchannel-logout")
        await source_receipts.record_standalone_sso_retirement_receipt(old)
        before = await snapshot(pool, source["user_id"])
        yield pool, source, old, before
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


async def prove_pg_adoption(pg_source, receipt, spec, receipt_bytes, monkeypatch):
    from app.config import settings
    from app.services import standalone_sso_receipt as source_receipts
    from app.services import recovery_admin_service as recovery

    pool, source, old, before = pg_source
    assert await snapshot(pool, source["user_id"]) == before
    # The independent target-contract branch adds this Settings field; the
    # bounded base intentionally lacks it until integration.
    monkeypatch.setitem(settings.__dict__, "sso_prepared_target_id", str(receipt.target_id))
    monkeypatch.setattr(settings, "keycloak_server_url", spec.keycloak_public_url)
    monkeypatch.setattr(settings, "keycloak_realm", spec.realm)
    first = await record_target_adoption(receipt, spec=spec, preparation_bytes=receipt_bytes)
    second = await record_target_adoption(receipt, spec=spec, preparation_bytes=receipt_bytes)
    assert first == second and first["user_id"] != source["user_id"]
    assert await snapshot(pool, source["user_id"]) == before
    assert await source_receipts.load_standalone_sso_retirement_receipt() == old
    actual = await load_target_adoption(target_id=receipt.target_id, realm_id=str(receipt.realm_id), issuer=receipt.issuer)
    assert actual["akb_user_id"] == first["user_id"]
    with pytest.raises(RuntimeError):
        await load_target_adoption(target_id=receipt.target_id, realm_id=str(uuid4()), issuer=receipt.issuer)
    assert await load_target_adoption(target_id=uuid4(), realm_id=str(receipt.realm_id), issuer=receipt.issuer) is None
    async with pool.acquire() as conn:
        active = await recovery._locked_recovery_rows(conn)
        assert [str(row["id"]) for row in active] == [first["user_id"]]
        monkeypatch.setitem(settings.__dict__, "sso_prepared_target_id", "")
        default = await recovery._locked_recovery_rows(conn)
        assert [str(row["id"]) for row in default] == [source["user_id"]]


async def snapshot(pool, user_id):
    async with pool.acquire() as conn:
        return [dict(await conn.fetchrow("SELECT * FROM users WHERE id=$1", __import__("uuid").UUID(user_id))),
                [dict(row) for row in await conn.fetch("SELECT * FROM external_identities WHERE user_id=$1", __import__("uuid").UUID(user_id))],
                [dict(row) for row in await conn.fetch("SELECT * FROM standalone_sso_bootstrap_retirements")]]
