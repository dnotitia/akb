"""The bundled realm's brokered-account password guard, against a modelled realm.

The model keeps the Keycloak 26.7 behaviour the guard depends on, each measured
against the real server: built-in flows refuse new executions, a flow copy
prefixes its sub-flow and config aliases with the new name, new sub-flows and
executions start disabled, config aliases are unique in the realm, and a realm
update leaves the fields a request omits alone. The broker-chain fixture runs
the same installer against a real Keycloak.
"""

from __future__ import annotations

from copy import deepcopy
import itertools
import json
from urllib.parse import unquote

import httpx
import pytest

from app.services.standalone_sso_bootstrap import (
    StandaloneSSOBootstrapError,
    StandaloneSSOBootstrapSpec,
)
from app.services.standalone_sso_keycloak import KeycloakStandaloneSSOControl
from app.sso.brokered_account_guard import (
    BROKERED_ACCOUNT_ROLE,
    identity_provider_mapper,
)


pytestmark = pytest.mark.asyncio

_REALM = "/admin/realms/akb"


def _spec() -> StandaloneSSOBootstrapSpec:
    return StandaloneSSOBootstrapSpec(
        keycloak_internal_url="http://keycloak:8080",
        keycloak_public_url="https://auth.akb.example.com",
        realm="akb",
        akb_public_url="https://akb.example.com",
        bootstrap_client_id="akb-bootstrap-temporary",
        bootstrap_client_secret="",
        management_client_id="akb-sso-manager",
        management_client_secret="",
        api_client_id="akb-web",
        api_client_secret="",
        admin_client_id="akb-admin",
        admin_client_secret="",
        product_admin_username="product-admin",
        product_admin_email="product-admin@example.com",
        product_admin_password="",
    )


