"""REST grants must reach the same metadata audit producer as MCP grants."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.routes import access
from app.exceptions import ForbiddenError
from app.services import audit_log


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grant", "revoke"])
@pytest.mark.parametrize("denied", [False, True])
async def test_rest_and_mcp_access_audit_match(monkeypatch, operation, denied):
    monkeypatch.setattr(audit_log.settings.audit, "enabled", True)
    records = []
    monkeypatch.setattr(audit_log, "record", lambda **entry: records.append(entry))
    actor = SimpleNamespace(user_id="actor-id", username="operator")
    args = {"vault": "documents", "user": "colleague", "source_key": "team:example", "revision": 7}
    if operation == "grant":
        args["role"] = "reader"
    result = {"effective_role": "reader", "applied": True}
    error = ForbiddenError("private details must not be copied into audit")
    service = AsyncMock(side_effect=error) if denied else AsyncMock(return_value=result)
    monkeypatch.setattr(access, f"{operation}_access", service)
    req_type = access.GrantRequest if operation == "grant" else access.RevokeRequest
    req = req_type(**{key: val for key, val in args.items() if key != "vault"})
    if denied:
        with pytest.raises(ForbiddenError) as caught:
            await getattr(access, operation)("documents", req, actor)
        assert caught.value is error
    else:
        assert await getattr(access, operation)("documents", req, actor) is result
    assert len(records) == 1, "REST operation disappeared from the audit producer"
    rest = records.pop()
    audit_log.record_tool(f"akb_{operation}", args, actor,
                          {"error": True} if denied else result, is_write=True,
                          protocol={"transport": "mcp"})
    mcp = records.pop()
    for field in ("action", "actor", "actor_id", "vault", "target", "outcome"):
        assert rest[field] == mcp[field]
    assert rest["outcome"] == ("error" if denied else "ok")
    assert rest["meta"]["transport"] == "rest"
    assert rest["meta"]["access"] == mcp["meta"]["access"]
    assert rest["meta"]["access"]["user"] == "colleague"
    assert rest["meta"]["access"]["source_key"] == "team:example"
    assert "private details" not in str(rest)
    assert service.call_args.kwargs["source_key"] == "team:example"
    assert service.call_args.kwargs["revision"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grant", "revoke"])
async def test_omitted_source_keeps_grant_and_revoke_meanings(monkeypatch, operation):
    monkeypatch.setattr(audit_log.settings.audit, "enabled", True)
    records = []
    monkeypatch.setattr(audit_log, "record", lambda **entry: records.append(entry))
    service = AsyncMock(return_value={"applied": True, "effective_role": None})
    monkeypatch.setattr(access, f"{operation}_access", service)
    actor = SimpleNamespace(user_id="actor-id", username="operator")
    req = access.GrantRequest(user="u", role="reader") if operation == "grant" else access.RevokeRequest(user="u")
    await getattr(access, operation)("documents", req, actor)
    if operation == "grant":
        assert "source_key" not in service.call_args.kwargs
        assert records[0]["meta"]["access"]["source_key"] == "direct"
    else:
        assert service.call_args.kwargs["source_key"] is None
        assert records[0]["meta"]["access"]["source_key"] is None
