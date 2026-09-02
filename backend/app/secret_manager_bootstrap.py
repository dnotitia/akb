"""Chart-native bootstrap for a bundled Vault-compatible Secret Manager.

The command runs only in the short-lived Helm-managed bootstrap Job.  It keeps
runtime application code independent from Vault/OpenBao while allowing a plain
``helm install`` to finish initialization, first unseal, policy setup, and VSO
projection without Docker or kubectl inside the Pod.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.deployment_secret_material import generate_secret_contract_material


class BootstrapError(RuntimeError):
    """A safe-to-report bootstrap failure that never embeds secret values."""


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise BootstrapError(f"{name} is required")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise BootstrapError(f"{name} must be an integer") from exc
    if value < 1:
        raise BootstrapError(f"{name} must be positive")
    return value


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _unb64(value: str) -> str:
    return base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")


def _load_json_list(path: str) -> list[str]:
    if not path:
        return []
    candidate = Path(path)
    if not candidate.exists():
        return []
    data = json.loads(candidate.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise BootstrapError(f"{path} must contain a JSON string array")
    return [item for item in data if item.strip()]


def _load_text(path: str) -> str:
    if not path:
        return ""
    candidate = Path(path)
    if not candidate.exists():
        return ""
    return candidate.read_text(encoding="utf-8").strip()


def normalize_pgp_public_key(value: str) -> str:
    """Return the base64 OpenPGP packet accepted by the native init API."""

    stripped = value.strip()
    if "-----BEGIN PGP PUBLIC KEY BLOCK-----" not in stripped:
        compact = "".join(stripped.split())
        try:
            base64.b64decode(compact, validate=True)
        except ValueError as exc:
            raise BootstrapError("PGP public keys must be ASCII-armoured or base64") from exc
        return compact

    body: list[str] = []
    in_body = False
    for line in stripped.splitlines():
        line = line.strip()
        if line == "-----BEGIN PGP PUBLIC KEY BLOCK-----":
            continue
        if not in_body:
            if not line:
                in_body = True
            continue
        if line.startswith("=") or line == "-----END PGP PUBLIC KEY BLOCK-----":
            break
        if line:
            body.append(line)
    packet = "".join(body)
    if not packet:
        raise BootstrapError("PGP public key armour contains no packet data")
    return packet


class KubernetesClient:
    def __init__(self, namespace: str) -> None:
        host = _required_env("KUBERNETES_SERVICE_HOST")
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text(encoding="utf-8")
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        self.namespace = namespace
        self.client = httpx.Client(
            base_url=f"https://{host}:{port}",
            verify=ca_path,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )

    def _resource_path(self, plural: str, name: str | None = None) -> str:
        path = f"/api/v1/namespaces/{self.namespace}/{plural}"
        return f"{path}/{name}" if name else path

    def get(self, plural: str, name: str) -> dict[str, Any] | None:
        response = self.client.get(self._resource_path(plural, name))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BootstrapError(f"Kubernetes GET {plural}/{name} failed with HTTP {response.status_code}")
        return response.json()

    def upsert(self, plural: str, name: str, resource: dict[str, Any]) -> None:
        existing = self.get(plural, name)
        if existing is None:
            response = self.client.post(self._resource_path(plural), json=resource)
            expected = 201
        else:
            response = self.client.patch(
                self._resource_path(plural, name),
                json=resource,
                headers={"Content-Type": "application/merge-patch+json"},
            )
            expected = 200
        if response.status_code != expected:
            raise BootstrapError(f"Kubernetes write {plural}/{name} failed with HTTP {response.status_code}")

    def delete(self, plural: str, name: str) -> None:
        response = self.client.delete(self._resource_path(plural, name))
        if response.status_code not in {200, 202, 404}:
            raise BootstrapError(f"Kubernetes DELETE {plural}/{name} failed with HTTP {response.status_code}")

    def decoded_secret(self, name: str) -> dict[str, str] | None:
        secret = self.get("secrets", name)
        if secret is None:
            return None
        return {key: _unb64(value) for key, value in secret.get("data", {}).items()}


class VaultClient:
    def __init__(self, address: str, ca_path: str) -> None:
        self.client = httpx.Client(base_url=address, verify=ca_path, timeout=20.0)

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        body: dict[str, Any] | None = None,
        allowed: set[int] | None = None,
    ) -> httpx.Response:
        headers = {"X-Vault-Token": token} if token else {}
        response = self.client.request(method, f"/v1/{path.lstrip('/')}", headers=headers, json=body)
        if response.status_code not in (allowed or {200, 204}):
            raise BootstrapError(f"Secret Manager {method} {path} failed with HTTP {response.status_code}")
        return response

    def seal_status(self) -> dict[str, Any]:
        return self.request("GET", "sys/seal-status").json()


@dataclass(frozen=True)
class Settings:
    namespace: str
    engine: str
    seal_mode: str
    auth_profile: str
    replicas: int
    key_shares: int
    key_threshold: int
    kv_mount: str
    kv_path: str
    auth_mount: str
    runtime_role: str
    operator_role: str
    operator_service_account: str
    recovery_secret: str
    input_secret: str
    receipt: str
    secret_contract: str
    secret_store_base: str
    ca_path: str
    wait_seconds: int
    pgp_unseal_keys: tuple[str, ...]
    pgp_recovery_keys: tuple[str, ...]
    pgp_root_key: str

    @classmethod
    def from_environment(cls) -> "Settings":
        engine = _required_env("SECRET_ENGINE")
        seal_mode = _required_env("SECRET_SEAL_MODE")
        auth_profile = _required_env("AUTH_PROFILE")
        if engine not in {"openbao", "hashicorp-vault"}:
            raise BootstrapError("SECRET_ENGINE must be openbao or hashicorp-vault")
        if seal_mode not in {"plaintext", "pgp", "auto"}:
            raise BootstrapError("SECRET_SEAL_MODE must be plaintext, pgp, or auto")
        if auth_profile not in {"local", "sso"}:
            raise BootstrapError("AUTH_PROFILE must be local or sso")

        shares = _positive_int("SECRET_KEY_SHARES", 5)
        threshold = _positive_int("SECRET_KEY_THRESHOLD", 3)
        if threshold > shares:
            raise BootstrapError("SECRET_KEY_THRESHOLD cannot exceed SECRET_KEY_SHARES")

        unseal_keys = _load_json_list(os.getenv("PGP_UNSEAL_KEYS_FILE", ""))
        recovery_keys = _load_json_list(os.getenv("PGP_RECOVERY_KEYS_FILE", ""))
        root_key = _load_text(os.getenv("PGP_ROOT_KEY_FILE", ""))
        if seal_mode == "pgp" and (len(unseal_keys) != shares or not root_key):
            raise BootstrapError("PGP mode requires one unseal public key per share and a root-token public key")
        if seal_mode == "auto" and recovery_keys and len(recovery_keys) != shares:
            raise BootstrapError("Auto Seal recovery public-key count must match key shares")

        return cls(
            namespace=_required_env("POD_NAMESPACE"),
            engine=engine,
            seal_mode=seal_mode,
            auth_profile=auth_profile,
            replicas=_positive_int("SECRET_STORE_REPLICAS", 1),
            key_shares=shares,
            key_threshold=threshold,
            kv_mount=os.getenv("KV_MOUNT", "kv"),
            kv_path=os.getenv("KV_PATH", "akb/runtime"),
            auth_mount=os.getenv("KUBERNETES_AUTH_MOUNT", "kubernetes"),
            runtime_role=os.getenv("VAULT_ROLE", "akb-runtime-reader"),
            operator_role=os.getenv("SECRET_OPERATOR_ROLE", "akb-operator-admin"),
            operator_service_account=os.getenv("SECRET_OPERATOR_SERVICE_ACCOUNT", "akb-secret-admin"),
            recovery_secret=os.getenv("RECOVERY_SECRET_NAME", "akb-secret-manager-recovery"),
            input_secret=os.getenv("BOOTSTRAP_INPUT_SECRET_NAME", "akb-secret-manager-bootstrap-input"),
            receipt=os.getenv("BOOTSTRAP_RECEIPT_NAME", "akb-secret-manager-bootstrap"),
            secret_contract=os.getenv("SECRET_CONTRACT_NAME", "akb-secret"),
            secret_store_base=os.getenv(
                "SECRET_STORE_POD_ADDRESS_TEMPLATE",
                "https://akb-secret-store-{index}.akb-secret-store-internal:8200",
            ),
            ca_path=os.getenv("SECRET_STORE_CA_PATH", "/var/run/akb-secret-manager/ca/ca.crt"),
            wait_seconds=_positive_int("BOOTSTRAP_WAIT_SECONDS", 1800),
            pgp_unseal_keys=tuple(normalize_pgp_public_key(item) for item in unseal_keys),
            pgp_recovery_keys=tuple(normalize_pgp_public_key(item) for item in recovery_keys),
            pgp_root_key=normalize_pgp_public_key(root_key) if root_key else "",
        )

    def pod_address(self, index: int) -> str:
        return self.secret_store_base.format(index=index)

    def receipt_contract(self) -> dict[str, str]:
        return {
            "contract": "akb-secret-manager-bootstrap-v2",
            "engine": self.engine,
            "seal-mode": self.seal_mode,
            "auth-profile": self.auth_profile,
            "kv-path": f"{self.kv_mount}/{self.kv_path}",
            "operator-role": self.operator_role,
            "operator-service-account": self.operator_service_account,
        }


def _wait_for_status(client: VaultClient, deadline: float) -> dict[str, Any]:
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return client.seal_status()
        except (BootstrapError, httpx.HTTPError) as exc:
            last_error = exc
            time.sleep(2)
    raise BootstrapError("Timed out waiting for Secret Manager status") from last_error


def _wait_for_initialized(client: VaultClient, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        status = _wait_for_status(client, deadline)
        if status.get("initialized") is True:
            return status
        time.sleep(2)
    raise BootstrapError("Timed out waiting for a Raft member to initialize")


def _wait_for_unsealed(client: VaultClient, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        status = _wait_for_status(client, deadline)
        if status.get("sealed") is False:
            return status
        time.sleep(2)
    raise BootstrapError("Timed out waiting for a Secret Manager member to unseal")


def _wait_for_active(clients: list[VaultClient], deadline: float) -> tuple[VaultClient, dict[str, Any]]:
    """Wait past the post-unseal Raft election and return the active member."""

    last_error: Exception | None = None
    while time.monotonic() < deadline:
        for client in clients:
            try:
                status = client.request("GET", "sys/leader", allowed={200}).json()
            except (BootstrapError, httpx.HTTPError) as exc:
                last_error = exc
                continue
            if status.get("ha_enabled") is False or status.get("is_self") is True:
                return client, status
        time.sleep(2)
    raise BootstrapError("Timed out waiting for an active Secret Manager member") from last_error


def _init_payload(settings: Settings) -> dict[str, Any]:
    if settings.seal_mode in {"plaintext", "pgp"}:
        payload: dict[str, Any] = {
            "secret_shares": settings.key_shares,
            "secret_threshold": settings.key_threshold,
        }
        if settings.seal_mode == "pgp":
            payload["pgp_keys"] = list(settings.pgp_unseal_keys)
            payload["root_token_pgp_key"] = settings.pgp_root_key
        return payload

    payload = {
        "recovery_shares": settings.key_shares,
        "recovery_threshold": settings.key_threshold,
    }
    if settings.pgp_recovery_keys:
        payload["recovery_pgp_keys"] = list(settings.pgp_recovery_keys)
    if settings.pgp_root_key:
        payload["root_token_pgp_key"] = settings.pgp_root_key
    return payload


def _extract_recovery(settings: Settings, init_data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if settings.seal_mode == "auto":
        shares = init_data.get("recovery_keys_base64") or init_data.get("recovery_keys_b64")
        share_type = "recovery_keys_b64"
        encrypted = bool(settings.pgp_recovery_keys)
    else:
        shares = init_data.get("keys_base64") or init_data.get("unseal_keys_b64")
        share_type = "unseal_keys_b64"
        encrypted = settings.seal_mode == "pgp"
    if not isinstance(shares, list) or len(shares) != settings.key_shares:
        raise BootstrapError("Secret Manager initialization returned an invalid share set")
    root_token = init_data.get("root_token", "")
    if not isinstance(root_token, str) or not root_token:
        raise BootstrapError("Secret Manager initialization returned no initial root token")
    recovery = {
        "format": "vault-compatible-native-init-v1",
        "engine": settings.engine,
        "seal_mode": settings.seal_mode,
        "share_type": share_type,
        "shares": shares,
        "shares_encrypted": encrypted,
        "key_shares": settings.key_shares,
        "key_threshold": settings.key_threshold,
        "root_token_encrypted": bool(settings.pgp_root_key),
    }
    if settings.pgp_root_key:
        recovery["encrypted_initial_root_token"] = root_token
    # Native init returns ciphertext in the root_token field when
    # root_token_pgp_key was supplied. It is custody output, not an authority
    # token. Wait for a key holder to provide the decrypted value instead of
    # accidentally presenting ciphertext to the API.
    return recovery, "" if settings.pgp_root_key else root_token


def _write_recovery_secret(
    kube: KubernetesClient,
    settings: Settings,
    recovery: dict[str, Any],
    root_token: str,
) -> None:
    data = {"recovery.json": _b64(json.dumps(recovery, separators=(",", ":")))}
    if not settings.pgp_root_key:
        data["bootstrap-root-token"] = _b64(root_token)
    resource = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": settings.recovery_secret,
            "namespace": settings.namespace,
            "labels": {
                "app.kubernetes.io/name": "akb",
                "app.kubernetes.io/component": "secret-manager-recovery",
            },
            "annotations": {
                "akb.dnotitia.com/recovery-material": "store-off-cluster-then-delete",
                "helm.sh/resource-policy": "keep",
            },
        },
        "type": "Opaque",
        "data": data,
    }
    kube.upsert("secrets", settings.recovery_secret, resource)


def _load_root_token(kube: KubernetesClient, settings: Settings, deadline: float) -> str:
    recovery = kube.decoded_secret(settings.recovery_secret) or {}
    root_token = recovery.get("bootstrap-root-token", "")
    if root_token:
        return root_token

    print(
        "AwaitingKeyHolderBootstrap: decrypt the initial root token and create "
        f"Secret/{settings.input_secret} with key root-token",
        flush=True,
    )
    while time.monotonic() < deadline:
        supplied = kube.decoded_secret(settings.input_secret) or {}
        root_token = supplied.get("root-token", "")
        if root_token:
            return root_token
        time.sleep(3)
    raise BootstrapError("Timed out waiting for the key-holder bootstrap input Secret")


def _unseal_members(
    settings: Settings,
    clients: list[VaultClient],
    shares: list[str],
    deadline: float,
) -> None:
    for index, client in enumerate(clients):
        _wait_for_initialized(client, deadline)
        status = client.seal_status()
        if status.get("sealed") is False:
            continue
        if settings.seal_mode == "auto":
            _wait_for_unsealed(client, deadline)
            continue
        if settings.seal_mode == "pgp":
            print(
                f"AwaitingKeyHolderUnseal: akb-secret-store-{index} requires {settings.key_threshold} decrypted shares",
                flush=True,
            )
            _wait_for_unsealed(client, deadline)
            continue
        for share in shares:
            response = client.request("PUT", "sys/unseal", body={"key": share}, allowed={200}).json()
            if response.get("sealed") is False:
                break
        if client.seal_status().get("sealed") is not False:
            raise BootstrapError(f"Unseal threshold was not reached for member {index}")


def _ensure_mount(client: VaultClient, token: str, mount: str) -> None:
    response = client.request("GET", "sys/mounts", token=token).json()
    mounts = response.get("data", response)
    if f"{mount}/" not in mounts:
        client.request(
            "POST",
            f"sys/mounts/{mount}",
            token=token,
            body={"type": "kv", "options": {"version": "2"}},
            allowed={200, 204},
        )


def _ensure_auth(client: VaultClient, token: str, mount: str) -> None:
    response = client.request("GET", "sys/auth", token=token).json()
    auth = response.get("data", response)
    if f"{mount}/" not in auth:
        client.request(
            "POST",
            f"sys/auth/{mount}",
            token=token,
            body={"type": "kubernetes"},
            allowed={200, 204},
        )


def _configure(client: VaultClient, token: str, settings: Settings) -> None:
    _ensure_mount(client, token, settings.kv_mount)
    _ensure_auth(client, token, settings.auth_mount)

    kubernetes_ca = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt").read_text(encoding="utf-8")
    client.request(
        "POST",
        f"auth/{settings.auth_mount}/config",
        token=token,
        body={
            "kubernetes_host": "https://kubernetes.default.svc:443",
            "kubernetes_ca_cert": kubernetes_ca,
            "disable_iss_validation": True,
        },
    )

    runtime_policy = f'''path "{settings.kv_mount}/data/{settings.kv_path}" {{
  capabilities = ["read"]
}}
path "auth/token/lookup-self" {{ capabilities = ["read"] }}
path "auth/token/renew-self" {{ capabilities = ["update"] }}
path "auth/token/revoke-self" {{ capabilities = ["update"] }}
'''
    client.request(
        "PUT",
        "sys/policies/acl/akb-runtime-reader",
        token=token,
        body={"policy": runtime_policy},
    )
    client.request(
        "POST",
        f"auth/{settings.auth_mount}/role/{settings.runtime_role}",
        token=token,
        body={
            "bound_service_account_names": ["akb-secret-sync"],
            "bound_service_account_namespaces": [settings.namespace],
            "audience": "vault",
            "token_policies": ["akb-runtime-reader"],
            "token_ttl": "10m",
            "token_max_ttl": "1h",
            "token_no_default_policy": True,
        },
    )

    client.request(
        "PUT",
        f"sys/policies/acl/{settings.operator_role}",
        token=token,
        body={"policy": 'path "*" { capabilities = ["create", "read", "update", "patch", "delete", "list", "sudo"] }'},
    )
    client.request(
        "POST",
        f"auth/{settings.auth_mount}/role/{settings.operator_role}",
        token=token,
        body={
            "bound_service_account_names": [settings.operator_service_account],
            "bound_service_account_namespaces": [settings.namespace],
            "audience": "vault",
            "token_policies": [settings.operator_role],
            "token_ttl": "30m",
            "token_max_ttl": "4h",
            "token_no_default_policy": True,
        },
    )

    path = f"{settings.kv_mount}/data/{settings.kv_path}"
    existing = client.request("GET", path, token=token, allowed={200, 404})
    if existing.status_code == 200:
        mode = existing.json().get("data", {}).get("data", {}).get("auth_runtime_mode", "local")
        if mode != settings.auth_profile:
            raise BootstrapError(f"Existing runtime material uses auth profile {mode}; refusing replacement")
        print("Preserving existing AKB Secret Contract v1 material", flush=True)
    else:
        material = generate_secret_contract_material(settings.auth_profile)
        client.request("POST", path, token=token, body={"data": material})
        print("AKB Secret Contract v1 material created", flush=True)


def _write_receipt(
    kube: KubernetesClient,
    settings: Settings,
    *,
    status: str,
    cluster_id: str,
    root_token_revoked: bool,
) -> None:
    data = {
        **settings.receipt_contract(),
        "status": status,
        "cluster-id": cluster_id,
        "root-token-revoked": str(root_token_revoked).lower(),
    }
    resource = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": settings.receipt,
            "namespace": settings.namespace,
            "labels": {
                "app.kubernetes.io/name": "akb",
                "app.kubernetes.io/component": "secret-manager-bootstrap",
            },
            "annotations": {"helm.sh/resource-policy": "keep"},
        },
        "data": data,
    }
    kube.upsert("configmaps", settings.receipt, resource)


def _validate_receipt(settings: Settings, receipt: dict[str, Any]) -> str:
    data = receipt.get("data", {})
    contract = data.get("contract")
    if contract not in {
        "akb-secret-manager-bootstrap-v1",
        "akb-secret-manager-bootstrap-v2",
    }:
        raise BootstrapError("Existing bootstrap receipt has an unsupported contract")
    expected_contract = settings.receipt_contract()
    for key, expected in expected_contract.items():
        if key == "contract":
            continue
        if data.get(key) != expected:
            raise BootstrapError(f"Existing bootstrap receipt conflicts at {key}; refusing reinterpretation")
    if contract == "akb-secret-manager-bootstrap-v1":
        return "complete" if data.get("root-token-revoked") == "true" else "bootstrapped"
    return str(data.get("status", ""))


def _wait_for_contract(kube: KubernetesClient, settings: Settings, deadline: float) -> None:
    required = [settings.secret_contract, "redis-credentials"]
    if settings.auth_profile == "sso":
        required.extend(
            [
                "akb-keycloak-db-credentials",
                "akb-keycloak-bootstrap",
                "akb-product-admin-bootstrap",
            ]
        )
    while time.monotonic() < deadline:
        if all(kube.get("secrets", name) is not None for name in required):
            return
        time.sleep(2)
    raise BootstrapError("Timed out waiting for VSO to project AKB runtime Secrets")


def run() -> None:
    settings = Settings.from_environment()
    kube = KubernetesClient(settings.namespace)
    deadline = time.monotonic() + settings.wait_seconds
    clients = [VaultClient(settings.pod_address(index), settings.ca_path) for index in range(settings.replicas)]
    bootstrap_node = clients[0]

    leader_status = _wait_for_status(bootstrap_node, deadline)
    receipt = kube.get("configmaps", settings.receipt)
    receipt_status = _validate_receipt(settings, receipt) if receipt else ""
    if receipt_status == "complete":
        if leader_status.get("sealed") is True:
            raise BootstrapError(
                "Secret Manager is sealed after completed bootstrap; use stored shares or repair Auto Seal"
            )
        _wait_for_contract(kube, settings, deadline)
        print("Secret Manager bootstrap is already complete", flush=True)
        return

    recovery_secret = kube.decoded_secret(settings.recovery_secret) or {}
    recovery: dict[str, Any]
    root_token: str
    if leader_status.get("initialized") is not True:
        init_data = bootstrap_node.request("PUT", "sys/init", body=_init_payload(settings)).json()
        recovery, root_token = _extract_recovery(settings, init_data)
        _write_recovery_secret(kube, settings, recovery, root_token)
        print(
            f"Recovery material is ready in Secret/{settings.recovery_secret}; "
            "store it off-cluster and delete the Secret after verification",
            flush=True,
        )
    else:
        raw_recovery = recovery_secret.get("recovery.json", "")
        if not raw_recovery:
            raise BootstrapError("Initialized cluster has no bootstrap receipt or recovery checkpoint")
        recovery = json.loads(raw_recovery)
        root_token = recovery_secret.get("bootstrap-root-token", "")

    shares = recovery.get("shares", [])
    if not isinstance(shares, list):
        raise BootstrapError("Recovery checkpoint has an invalid share set")
    _unseal_members(settings, clients, shares, deadline)
    leader, _ = _wait_for_active(clients, deadline)

    if receipt_status != "bootstrapped":
        if not root_token:
            root_token = _load_root_token(kube, settings, deadline)
        _configure(leader, root_token, settings)
        status = leader.seal_status()
        _write_receipt(
            kube,
            settings,
            status="bootstrapped",
            cluster_id=str(status.get("cluster_id", "unknown")),
            root_token_revoked=False,
        )

    if not root_token:
        root_token = (kube.decoded_secret(settings.recovery_secret) or {}).get("bootstrap-root-token", "")
    if not root_token and settings.pgp_root_key:
        root_token = _load_root_token(kube, settings, deadline)
    if root_token:
        leader.request(
            "POST",
            "auth/token/revoke-self",
            token=root_token,
            body={},
            # A crash can occur after revocation but before the final receipt.
            # Only that resumable state may interpret 403 as "already gone";
            # the first execution must observe a successful revocation.
            allowed={200, 204, 403} if receipt_status == "bootstrapped" else {200, 204},
        )
    kube.delete("secrets", settings.input_secret)
    recovery_resource = kube.get("secrets", settings.recovery_secret)
    if recovery_resource is not None:
        kube.upsert(
            "secrets",
            settings.recovery_secret,
            {
                "metadata": {
                    "name": settings.recovery_secret,
                    "namespace": settings.namespace,
                    "annotations": {"akb.dnotitia.com/bootstrap-status": "complete"},
                },
                "data": {"bootstrap-root-token": None},
            },
        )
    status = leader.seal_status()
    _write_receipt(
        kube,
        settings,
        status="complete",
        cluster_id=str(status.get("cluster_id", "unknown")),
        root_token_revoked=True,
    )
    _wait_for_contract(kube, settings, deadline)
    print("Secret Manager init, first unseal, bootstrap, and VSO projection completed", flush=True)


def main() -> int:
    try:
        run()
    except (BootstrapError, httpx.HTTPError, json.JSONDecodeError) as exc:
        print(f"Secret Manager bootstrap failed: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