class _Realm:
    """Just enough of one Keycloak realm's Admin REST surface."""

    def __init__(self) -> None:
        self._ids = (f"id-{n}" for n in itertools.count())
        self.realm: dict[str, object] = {
            "id": "akb-realm-id",
            "realm": "akb",
            "browserFlow": "browser",
            "directGrantFlow": "direct grant",
            "displayName": "AKB",
        }
        self.flows: dict[str, dict[str, object]] = {}
        self.executions: dict[str, dict[str, object]] = {}
        self.configs: dict[str, dict[str, object]] = {}
        self.roles: dict[str, dict[str, object]] = {}
        self.providers: list[dict[str, object]] = []
        self.mappers: dict[str, list[dict[str, object]]] = {}
        self.links: dict[str, list[str]] = {}
        self.user_roles: dict[str, set[str]] = {}
        self.requests: list[tuple[str, str]] = []
        self._built_in_flows()

    # -- construction -------------------------------------------------------

    def _flow(self, alias: str, *, top_level: bool, built_in: bool = True) -> dict[str, object]:
        flow = {
            "id": next(self._ids),
            "alias": alias,
            "providerId": "basic-flow",
            "topLevel": top_level,
            "builtIn": built_in,
            "executions": [],
        }
        self.flows[alias] = flow
        return flow

    def _execution(
        self,
        parent: dict[str, object],
        *,
        provider: str | None = None,
        subflow: dict[str, object] | None = None,
        requirement: str,
        config: str | None = None,
    ) -> dict[str, object]:
        execution = {
            "id": next(self._ids),
            "providerId": provider,
            "subflow": None if subflow is None else subflow["alias"],
            "requirement": requirement,
            "authenticationConfig": config,
        }
        self.executions[str(execution["id"])] = execution
        executions = parent["executions"]
        assert isinstance(executions, list)
        executions.append(execution["id"])
        return execution

    def _config(self, alias: str, values: dict[str, str]) -> str:
        config_id = next(self._ids)
        self.configs[config_id] = {"id": config_id, "alias": alias, "config": dict(values)}
        return config_id

    def _built_in_flows(self) -> None:
        browser = self._flow("browser", top_level=True)
        self._execution(browser, provider="auth-cookie", requirement="ALTERNATIVE")
        self._execution(browser, provider="auth-spnego", requirement="DISABLED")
        self._execution(browser, provider="identity-provider-redirector", requirement="ALTERNATIVE")
        forms = self._flow("forms", top_level=False)
        self._execution(browser, subflow=forms, requirement="ALTERNATIVE")
        self._execution(
            forms,
            provider="auth-username-password-form",
            requirement="REQUIRED",
            config=self._config(
                "akb-native-password-amr",
                {"default.reference.value": "pwd", "default.reference.maxAge": "300"},
            ),
        )
        two_factor = self._flow("Browser - Conditional 2FA", top_level=False)
        self._execution(forms, subflow=two_factor, requirement="CONDITIONAL")
        self._execution(two_factor, provider="conditional-user-configured", requirement="REQUIRED")
        self._execution(two_factor, provider="auth-otp-form", requirement="ALTERNATIVE")

        direct = self._flow("direct grant", top_level=True)
        self._execution(direct, provider="direct-grant-validate-username", requirement="REQUIRED")
        self._execution(direct, provider="direct-grant-validate-password", requirement="REQUIRED")
        otp = self._flow("Direct Grant - Conditional OTP", top_level=False)
        self._execution(direct, subflow=otp, requirement="CONDITIONAL")
        self._execution(otp, provider="conditional-user-configured", requirement="REQUIRED")
        self._execution(otp, provider="direct-grant-validate-otp", requirement="REQUIRED")

    def add_provider(self, alias: str, *, enabled: bool = True, linked: int = 0) -> list[str]:
        self.providers.append({"alias": alias, "providerId": "oidc", "enabled": enabled})
        users = [f"{alias}-user-{n}" for n in range(linked)]
        self.links[alias] = users
        return users

    # -- reads --------------------------------------------------------------

    def _flattened(self, flow: dict[str, object], level: int = 0) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        executions = flow["executions"]
        assert isinstance(executions, list)
        for index, execution_id in enumerate(executions):
            execution = self.executions[execution_id]
            row: dict[str, object] = {
                "id": execution_id,
                "requirement": execution["requirement"],
                "level": level,
                "index": index,
                "priority": (index + 1) * 10,
            }
            if execution["subflow"] is not None:
                subflow = self.flows[str(execution["subflow"])]
                row.update(
                    {
                        "displayName": subflow["alias"],
                        "authenticationFlow": True,
                        "flowId": subflow["id"],
                    }
                )
                rows.append(row)
                rows.extend(self._flattened(subflow, level + 1))
            else:
                row.update(
                    {
                        "displayName": execution["providerId"],
                        "providerId": execution["providerId"],
                    }
                )
                if execution["authenticationConfig"] is not None:
                    row["authenticationConfig"] = execution["authenticationConfig"]
                rows.append(row)
        return rows

    def bound_flow(self, binding: str) -> list[dict[str, object]]:
        return self._flattened(self.flows[str(self.realm[binding])])

    # -- writes -------------------------------------------------------------

    def _copy(self, source: dict[str, object], new_alias: str, prefix: str, *, top_level: bool) -> dict[str, object]:
        copy = self._flow(new_alias, top_level=top_level, built_in=False)
        executions = source["executions"]
        assert isinstance(executions, list)
        for execution_id in executions:
            execution = self.executions[execution_id]
            subflow = None
            if execution["subflow"] is not None:
                subflow = self._copy(
                    self.flows[str(execution["subflow"])],
                    f"{prefix} {execution['subflow']}",
                    prefix,
                    top_level=False,
                )
            config = None
            if execution["authenticationConfig"] is not None:
                original = self.configs[str(execution["authenticationConfig"])]
                config = self._config(f"{prefix} {original['alias']}", dict(original["config"]))  # type: ignore[arg-type]
            self._execution(
                copy,
                provider=execution["providerId"],  # type: ignore[arg-type]
                subflow=subflow,
                requirement=str(execution["requirement"]),
                config=config,
            )
        return copy

    def _alias_taken(self, alias: str, *, except_id: str | None = None) -> bool:
        return any(item["alias"] == alias and item["id"] != except_id for item in self.configs.values())

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: C901 - one switch per endpoint
        path = unquote(request.url.path)
        method = request.method
        self.requests.append((method, path))
        body = json.loads(request.content) if request.content else None
        if path == _REALM:
            if method == "GET":
                return httpx.Response(200, json=self.realm)
            if method == "PUT":
                assert isinstance(body, dict)
                self.realm.update(body)
                return httpx.Response(204)
        if path == f"{_REALM}/roles" and method == "POST":
            self.roles[body["name"]] = {"id": f"role-{body['name']}", **body}
            return httpx.Response(201)
        if path.startswith(f"{_REALM}/roles/") and method == "GET":
            role = self.roles.get(path.removeprefix(f"{_REALM}/roles/"))
            return httpx.Response(404) if role is None else httpx.Response(200, json=role)
        if path == f"{_REALM}/authentication/flows" and method == "GET":
            return httpx.Response(
                200,
                json=[
                    {key: value for key, value in flow.items() if key != "executions"}
                    for flow in self.flows.values()
                    if flow["topLevel"]
                ],
            )
        flows_prefix = f"{_REALM}/authentication/flows/"
        if path.startswith(flows_prefix):
            alias, _, rest = path.removeprefix(flows_prefix).partition("/")
            flow = self.flows.get(alias)
            if flow is None:
                return httpx.Response(404)
            if rest == "copy" and method == "POST":
                new_alias = body["newName"]
                if new_alias in self.flows:
                    return httpx.Response(409)
                self._copy(flow, new_alias, new_alias, top_level=True)
                return httpx.Response(201)
            if rest == "executions" and method == "GET":
                return httpx.Response(200, json=self._flattened(flow))
            if rest == "executions" and method == "PUT":
                execution = self.executions[body["id"]]
                execution["requirement"] = body["requirement"]
                return httpx.Response(204)
            if rest in {"executions/flow", "executions/execution"} and method == "POST":
                if flow["builtIn"]:
                    return httpx.Response(400, json={"error": "It is illegal to add sub-flow to a built in flow"})
                if rest == "executions/flow":
                    if body["alias"] in self.flows:
                        return httpx.Response(409)
                    subflow = self._flow(body["alias"], top_level=False, built_in=False)
                    self._execution(flow, subflow=subflow, requirement="DISABLED")
                else:
                    self._execution(flow, provider=body["provider"], requirement="DISABLED")
                return httpx.Response(201)
        executions_prefix = f"{_REALM}/authentication/executions/"
        if path.startswith(executions_prefix) and path.endswith("/config") and method == "POST":
            execution = self.executions[path.removeprefix(executions_prefix).removesuffix("/config")]
            if self._alias_taken(body["alias"]):
                return httpx.Response(409)
            execution["authenticationConfig"] = self._config(body["alias"], body["config"])
            return httpx.Response(201)
        configs_prefix = f"{_REALM}/authentication/config/"
        if path.startswith(configs_prefix):
            config_id = path.removeprefix(configs_prefix)
            if method == "GET":
                return httpx.Response(200, json=self.configs[config_id])
            if method == "PUT":
                if self._alias_taken(body["alias"], except_id=config_id):
                    return httpx.Response(409)
                self.configs[config_id] = {"id": config_id, "alias": body["alias"], "config": body["config"]}
                return httpx.Response(204)
        if path == f"{_REALM}/identity-provider/instances" and method == "GET":
            return httpx.Response(200, json=self.providers)
        providers_prefix = f"{_REALM}/identity-provider/instances/"
        if path.startswith(providers_prefix) and "/mappers" in path:
            alias, _, rest = path.removeprefix(providers_prefix).partition("/mappers")
            mappers = self.mappers.setdefault(alias, [])
            if method == "GET":
                return httpx.Response(200, json=deepcopy(mappers))
            if method == "POST":
                mappers.append({**body, "id": next(self._ids)})
                return httpx.Response(201)
            if method == "PUT":
                index = next(i for i, item in enumerate(mappers) if item["id"] == rest.removeprefix("/"))
                mappers[index] = body
                return httpx.Response(204)
        if path == f"{_REALM}/users" and method == "GET":
            params = request.url.params
            linked = self.links.get(params["idpAlias"], [])
            first, size = int(params["first"]), int(params["max"])
            return httpx.Response(200, json=[{"id": user} for user in linked[first : first + size]])
        users_prefix = f"{_REALM}/users/"
        if path.startswith(users_prefix) and path.endswith("/role-mappings/realm") and method == "POST":
            user = path.removeprefix(users_prefix).removesuffix("/role-mappings/realm")
            self.user_roles.setdefault(user, set()).update(item["name"] for item in body)
            return httpx.Response(204)
        raise AssertionError(f"unexpected {method} {path}")


