from uuid import uuid4

import httpx
import pytest

from tests.rest_integration.helpers import register_user

pytestmark = pytest.mark.live_services

SESSIONS = "/api/v1/agent-sessions"
START = {"agent_id": "claude-code", "source": "startup"}


async def test_agent_session_lifecycle_and_context_recall(api: httpx.AsyncClient):
    owner = await register_user(api, label="agent-session")
    first_session = "cc-aaaa"
    session = f"{SESSIONS}/{first_session}"

    response = await api.post(session, headers=owner.headers, json={**START, "goal": "e2e validation", "cwd": "/tmp/x"})
    started = response.raise_for_status().json()
    assert started["collection_uri"]
    vault = started["memory_vault"]
    assert vault == f"agent-memory-{owner.id}"
    assert started["is_new"] is True

    response = await api.get("/api/v1/vaults", headers=owner.headers)
    assert any(item["name"] == vault for item in response.raise_for_status().json()["vaults"])
    response = await api.get(f"/api/v1/vaults/{vault}/members", headers=owner.headers)
    assert len(response.raise_for_status().json()["members"]) == 1

    for source in ("resume", "compact"):
        response = await api.post(session, headers=owner.headers, json={**START, "source": source})
        repeat = response.raise_for_status().json()
        assert repeat["is_new"] is False, source
        assert repeat["collection_uri"] == started["collection_uri"], source

    response = await api.post(
        f"{session}/snapshot",
        headers=owner.headers,
        json={"partial_summary": "halfway through e2e", "progress": {"step": 3, "of": 5}, "cause": "pre_compact"},
    )
    snapshot = response.raise_for_status().json()
    assert snapshot["snapshot_uri"]
    assert snapshot["sequence"] == 1
    response = await api.post(
        f"{session}/snapshot", headers=owner.headers, json={"partial_summary": "more progress", "cause": "manual"}
    )
    assert response.raise_for_status().json()["sequence"] == 2

    for title, collection, content in (
        ("Dark mode", "preferences", "I prefer dark mode for long sessions"),
        ("Korean conversation", "preferences", "Keep code comments in English, conversation in Korean"),
        ("Migration race", "learnings", "Migration needs a UniqueViolation guard"),
    ):
        response = await api.post(
            "/api/v1/documents",
            headers=owner.headers,
            json={"vault": vault, "collection": collection, "title": title, "content": content, "type": "reference"},
        )
        response.raise_for_status()
    response = await api.get(
        f"{session}/context", headers=owner.headers, params={"scopes": "preferences,learnings", "limit": 10}
    )
    context = response.raise_for_status().json()
    assert len(context["preferences"]) >= 2
    assert len(context["learnings"]) >= 1

    cursor_start = {"agent_id": "cursor", "source": "startup", "parent_session_id": first_session}
    response = await api.post(f"{SESSIONS}/cursor-xxxx", headers=owner.headers, json=cursor_start)
    cursor = response.raise_for_status().json()
    assert cursor["collection_uri"]
    assert "parent_recap" not in cursor["injected_context"]

    response = await api.post(
        f"{session}/end",
        headers=owner.headers,
        json={
            "reason": "completed",
            "summary": "e2e suite passed cleanly",
            "outcome": "success",
            "decisions": ["promote vault model"],
            "next_actions": ["ship plugin"],
            "duration_seconds": 42,
        },
    )
    ended = response.raise_for_status().json()
    assert ended["recap_uri"]
    assert ended["ended_at"]
    assert ended["duration_seconds"] == 42

    response = await api.get(
        f"/api/v1/browse/{vault}",
        headers=owner.headers,
        params={"collection": started["collection_path"], "depth": 0},
    )
    assert "recap.md" in response.raise_for_status().text
    response = await api.get(session, headers=owner.headers)
    assert response.raise_for_status().json()["ended"] is True

    response = await api.post(f"{SESSIONS}/cursor-xxxx-v2", headers=owner.headers, json=cursor_start)
    assert "parent_recap" in response.raise_for_status().json()["injected_context"]

    response = await api.get(SESSIONS, headers=owner.headers, params={"limit": 20})
    assert response.raise_for_status().json()["total"] >= 3
    response = await api.get(SESSIONS, headers=owner.headers, params={"agent_id": "cursor"})
    assert response.raise_for_status().json()["total"] >= 2


