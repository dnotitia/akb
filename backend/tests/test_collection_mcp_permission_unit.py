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

    monkeypatch.setattr(server, "check_vault_access", check_access)
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
