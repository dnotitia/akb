"""Vault-skill auto-injection at the MCP dispatch chokepoint.

Covers the shared attribution helper and the paths through `call_tool`
that decide whether a `vault_skill` payload is attached:

- success + attributable vault + a COMPLETED access check on that vault → attached
- handler returned an error dict → not attached
- handler raised (error envelope) → not attached
- no attributable vault          → injector never awaited
- handler never access-checked   → not attached
- handler authorized a DIFFERENT vault than the args name → not attached

The last two are the authorization coupling: attribution (`vault_of_call`)
is an ANALYTICS helper over raw caller args, so it alone can name a vault the
caller has no right to read. The injector follows `access_service`'s
authorized-vault contextvar instead, which only a successful
`check_vault_access` sets — fail-closed on absence and on mismatch.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from app.config import settings
from app.services.tool_usage import vault_of_call

# server.py selects the process-scoped DocumentService at module load, whose
# legacy implementation creates the git storage path via mkdir. These tests
# never write documents; this just keeps the lazy import off `/data/vaults`.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-skill-inject-test-vaults-")

from app.services import access_service  # noqa: E402
from app.services import vault_skill_service  # noqa: E402
from mcp_server import server as server_mod  # noqa: E402

# `Server.call_tool()`'s decorator registers an inner `handler` on the
# request-handler map and returns the ORIGINAL function unchanged, so the
# module symbol is the plain coroutine and is callable directly.
call_tool = server_mod.call_tool

_SENTINEL = {
    "vault": "v1",
    "version": "abc123",
    "reason": "first_touch",
    "body": "# conventions",
    "truncated": False,
}


def test_vault_of_call_public_name():
    assert vault_of_call("akb_get", {"uri": "akb://v1/doc/notes/a.md"}) == "v1"
    assert vault_of_call("akb_search", {"vault": "v2", "query": "x"}) == "v2"
    assert vault_of_call("akb_sql", {"vaults": ["a", "b"]}) is None


@pytest.fixture
def wired(monkeypatch):
    """Stub the chokepoint's collaborators and record injector calls.

    Returns a dict with `calls` (the (session_id, vault) tuples the injector
    was invoked with) and a `dispatch` setter.
    """
    calls: list[tuple] = []

    async def fake_payload(session_id, vault, vault_id=None):
        calls.append((session_id, vault, vault_id))
        return dict(_SENTINEL)

    async def fake_can_read(user, uid, vault):
        return access_service.authorized_vault() == vault

    async def fake_get_user():
        return server_mod._MCPUser()

    monkeypatch.setattr(vault_skill_service, "injection_payload", fake_payload)
    monkeypatch.setattr(server_mod, "_get_user", fake_get_user)
    monkeypatch.setattr(server_mod, "_can_read_vault", fake_can_read)
    monkeypatch.setattr(server_mod, "_session_id", lambda: "sess-1")
    monkeypatch.setattr(server_mod, "_vault_skill_preflight_version", lambda: 1)

    state = {"calls": calls}

    def set_dispatch(fn):
        monkeypatch.setattr(server_mod, "_dispatch", fn)

    state["dispatch"] = set_dispatch
    return state


async def _run(name: str, args: dict) -> dict:
    out = await call_tool(name, args)
    return json.loads(out[0].text)


def _authorizing(vault: str | None, result: dict | None = None):
    """A dispatch stub standing in for a handler that ran an access check.

    `vault=None` is the handler that never checked anything (e.g. akb_help's
    static-topic branch), which must leave the contextvar at its reset value.
    """
    async def handler(name, args, user):
        if vault is not None:
            access_service._authorized_vault.set(vault)
            access_service._authorized_vault_id.set(f"id-{vault}")
        return dict(result or {"ok": 1})
    return handler


async def test_success_path_attaches_payload(wired):
    wired["dispatch"](_authorizing("v1"))

    body = await _run("akb_get", {"uri": "akb://v1/doc/notes/a.md"})

    assert body["ok"] == 1
    assert body["vault_skill"] == _SENTINEL
    assert wired["calls"] == [("sess-1", "v1", "id-v1")]


async def test_no_access_check_means_no_injection(wired):
    """The `akb_help(topic="quickstart", vault=X)` shape.

    Attribution names X, but the handler authorized nothing — so the injector
    is never consulted and no payload (nor a vault-existence signal) escapes.
    """
    wired["dispatch"](_authorizing(None, {"help": "..."}))

    body = await _run("akb_help", {"topic": "quickstart", "vault": "v1"})

    assert "vault_skill" not in body
    assert wired["calls"] == []


async def test_authorized_vault_mismatch_blocks_injection(wired):
    """Fail-closed: the args name v2, the completed check covered v1."""
    wired["dispatch"](_authorizing("v1"))

    body = await _run("akb_get", {"uri": "akb://v2/doc/notes/a.md"})

    assert "vault_skill" not in body
    assert wired["calls"] == []


async def test_stale_authorization_does_not_leak_into_the_next_call(wired):
    """A reused task context must not carry a previous call's authorization."""
    wired["dispatch"](_authorizing("v1"))
    await _run("akb_get", {"uri": "akb://v1/doc/notes/a.md"})

    wired["dispatch"](_authorizing(None, {"help": "..."}))
    body = await _run("akb_help", {"topic": "quickstart", "vault": "v1"})

    assert "vault_skill" not in body
    assert wired["calls"] == [("sess-1", "v1", "id-v1")]  # only the first call's