def _control(realm: _Realm) -> tuple[KeycloakStandaloneSSOControl, StandaloneSSOBootstrapSpec]:
    spec = _spec()
    control = KeycloakStandaloneSSOControl()
    control._clients[spec.keycloak_internal_url] = httpx.AsyncClient(  # noqa: SLF001
        base_url=spec.keycloak_internal_url,
        transport=httpx.MockTransport(realm.handler),
    )
    return control, spec


async def _install(realm: _Realm) -> None:
    control, spec = _control(realm)
    try:
        await control.apply_brokered_account_guard(spec, token="one-time-token")
    finally:
        await control.aclose()


async def _readback(realm: _Realm) -> None:
    control, spec = _control(realm)
    try:
        await control.brokered_account_guard_readback(spec, management_token="manager-token")
    finally:
        await control.aclose()


def _row(rows: list[dict[str, object]], display_name: str) -> dict[str, object]:
    matches = [row for row in rows if row.get("displayName") == display_name]
    assert len(matches) == 1, display_name
    return matches[0]


async def test_install_binds_guarded_copies_of_both_password_flows():
    realm = _Realm()

    await _install(realm)

    assert realm.realm["browserFlow"] == "akb browser"
    assert realm.realm["directGrantFlow"] == "akb direct grant"
    # The built-in flows are kept exactly as Keycloak ships them.
    assert realm.flows["browser"]["builtIn"] is True
    assert len(realm.flows["forms"]["executions"]) == 2  # type: ignore[arg-type]
    assert realm.roles[BROKERED_ACCOUNT_ROLE]["name"] == BROKERED_ACCOUNT_ROLE

    browser = realm.bound_flow("browserFlow")
    password = _row(browser, "auth-username-password-form")
    guard = _row(browser, "akb browser brokered-account guard")
    assert guard["level"] == password["level"] == 1
    assert guard["index"] > password["index"]
    assert guard["requirement"] == "CONDITIONAL"
    # The admin client's amr mapper reads the copy now, so the copy carries it.
    assert realm.configs[str(password["authenticationConfig"])] == {
        "id": password["authenticationConfig"],
        "alias": "akb-browser-native-password-amr",
        "config": {"default.reference.value": "pwd", "default.reference.maxAge": "300"},
    }
    condition = _row(browser, "conditional-user-role")
    deny = _row(browser, "deny-access-authenticator")
    assert condition["requirement"] == deny["requirement"] == "REQUIRED"
    assert realm.configs[str(condition["authenticationConfig"])]["config"] == {
        "condUserRole": BROKERED_ACCOUNT_ROLE,
        "negate": "false",
    }

    direct = realm.bound_flow("directGrantFlow")
    direct_password = _row(direct, "direct-grant-validate-password")
    direct_guard = _row(direct, "akb direct grant brokered-account guard")
    assert direct_guard["level"] == direct_password["level"] == 0
    assert direct_guard["index"] > direct_password["index"]
    assert direct_guard["requirement"] == "CONDITIONAL"

    await _readback(realm)


