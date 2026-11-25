from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx


@dataclass(frozen=True)
class Actor:
    username: str
    id: UUID
    pat_id: UUID
    jwt: str = field(repr=False)
    pat: str = field(repr=False)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.pat}"}


async def register_user(
    api: httpx.AsyncClient,
    *,
    label: str,
    username: str | None = None,
    password: str = "test1234",  # pragma: allowlist secret
) -> Actor:
    handle = f"{label[:23].rstrip('._-')}-{uuid4().hex[:8]}"
    username = username or handle
    response = await api.post("/api/v1/auth/register", json={
        "username": username,
        "email": f"{handle}@example.test",
        "password": password,
    })
    assert response.status_code == 200, response.text
    account = response.json()

    response = await api.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["user"]["id"] == account["user_id"]
    jwt = session["token"]
    assert jwt

    response = await api.post(
        "/api/v1/auth/tokens", headers={"Authorization": f"Bearer {jwt}"}, json={"name": label},
    )
    assert response.status_code == 200, response.text
    token = response.json()
    assert token["user_id"] == account["user_id"]
    assert token["token"]
    return Actor(account["username"], UUID(account["user_id"]), UUID(token["token_id"]), jwt, token["token"])
