"""Static safety contract for the disposable broker-chain fixture."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.services.standalone_sso_bootstrap import MANAGEMENT_REALM_ROLES


_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "deploy" / "keycloak-dev" / "broker-chain"


def test_broker_chain_uses_pinned_images_and_no_persistent_volume():
    compose = yaml.safe_load((_FIXTURE / "compose.yaml").read_text())
    services = compose["services"]
    assert set(services) == {"broker", "upstream", "postgres"}
    assert services["broker"]["image"] == services["upstream"]["image"]
    assert services["broker"]["image"].startswith(
        "quay.io/keycloak/keycloak@sha256:"
    )
    assert services["postgres"]["image"].startswith("postgres:16-alpine@sha256:")
    assert services["postgres"]["tmpfs"] == ["/var/lib/postgresql/data"]
    assert services["broker"]["ports"] == ["127.0.0.1:19443:8443"]
    assert services["upstream"]["ports"] == ["127.0.0.1:19444:19444"]
    assert services["postgres"]["ports"] == ["127.0.0.1:19445:5432"]
    assert "extra_hosts" not in services["broker"]
    assert services["upstream"]["networks"]["default"]["aliases"] == [
        "upstream.localhost"
    ]
    assert "volumes" not in compose


def test_realms_are_distinct_and_upstream_client_is_code_pkce_only():
    broker = json.loads((_FIXTURE / "broker-realm.json").read_text())
    upstream = json.loads((_FIXTURE / "upstream-realm.json").read_text())
    assert (broker["realm"], upstream["realm"]) == ("akb", "workforce")
    client = next(
        value for value in upstream["clients"] if value["clientId"] == "akb-broker"
    )
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["fullScopeAllowed"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    browser = next(
        value for value in broker["clients"] if value["clientId"] == "fixture-browser"
    )
    probe = next(
        value for value in upstream["clients"] if value["clientId"] == "upstream-probe"
    )
    for public_client in (browser, probe):
        assert public_client["publicClient"] is True
        assert public_client["fullScopeAllowed"] is False
        assert public_client["defaultClientScopes"] == ["basic", "profile", "email"]


def test_fixture_management_authority_matches_the_product_bootstrap_profile():
    broker = json.loads((_FIXTURE / "broker-realm.json").read_text())
    client = next(
        value
        for value in broker["clients"]
        if value["clientId"] == "akb-sso-manager"
    )
    service_account = next(
        value
        for value in broker["users"]
        if value["serviceAccountClientId"] == "akb-sso-manager"
    )
    scoped = broker["clientScopeMappings"]["realm-management"]

    assert client["fullScopeAllowed"] is False
    assert client["defaultClientScopes"] == ["service_account"]
    assert set(service_account["clientRoles"]["realm-management"]) == set(
        MANAGEMENT_REALM_ROLES
    )
    assert scoped == [{
        "client": "akb-sso-manager",
        "roles": list(MANAGEMENT_REALM_ROLES),
    }]


def test_runner_has_unique_scope_guarded_teardown_and_secret_free_receipt():
    runner = (_FIXTURE / "run.sh").read_text()
    exercise = (_FIXTURE / "exercise.py").read_text()
    assert 'project_name="akb-sso-broker-chain-$$"' in runner
    assert "down --volumes --remove-orphans" in runner
    assert 'cert_dir="$(mktemp -d ' in runner
    assert 'if [[ "$cert_dir" ==' in runner
    assert '"client_secret_exposed": False' in exercise
