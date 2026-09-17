"""Remove one freshly registered hybrid E2E fixture through the safe API.

The fixture password arrives on stdin. Requires the isolated test runtime to
have account_self_service_enabled and its account cleanup worker running.
Never falls back to the retired cascading account endpoint.
"""
from __future__ import annotations

import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def cleanup(origin: str, username: str, password: str) -> None:
    if not re.fullmatch(r"hybrid-[a-z0-9-]+-[0-9]+", username):
        raise ValueError("cleanup requires a generated hybrid test fixture username")
    token = None

    def request(method: str, path: str, body=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = Request(origin.rstrip("/") + "/api/v1" + path, method=method,
                      headers=headers, data=None if body is None else json.dumps(body).encode())
        try:
            with urlopen(req, timeout=30) as response:
                content = response.read()
                return json.loads(content) if content else {}
        except HTTPError as exc:
            raise RuntimeError(f"{method} {path} returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, json.JSONDecodeError):
            raise RuntimeError(f"{method} {path} failed; cleanup result is unconfirmed") from None

    login = request("POST", "/auth/login", {"username": username, "password": password})
    token = login["token"]
    preview = request("GET", "/my/account/lifecycle")
    uid = preview["user_id"]
    if preview["username"] != username or uid != login["user"]["id"]:
        raise RuntimeError("cleanup identity mismatch")
    if preview["deletion"]["supported"] is not True:
        raise RuntimeError("account cleanup unsupported: enable account_self_service_enabled "
                           "and the cleanup worker in the isolated test runtime")
    # All vaults here are owned by the freshly registered fixture account.
    # Ownership blockers are authoritative even after test ownership transfers.
    while True:
        blockers = request("GET", "/my/account/deletion-blockers?limit=100")
        if blockers["user_id"] != uid:
            raise RuntimeError("cleanup blocker identity mismatch")
        if not blockers["owned_vaults"]:
            break
        for vault in blockers["owned_vaults"]:
            path = "/vaults/" + quote(vault["name"], safe="")
            request("POST", path + "/archive")
            request("DELETE", path)
    result = request("POST", "/my/account/deletion", {
        "expected_user_id": uid, "confirm_username": username, "current_password": password,
    })
    if result.get("deleted") is not True or result.get("user_id") != uid:
        raise RuntimeError("account deletion response did not confirm fixture identity")


if __name__ == "__main__":
    try:
        cleanup(sys.argv[1], sys.argv[2], sys.stdin.read().rstrip("\n"))
    except (ValueError, KeyError, IndexError, RuntimeError) as error:
        print(f"Fixture account cleanup failed: {error}", file=sys.stderr)
        sys.exit(1)
