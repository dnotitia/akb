"""Credential and native registration contracts for the operator script."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "keycloak" / "setup-akb-mcp-oauth.py"


def _load_script():
    """Import the hyphenated, non-package script by path.

    Registered in ``sys.modules`` before execution because ``dataclass``
    resolves its own module to inspect annotations, and a module absent
    from ``sys.modules`` makes that lookup fail.
    """
    spec = importlib.util.spec_from_file_location("akb_kc_setup_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


setup_script = _load_script()


# ── credential resolution ──────────────────────────────────────────


def test_client_credentials_pair_is_a_first_class_path():
    credential = setup_script.resolve_admin_credential(
        {"KC_ADMIN_CLIENT_ID": "akb-bootstrap-temporary", "KC_ADMIN_CLIENT_SECRET": "s"}
    )
    assert credential.kind == "client_credentials"
    assert credential.token_request_body() == {
        "grant_type": "client_credentials",
        "client_id": "akb-bootstrap-temporary",
        "client_secret": "s",
    }


def test_password_pair_still_works_for_the_dev_compose_fixture():
    credential = setup_script.resolve_admin_credential(
        {"KC_ADMIN_USER": "fixture-admin", "KC_ADMIN_PASS": "p"}
    )
    assert credential.kind == "password"
    body = credential.token_request_body()
    assert body["grant_type"] == "password"
    assert body["client_id"] == "admin-cli"
    assert body["username"] == "fixture-admin"


def test_client_credentials_win_when_both_pairs_are_complete_and_say_so():
    credential = setup_script.resolve_admin_credential(
        {
            "KC_ADMIN_CLIENT_ID": "akb-bootstrap-temporary",
            "KC_ADMIN_CLIENT_SECRET": "s",
            "KC_ADMIN_USER": "admin",
            "KC_ADMIN_PASS": "p",
        }
    )
    assert credential.kind == "client_credentials"
    # A stale export in the operator's shell is reported, not silently obeyed.
    assert credential.shadowed == ("KC_ADMIN_USER", "KC_ADMIN_PASS")


def test_no_credential_at_all_names_both_paths():
    with pytest.raises(setup_script.AdminCredentialError) as excinfo:
        setup_script.resolve_admin_credential({})
    message = str(excinfo.value)
    for name in (
        "KC_ADMIN_CLIENT_ID",
        "KC_ADMIN_CLIENT_SECRET",
        "KC_ADMIN_USER",
        "KC_ADMIN_PASS",
    ):
        assert name in message
    # and points at where each kind of credential actually comes from
    assert "KC_BOOTSTRAP_ADMIN_CLIENT_ID" in message
    assert "KC_BOOTSTRAP_ADMIN_USERNAME" in message


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"KC_ADMIN_CLIENT_ID": "x"}, "KC_ADMIN_CLIENT_SECRET"),
        ({"KC_ADMIN_CLIENT_SECRET": "s"}, "KC_ADMIN_CLIENT_ID"),
        ({"KC_ADMIN_USER": "u"}, "KC_ADMIN_PASS"),
        ({"KC_ADMIN_PASS": "p"}, "KC_ADMIN_USER"),
    ],
)
def test_half_a_pair_names_the_missing_half(env, missing):
    with pytest.raises(setup_script.AdminCredentialError) as excinfo:
        setup_script.resolve_admin_credential(env)
    assert str(excinfo.value).startswith(f"{missing} is not set")


def test_exported_but_empty_counts_as_unset():
    # `KC_ADMIN_PASS=` in a wrapper script is absence, not a blank password.
    with pytest.raises(setup_script.AdminCredentialError):
        setup_script.resolve_admin_credential({"KC_ADMIN_USER": "u", "KC_ADMIN_PASS": "   "})


def test_credential_realm_defaults_to_master_and_is_overridable():
    default = setup_script.resolve_admin_credential(
        {"KC_ADMIN_CLIENT_ID": "c", "KC_ADMIN_CLIENT_SECRET": "s"}
    )
    assert default.realm == "master"
    override = setup_script.resolve_admin_credential(
        {"KC_ADMIN_CLIENT_ID": "c", "KC_ADMIN_CLIENT_SECRET": "s", "KC_ADMIN_REALM": "akb"}
    )
    assert override.realm == "akb"


def test_describe_identifies_the_credential_without_leaking_it():
    client = setup_script.resolve_admin_credential(
        {"KC_ADMIN_CLIENT_ID": "akb-bootstrap-temporary", "KC_ADMIN_CLIENT_SECRET": "sup3rsecret"}
    )
    assert "akb-bootstrap-temporary" in client.describe()
    assert "sup3rsecret" not in client.describe()
    password = setup_script.resolve_admin_credential(
        {"KC_ADMIN_USER": "fixture-admin", "KC_ADMIN_PASS": "hunter2"}
    )
    assert "fixture-admin" in password.describe()
    assert "hunter2" not in password.describe()


# ── the manifest facts that make the above required ────────────────


@pytest.mark.parametrize(
    "manifest",
    [
        Path("deploy") / "k8s" / "standalone-sso" / "keycloak.yaml",
        Path("deploy") / "helm" / "akb" / "templates" / "sso.yaml",
    ],
)
def test_shipped_kubernetes_manifests_bootstrap_a_service_account_not_a_user(manifest):
    """Every deployment path in this repo must stay runnable by the script.

    If a manifest ever starts bootstrapping an admin USER, the password
    path is a legitimate choice again and this assertion should be
    revisited deliberately. Until then, a service account is the only
    admin these deployments have — which is why client credentials
    cannot be optional.
    """
    text = (_REPO_ROOT / manifest).read_text(encoding="utf-8")
    assert "KC_BOOTSTRAP_ADMIN_CLIENT_ID" in text
    assert "KC_BOOTSTRAP_ADMIN_USERNAME" not in text
    # …and the credential that manifest creates resolves for the script.
    credential = setup_script.resolve_admin_credential(
        {"KC_ADMIN_CLIENT_ID": "akb-bootstrap-temporary", "KC_ADMIN_CLIENT_SECRET": "s"}
    )
    assert credential.kind == "client_credentials"


def test_dev_compose_fixture_still_bootstraps_a_user():
    """The password path exists for this fixture; keep the reason visible."""
    compose = _REPO_ROOT / "deploy" / "keycloak-dev" / "broker-chain" / "compose.yaml"
    assert "KC_BOOTSTRAP_ADMIN_USERNAME" in compose.read_text(encoding="utf-8")


def test_native_client_has_only_public_code_pkce_and_exact_loopback_callback():
    client = setup_script.native_client("akb-mcp-native", "https://akb.example.com/prefix/mcp")
    assert client["publicClient"] is True
    assert "secret" not in client
    assert client["standardFlowEnabled"] is True
    assert all(client[name] is False for name in ["implicitFlowEnabled", "directAccessGrantsEnabled", "serviceAccountsEnabled", "fullScopeAllowed"])
    assert client["attributes"] == {"pkce.code.challenge.method": "S256", "use.refresh.tokens": "true"}
    assert client["redirectUris"] == ["http://127.0.0.1/oauth/callback"]
    assert client["defaultClientScopes"] == ["profile", "email"]
    assert client["optionalClientScopes"] == ["akb:vault:read", "akb:vault:write"]
    assert client["protocolMappers"][0]["config"]["included.custom.audience"] == "https://akb.example.com/prefix/mcp"


class NativeRealm:
    def __init__(self):
        self.scopes = [{"id": name, "name": name} for name in ["profile", "email"]]
        self.client = None
        self.realm = {}
        self.calls = []
        self.assigned = {"default": [], "optional": []}
        self.mappers = []

    def request(self, method, url, token, body=None):
        path = url.split("/admin/realms/test", 1)[1]
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/client-scopes":
            if method == "POST":
                self.scopes.append({**body, "id": body["name"]})
            result = self.scopes
        elif path.startswith("/client-scopes/"):
            result = []
        elif path.startswith("/clients?"):
            result = [self.client] if self.client else []
        elif path == "/clients":
            self.client = {**body, "id": "native-id"}
            result = None
        elif path == "/clients/native-id":
            if method == "PUT":
                self.client = {**body, "id": "native-id"}
            result = self.client
        elif "/protocol-mappers/models" in path:
            if method == "POST":
                self.mappers.append({**body, "id": "mapper"})
            if method == "DELETE":
                self.mappers.clear()
            result = self.mappers
        elif "-client-scopes" in path:
            kind = "default" if "/default-" in path else "optional"
            if method == "PUT":
                name = path.rsplit("/", 1)[1]
                if name not in [scope["name"] for scope in self.assigned[kind]]:
                    self.assigned[kind].append({"id": name, "name": name})
            result = self.assigned[kind]
        elif path == "":
            if method == "PUT":
                self.realm.update(body)
            result = self.realm
        else:
            raise AssertionError((method, path))
        return (200, copy.deepcopy(result), {})


def test_provisioning_converges_and_rereads_registration_mapper_scopes_rotation():
    realm = NativeRealm()
    for _ in range(2):
        setup_script.provision_native_client("https://kc.example/admin/realms/test", "admin-token",
                                            "akb-mcp-native", "https://akb.example.com/mcp", request=realm.request)
    assert sum(method == "POST" and path == "/clients" for method, path, _ in realm.calls) == 1
    assert realm.realm == {"revokeRefreshToken": True, "refreshTokenMaxReuse": 0}
    assert realm.client["redirectUris"] == [setup_script.NATIVE_REDIRECT_URI]
    assert len(realm.mappers) == 1
    assert all("/components" not in path for _method, path, _body in realm.calls)


def test_refused_admin_write_stops_provisioning():
    def refused(*_args, **_kwargs):
        return 403, {}, {}

    with pytest.raises(RuntimeError, match="failed"):
        setup_script.provision_native_client("https://kc.example/admin/realms/test", "token", "native",
                                            "https://akb.example.com/mcp", request=refused)


@pytest.mark.parametrize("status", [400, 200])
def test_admin_auth_error_never_prints_response_credentials(monkeypatch, capsys, status):
    marker = "private-response-marker"
    payload = {"refresh_token": marker, "id_token": marker, "error_description": marker}
    monkeypatch.setattr(setup_script, "http", lambda *_args, **_kwargs: (status, payload, {}))
    credential = setup_script.resolve_admin_credential({"KC_ADMIN_USER": "admin", "KC_ADMIN_PASS": "input-marker"})
    with pytest.raises(SystemExit) as error:
        setup_script.get_admin_token("https://kc.example", credential)
    captured = capsys.readouterr()
    output = str(error.value) + captured.out + captured.err
    assert marker not in output
    assert "input-marker" not in output
    assert str(status) in output


def test_operator_http_rejects_real_redirect_without_forwarding_credentials():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/credential-receiver")
            self.end_headers()

        def do_GET(self):
            paths.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, _ = setup_script.http("POST", f"http://127.0.0.1:{server.server_port}/token", "bearer-marker",
                                         body={"client_secret": "secret-marker"})
        assert status == 302
        assert paths == ["/token"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_operator_http_has_a_bounded_timeout(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append(timeout)
            raise TimeoutError("sensitive-network-marker")

    monkeypatch.setattr(setup_script.urllib.request, "build_opener", lambda *_args: Opener())
    with pytest.raises(RuntimeError, match="timed out") as error:
        setup_script.http("GET", "https://kc.example", "bearer-marker")
    assert "sensitive-network-marker" not in str(error.value)
    assert calls == [10.0]