async def test_raised_exception_gets_envelope_without_injection(wired):
    async def boom(name, args, user):
        raise RuntimeError("handler exploded")

    wired["dispatch"](boom)

    body = await _run("akb_get", {"uri": "akb://v1/doc/notes/a.md"})

    assert body.get("error")
    assert "vault_skill" not in body
    # The error path must not even consult the injector.
    assert wired["calls"] == []


async def test_handler_error_dict_gets_no_injection(wired):
    async def denied(name, args, user):
        return {"error": "x", "code": "FORBIDDEN"}

    wired["dispatch"](denied)

    body = await _run("akb_get", {"uri": "akb://v1/doc/notes/a.md"})

    assert body["code"] == "FORBIDDEN"
    assert "vault_skill" not in body
    assert wired["calls"] == []


async def test_no_attributable_vault_never_awaits_injector(wired):
    # akb_sql access-checks every listed vault, so an authorization IS in
    # place — but multi-vault attribution yields None, and the hot path must
    # skip the lookup entirely rather than fall back to the checked vault.
    wired["dispatch"](_authorizing("b", {"vaults": [], "total": 0}))

    body = await _run("akb_sql", {"vaults": ["a", "b"], "query": "SELECT 1"})

    assert "vault_skill" not in body
    assert wired["calls"] == []


async def test_injector_failure_never_fails_the_call(monkeypatch, wired):
    """The attach is best-effort: a raising injector still returns the result."""
    async def exploding(session_id, vault, vault_id=None):
        raise RuntimeError("cache backend down")

    wired["dispatch"](_authorizing("v1"))
    monkeypatch.setattr(vault_skill_service, "injection_payload", exploding)

    body = await _run("akb_get", {"uri": "akb://v1/doc/notes/a.md"})

    assert body == {"ok": 1}


async def test_first_write_returns_guide_before_dispatch(monkeypatch, wired):
    dispatched = 0

    async def can_read(user, uid, vault):
        access_service._authorized_vault.set(vault)
        access_service._authorized_vault_id.set("immutable-v1")
        return True

    payloads = iter((dict(_SENTINEL), None, None))

    async def payload(session_id, vault, vault_id=None):
        return next(payloads)

    async def dispatch(name, args, user):
        nonlocal dispatched
        dispatched += 1
        return {"updated": True}

    monkeypatch.setattr(server_mod, "_can_read_vault", can_read)
    monkeypatch.setattr(vault_skill_service, "injection_payload", payload)
    wired["dispatch"](dispatch)
    args = {"uri": "akb://v1/doc/notes/a.md", "content": "new"}

    first = await _run("akb_update", args)
    assert first["code"] == "vault_skill_required"
    assert first["vault_skill"] == _SENTINEL
    assert dispatched == 0

    second = await _run("akb_update", args)
    assert second == {"updated": True}
    assert dispatched == 1