async def test_install_binds_only_after_both_guards_are_armed():
    realm = _Realm()

    await _install(realm)

    writes = [(method, path) for method, path in realm.requests if method != "GET"]
    binding = writes.index(("PUT", _REALM))
    arming = [index for index, (method, path) in enumerate(writes) if path.endswith("/executions") and method == "PUT"]
    assert arming and max(arming) < binding


async def test_install_marks_every_provider_and_the_accounts_it_already_linked():
    realm = _Realm()
    entra = realm.add_provider("entra", linked=230)
    dormant = realm.add_provider("retired-idp", enabled=False, linked=2)

    await _install(realm)

    for alias in ("entra", "retired-idp"):
        assert [{k: v for k, v in item.items() if k != "id"} for item in realm.mappers[alias]] == [
            identity_provider_mapper(alias)
        ]
    # A provider switched off is where an upstream revocation leaves people;
    # its accounts are marked too, so a password they made cannot replace it.
    assert all(realm.user_roles[user] == {BROKERED_ACCOUNT_ROLE} for user in [*entra, *dormant])
    await _readback(realm)


async def test_install_is_convergent_on_a_second_run():
    realm = _Realm()
    realm.add_provider("entra", linked=1)
    await _install(realm)
    realm.requests.clear()

    await _install(realm)

    writes = [(method, path) for method, path in realm.requests if method != "GET"]
    # Marking an already-marked account is idempotent in Keycloak, and the
    # one write a second run repeats.
    assert writes == [("POST", f"{_REALM}/users/entra-user-0/role-mappings/realm")]


