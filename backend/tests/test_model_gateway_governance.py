from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.services import http_pool, index_service, llm_service, rerank_service


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "response"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://gateway.example/v1")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("rejected", request=request, response=response)


class _Client:
    def __init__(self, responses: list[_Response]):
        self.responses = responses
        self.calls: list[dict] = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def _set_hard_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_api_governance_mode", "platform_hard")
    monkeypatch.setattr(
        settings, "platform_gateway_base_url", "https://gateway.example/v1"
    )
    monkeypatch.setattr(settings, "embed_base_url", "https://gateway.example/v1")
    monkeypatch.setattr(settings, "embed_api_key", "gw_test")  # pragma: allowlist secret
    monkeypatch.setattr(settings, "llm_base_url", "https://gateway.example/v1")
    monkeypatch.setattr(settings, "llm_api_key", "gw_test")  # pragma: allowlist secret
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_base_url", "")
    monkeypatch.setattr(settings, "rerank_api_key", "")


def _assert_managed_idempotency_key(call: dict) -> None:
    value = call["headers"].get("Idempotency-Key")
    assert value
    assert str(uuid.UUID(value)) == value


def test_platform_hard_config_rejects_direct_or_uncredentialed_model_routes():
    with pytest.raises(ValidationError, match="embed_base_url"):
        Settings(
            model_api_governance_mode="platform_hard",
            platform_gateway_base_url="https://gateway.example/v1",
            embed_base_url="https://api.openai.com/v1",
            embed_api_key="gw_test",  # pragma: allowlist secret
        )

    with pytest.raises(ValidationError, match="embed_api_key"):
        Settings(
            model_api_governance_mode="platform_hard",
            platform_gateway_base_url="https://gateway.example/v1",
            embed_base_url="https://gateway.example/v1/",
        )

    configured = Settings(
        model_api_governance_mode="platform_hard",
        platform_gateway_base_url="https://gateway.example/v1/",
        embed_base_url="https://gateway.example/v1",
        embed_api_key="gw_test",  # pragma: allowlist secret
        llm_base_url="https://gateway.example/v1",
        llm_api_key="gw_test",  # pragma: allowlist secret
        rerank_enabled=True,
    )
    assert configured.model_api_governance_mode == "platform_hard"

    standalone = Settings(
        model_api_governance_mode="external_metering",
        embed_base_url="https://api.openai.com/v1",
    )
    assert standalone.embed_base_url == "https://api.openai.com/v1"


@pytest.mark.asyncio
async def test_platform_hard_model_calls_send_one_caller_generated_identity(monkeypatch):
    _set_hard_mode(monkeypatch)

    embed_client = _Client([_Response(200, {"data": [{"index": 0, "embedding": [1.0]}]})])
    status, embeddings, _ = await index_service._embed_call(embed_client, ["text"], 5.0)
    assert status == "ok" and embeddings == [[1.0]]
    _assert_managed_idempotency_key(embed_client.calls[0])

    chat_client = _Client([_Response(200, {
        "choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}]
    })])
    monkeypatch.setattr(http_pool, "get_client", lambda: chat_client)
    assert await llm_service.chat_json(system="system", user="user") == {"ok": True}
    _assert_managed_idempotency_key(chat_client.calls[0])

    rerank_client = _Client([_Response(200, {
        "results": [{"index": 0, "relevance_score": 0.9}]
    })])
    monkeypatch.setattr(http_pool, "get_client", lambda: rerank_client)
    assert await rerank_service.rerank("query", ["document"]) == [(0, 0.9)]
    _assert_managed_idempotency_key(rerank_client.calls[0])


@pytest.mark.asyncio
async def test_external_metering_preserves_header_compatibility(monkeypatch):
    monkeypatch.setattr(settings, "model_api_governance_mode", "external_metering")
    client = _Client([_Response(200, {"data": [{"index": 0, "embedding": [1.0]}]})])

    status, _, _ = await index_service._embed_call(client, ["text"], 5.0)

    assert status == "ok"
    assert "Idempotency-Key" not in client.calls[0]["headers"]


@pytest.mark.asyncio
async def test_external_metering_preserves_legacy_4xx_classification(monkeypatch):
    monkeypatch.setattr(settings, "model_api_governance_mode", "external_metering")
    embed_client = _Client([_Response(401)])
    status, _, _ = await index_service._embed_call(embed_client, ["text"], 5.0)
    assert status == "permanent"

    monkeypatch.setattr(settings, "llm_base_url", "https://api.openai.com/v1")
    chat_client = _Client([_Response(403)])
    monkeypatch.setattr(http_pool, "get_client", lambda: chat_client)
    with pytest.raises(llm_service.LLMError) as caught:
        await llm_service.chat_json(system="system", user="user")
    assert not isinstance(caught.value, llm_service.LLMPermanentError)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 402, 403, 404, 409, 422, 428])
async def test_embedding_governance_rejection_does_not_fan_out_batch(
    monkeypatch, status_code
):
    _set_hard_mode(monkeypatch)
    client = _Client([_Response(status_code)])
    monkeypatch.setattr(http_pool, "get_client", lambda: client)

    embeddings = await index_service.generate_embeddings(["one", "two"])

    assert embeddings == [[], []]
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_chat_budget_denial_remains_deferred_instead_of_abandoned(monkeypatch):
    _set_hard_mode(monkeypatch)
    client = _Client([_Response(402)])
    monkeypatch.setattr(http_pool, "get_client", lambda: client)

    with pytest.raises(llm_service.LLMError, match="HTTP 402") as caught:
        await llm_service.chat_json(system="system", user="user")

    assert not isinstance(caught.value, llm_service.LLMPermanentError)
    assert len(client.calls) == 1
