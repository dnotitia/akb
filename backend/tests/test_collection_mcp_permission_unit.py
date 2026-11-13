"""MCP collection deletion forwards the same table-drop capability as REST."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected"),
    [("writer", False), ("admin", True), ("owner", True)],
)
async def test_delete_collection_derives_table_capability_from_authorized_role(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    expected: bool,
) -> None:
    from app.services import collection_service
    from mcp_server import server

    captured: dict[str, Any] = {}

    async def check_access(*_args: Any, **kwargs: Any) -> dict[str, str]:
        assert kwargs["required_role"] == "writer"
        return {"role": role}

    class FakeCollectionService:
        async def delete(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "ok": True,
                "collection": "data",
                "deleted_docs": 0,
                "deleted_files": 0,
                "deleted_sub_collections": 0,
                "deleted_tables": 0,
            }

    async def can_read(_user: Any, _uid: str, _vault: str) -> bool:
        return True

    monkeypatch.setattr(server, "check_vault_access", check_access)
    monkeypatch.setattr(server, "_can_read_vault", can_read)
    monkeypatch.setattr(
        collection_service,
        "CollectionService",
        FakeCollectionService,
    )

    result = await server._handle_delete_collection(
        {"vault": "reef", "path": "data", "recursive": True},
        "user-id",
        object(),  # type: ignore[arg-type]
    )

    assert result["ok"] is True
    assert captured["allow_table_delete"] is expected
    assert captured["disclose_paths"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("can_read", [True, False])
async def test_delete_collection_discloses_paths_only_to_a_reader(
    monkeypatch: pytest.MonkeyPatch, can_read: bool,
) -> None:
    """Paths of what a delete did or left go only to a caller who may read
    the Vault; a write-only caller gets counts."""
    from app.services import collection_service
    from mcp_server import server

    captured: dict[str, Any] = {}

    async def check_access(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"role": "writer"}

    async def read_authority(_user: Any, _uid: str, _vault: str) -> bool:
        return can_read

    class FakeCollectionService:
        async def delete(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"ok": True, "collection": "data", "deleted_docs": 0, "deleted_files": 0,
                    "deleted_sub_collections": 0, "deleted_tables": 0}

    monkeypatch.setattr(server, "check_vault_access", check_access)
    monkeypatch.setattr(server, "_can_read_vault", read_authority)
    monkeypatch.setattr(collection_service, "CollectionService", FakeCollectionService)

    await server._handle_delete_collection(
        {"vault": "reef", "path": "data", "recursive": True}, "user-id", object(),  # type: ignore[arg-type]
    )
    assert captured["disclose_paths"] is can_read