async def test_v2_write_requires_explicit_matching_ack_and_strips_it(
    monkeypatch, wired
):
    dispatched = 0
    acknowledgements = []
    token = "opaque-session-vault-challenge"

    async def can_read(user, uid, vault):
        access_service._authorized_vault.set(vault)
        access_service._authorized_vault_id.set("immutable-v1")
        return True

    async def strict_payload(
        session_id, vault, vault_id=None, *, acknowledgement=None
    ):
        acknowledgements.append(acknowledgement)
        if acknowledgement == token:
            return None
        return {**_SENTINEL, "ack_token": token}

    async def no_additive_payload(session_id, vault, vault_id=None):
        return None

    async def dispatch(name, args, user):
        nonlocal dispatched
        dispatched += 1
        assert server_mod.VAULT_SKILL_ACK_ARGUMENT not in args
        return {"updated": True}

    monkeypatch.setattr(server_mod, "_vault_skill_preflight_version", lambda: 2)
    monkeypatch.setattr(server_mod, "_can_read_vault", can_read)
    monkeypatch.setattr(vault_skill_service, "preflight_payload", strict_payload)
    monkeypatch.setattr(
        vault_skill_service, "injection_payload", no_additive_payload
    )
    wired["dispatch"](dispatch)
    args = {"uri": "akb://v1/doc/notes/a.md", "content": "new"}

    first = await _run("akb_update", args)
    second = await _run("akb_update", args)
    assert first["code"] == second["code"] == "vault_skill_required"
    assert first["vault_skill"]["ack_token"] == token
    assert dispatched == 0

    successful = await _run(
        "akb_update", {**args, server_mod.VAULT_SKILL_ACK_ARGUMENT: token}
    )
    assert successful == {"updated": True}
    assert dispatched == 1
    assert acknowledgements == [None, None, token]


async def test_v2_tool_list_advertises_ack_only_on_possible_writes(monkeypatch):
    monkeypatch.setattr(server_mod, "_vault_skill_preflight_version", lambda: 2)
    tools = await server_mod.list_tools()
    by_name = {tool.name: tool for tool in tools}

    assert server_mod.VAULT_SKILL_ACK_ARGUMENT in (
        by_name["akb_update"].input_schema["properties"]
    )
    # akb_grep is normally read-only but becomes a writer when `replace` is
    # present, so its schema must carry the acknowledgement too.
    assert server_mod.VAULT_SKILL_ACK_ARGUMENT in (
        by_name["akb_grep"].input_schema["properties"]
    )
    assert server_mod.VAULT_SKILL_ACK_ARGUMENT not in (
        by_name["akb_get"].input_schema["properties"]
    )


async def test_legacy_client_write_succeeds_and_gets_additive_guide(
    monkeypatch, wired
):
    """Clients that did not negotiate retries keep the pre-feature contract."""
    dispatched = 0

    async def dispatch(name, args, user):
        nonlocal dispatched
        dispatched += 1
        access_service._authorized_vault.set("v1")
        access_service._authorized_vault_id.set("id-v1")
        return {"updated": True}

    monkeypatch.setattr(server_mod, "_vault_skill_preflight_version", lambda: None)
    wired["dispatch"](dispatch)

    body = await _run(
        "akb_update", {"uri": "akb://v1/doc/notes/a.md", "content": "new"}
    )

    assert body == {"updated": True, "vault_skill": _SENTINEL}
    assert dispatched == 1


def test_preflight_capability_is_fail_closed_without_request_context():
    assert server_mod._supports_vault_skill_preflight() is False


async def test_write_only_caller_executes_without_skill_disclosure(monkeypatch, wired):
    async def cannot_read(user, uid, vault):
        return False

    monkeypatch.setattr(server_mod, "_can_read_vault", cannot_read)
    wired["dispatch"](_authorizing("v1", {"updated": True}))
    body = await _run(
        "akb_update", {"uri": "akb://v1/doc/notes/a.md", "content": "new"}
    )
    assert body == {"updated": True}
