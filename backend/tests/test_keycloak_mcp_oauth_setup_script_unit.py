"""The realm setup script must be runnable against the way this repo ships Keycloak.

`scripts/keycloak/setup-akb-mcp-oauth.py` authenticated with a password
grant only, while both Kubernetes paths bootstrap Keycloak with an admin
*service account* and no admin user at all. The script therefore worked
against the dev compose fixture it was written on and against neither
shipped manifest, and the only way past it was to create an admin account
the deployment deliberately did not create (akb#635).

These tests pin both halves: the credential resolution itself, and the
manifest facts that make the client-credentials path the required one.
"""

from __future__ import annotations

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