@pytest.mark.parametrize("binding", ["browserFlow", "directGrantFlow"])
async def test_install_refuses_a_flow_someone_else_bound_before_writing_anything(binding):
    realm = _Realm()
    realm.realm[binding] = "operator custom flow"

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await _install(realm)

    assert captured.value.code == "keycloak_authentication_flow_binding_unexpected"
    assert [method for method, _path in realm.requests] == ["GET"]


def _unbind(realm: _Realm) -> None:
    realm.realm["browserFlow"] = "browser"


def _unbind_direct_grant(realm: _Realm) -> None:
    realm.realm["directGrantFlow"] = "direct grant"


def _drop_role(realm: _Realm) -> None:
    realm.roles.clear()


def _execution_for(realm: _Realm, binding: str, display_name: str) -> dict[str, object]:
    return realm.executions[str(_row(realm.bound_flow(binding), display_name)["id"])]


def _disable_guard(realm: _Realm) -> None:
    _execution_for(realm, "browserFlow", "akb browser brokered-account guard")["requirement"] = "DISABLED"


def _disable_direct_grant_deny(realm: _Realm) -> None:
    _execution_for(realm, "directGrantFlow", "deny-access-authenticator")["requirement"] = "DISABLED"


def _negate_condition(realm: _Realm) -> None:
    condition = _execution_for(realm, "browserFlow", "conditional-user-role")
    realm.configs[str(condition["authenticationConfig"])]["config"] = {
        "condUserRole": BROKERED_ACCOUNT_ROLE,
        "negate": "true",
    }


def _retarget_condition(realm: _Realm) -> None:
    condition = _execution_for(realm, "directGrantFlow", "conditional-user-role")
    realm.configs[str(condition["authenticationConfig"])]["config"] = {
        "condUserRole": "default-roles-akb",
        "negate": "false",
    }


def _remove_deny(realm: _Realm) -> None:
    guard = realm.flows["akb browser brokered-account guard"]
    executions = guard["executions"]
    assert isinstance(executions, list)
    executions[:] = [item for item in executions if realm.executions[item]["providerId"] != "deny-access-authenticator"]


def _move_guard_before_password(realm: _Realm) -> None:
    forms = realm.flows["akb browser forms"]
    executions = forms["executions"]
    assert isinstance(executions, list)
    guard = next(item for item in executions if realm.executions[item]["subflow"] is not None and "guard" in str(realm.executions[item]["subflow"]))
    executions.remove(guard)
    executions.insert(0, guard)


def _drop_native_amr_from_the_copy(realm: _Realm) -> None:
    _execution_for(realm, "browserFlow", "auth-username-password-form")["authenticationConfig"] = None


@pytest.mark.parametrize(
    "damage",
    [
        _unbind,
        _unbind_direct_grant,
        _drop_role,
        _disable_guard,
        _disable_direct_grant_deny,
        _negate_condition,
        _retarget_condition,
        _remove_deny,
        _move_guard_before_password,
        _drop_native_amr_from_the_copy,
    ],
    ids=lambda damage: damage.__name__.lstrip("_"),
)
async def test_readback_refuses_a_realm_whose_flows_no_longer_refuse(damage):
    realm = _Realm()
    await _install(realm)
    damage(realm)

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await _readback(realm)

    assert captured.value.code == "keycloak_brokered_account_guard_readback_failed"


async def test_readback_refuses_an_enabled_provider_that_does_not_mark_arrivals():
    realm = _Realm()
    await _install(realm)
    realm.add_provider("added-in-the-console")
    realm.add_provider("parked", enabled=False)

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await _readback(realm)
    assert captured.value.code == "keycloak_identity_provider_guard_mapper_missing"

    realm.mappers["added-in-the-console"] = [{**identity_provider_mapper("added-in-the-console"), "id": "m"}]
    # A disabled provider brings nobody, so it does not count against the mark.
    await _readback(realm)


async def test_readback_never_writes():
    realm = _Realm()
    realm.add_provider("entra", linked=1)
    await _install(realm)
    realm.requests.clear()

    await _readback(realm)

    assert {method for method, _path in realm.requests} == {"GET"}