async def test_agent_session_window_close_and_validation(api: httpx.AsyncClient):
    owner = await register_user(api, label="agent-session-errors")
    session = f"{SESSIONS}/window-close"

    response = await api.post(session, headers=owner.headers, json=START)
    response.raise_for_status()
    response = await api.post(
        f"{session}/end",
        headers=owner.headers,
        json={"reason": "window_close", "summary": "user closed terminal", "outcome": "partial"},
    )
    ended = response.raise_for_status().json()
    assert ended["reason"] == "window_close"
    assert ended["outcome"] == "partial"

    response = await api.post(
        f"{SESSIONS}/bad-src", headers=owner.headers, json={**START, "source": "not-a-valid-source"}
    )
    assert response.status_code == 422, response.text

    session = f"{SESSIONS}/reason-check"
    response = await api.post(session, headers=owner.headers, json=START)
    response.raise_for_status()
    response = await api.post(
        f"{session}/end", headers=owner.headers, json={"reason": "not-a-real-reason", "summary": "x"}
    )
    assert response.status_code == 422, response.text
    response = await api.post(f"{SESSIONS}/unknown/end", headers=owner.headers, json={"reason": "completed"})
    assert response.status_code == 404, response.text
    response = await api.post(f"{SESSIONS}/anonymous", json={"agent_id": "claude-code"})
    assert response.status_code in (401, 403), response.text


@pytest.mark.parametrize(
    "reason", ["clear", "logout", "prompt_input_exit", "bypass_permissions_disabled", "other", "resume"]
)
async def test_agent_session_accepts_claude_code_end_reasons_verbatim(api: httpx.AsyncClient, reason: str):
    owner = await register_user(api, label="agent-session-reason")
    session = f"{SESSIONS}/reason-{reason}"
    response = await api.post(session, headers=owner.headers, json=START)
    response.raise_for_status()
    response = await api.post(
        f"{session}/end",
        headers=owner.headers,
        json={"reason": reason, "outcome": "success", "summary": f"reason {reason}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["reason"] == reason


async def test_agent_memory_vault_handles_a_cjk_username(api: httpx.AsyncClient):
    label = "한글유저"
    owner = await register_user(api, label="agent-session-cjk", username=f"{label}-{uuid4().hex[:12]}")
    response = await api.patch("/api/v1/auth/me", headers=owner.headers, json={"display_name": label})
    response.raise_for_status()
    response = await api.post(f"{SESSIONS}/cjk", headers=owner.headers, json=START)
    assert response.status_code == 200, response.text
    vault = f"agent-memory-{owner.id}"
    assert response.json()["memory_vault"] == vault
    response = await api.get("/api/v1/vaults", headers=owner.headers)
    details = next(item for item in response.raise_for_status().json()["vaults"] if item["name"] == vault)
    assert label in details["description"]


async def test_agent_memory_vault_adopts_legacy_names_only_for_the_owner(api: httpx.AsyncClient):
    suffix = uuid4().hex[:12]
    owner = await register_user(api, label="legacy-owner", username=f"Collidé-{suffix}")
    another = await register_user(api, label="legacy-other", username=f"Collide-{suffix}")
    owner_jwt = {"Authorization": f"Bearer {owner.jwt}"}
    legacy = f"agent-memory-collide-{suffix}"

    response = await api.post("/api/v1/vaults", headers=owner_jwt, params={"name": legacy, "description": "premig-A"})
    assert response.status_code == 200, response.text
    response = await api.post(f"{SESSIONS}/collide-a", headers=owner_jwt, json=START)
    assert response.raise_for_status().json()["memory_vault"] == legacy

    response = await api.post(f"{SESSIONS}/collide-b", headers=another.headers, json=START)
    assert response.raise_for_status().json()["memory_vault"] == f"agent-memory-{another.id}"
