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


# ── the "Allowed Client Scopes" policy, both subtypes ──────────────
#
# Keycloak ships this policy twice. It rejects any spec-compliant DCR body
# for the same reason in both: the body carries `scope=openid`, and `openid`
# is the OIDC sentinel rather than an entry in the realm's client-scope
# catalog, so no setting of the policy can permit it. The script used to
# remove only the `anonymous` one.
#
# The `authenticated` one is what an Initial Access Token registration hits
# — an IAT does not bypass registration policies, it switches which subtype
# applies. So Protected DCR, the option the design offers for hostile
# internet exposure, ran into the wall this script exists to remove, while
# the open path worked. Measured on a realm the script had already
# configured: IAT + a scope field → 403 "Policy 'Allowed Client Scopes'
# rejected request … Not permitted to use specified clientScope"; the same
# registration with the scope field omitted → 201.


def _policy(subtype: str, provider: str = "allowed-client-templates") -> dict:
    return {"id": f"id-{provider}-{subtype}", "providerId": provider, "subType": subtype}


def test_both_policy_subtypes_are_selected_for_removal():
    components = [
        _policy("anonymous"),
        _policy("authenticated"),
        _policy("anonymous", provider="trusted-hosts"),
        _policy("anonymous", provider="max-clients"),
    ]
    selected = setup_script.client_scope_policies(components)
    assert [p["subType"] for p in selected] == ["anonymous", "authenticated"]


def test_the_authenticated_policy_alone_is_still_selected():
    """The state the old script left behind, and the one that traps Protected DCR.

    A realm the previous version had "configured" has only the
    authenticated policy left. Selecting nothing here is exactly the
    regression: the open path works and the hardened path does not.
    """
    assert setup_script.client_scope_policies([_policy("authenticated")]) != []


def test_other_registration_policies_are_left_alone():
    # `consent-required`, `trusted-hosts` (URI) and `max-clients` are the
    # meaningful guards and must survive.
    components = [
        _policy("anonymous", provider="trusted-hosts"),
        _policy("anonymous", provider="consent-required"),
        _policy("anonymous", provider="max-clients"),
    ]
    assert setup_script.client_scope_policies(components) == []


def test_a_realm_already_cleaned_selects_nothing():
    assert setup_script.client_scope_policies([]) == []


@pytest.mark.parametrize("payload", [None, "error body", {"error": "nope"}, [None, "x", 3]])
def test_a_non_list_or_ragged_response_selects_nothing_rather_than_raising(payload):
    # `http()` hands back whatever the admin API said; an error body used to
    # reach `.get()` on a string as an AttributeError.
    assert setup_script.client_scope_policies(payload) == []


def test_verify_step_names_the_subtypes_still_present():
    subtypes = setup_script._policy_subtypes([_policy("anonymous"), _policy("authenticated")])
    assert sorted(subtypes) == ["anonymous", "authenticated"]
    assert setup_script._policy_subtypes([]) == []


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
