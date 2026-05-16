"""Thin OpenAI-compatible chat client for AKB-internal LLM calls.

Mirrors the shape of `index_service.generate_embeddings`: one async
function, httpx, settings-driven base URL + model + key. Kept tiny on
purpose — when more LLM features land they should reuse this entry
point instead of growing per-feature client code.
"""

from __future__ import annotations

import json
import logging
from typing import Any


from app.config import settings
from app.services import http_pool

logger = logging.getLogger("akb.llm")


class LLMError(RuntimeError):
    """Raised when the LLM endpoint returns no usable result. Callers
    are expected to treat this as a transient failure (back off + retry).
    """


class LLMPermanentError(LLMError):
    """Deterministic failure — same input will produce the same outcome.
    Callers should abandon rather than retry (e.g. response truncated by
    `max_tokens`, content filter refusal). Burning retries on these just
    wastes API calls.
    """


async def chat_json(
    *,
    system: str,
    user: str,
    timeout: float = 30.0,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    disable_reasoning: bool = True,
) -> dict[str, Any]:
    """Call the configured chat endpoint and parse the response as JSON.

    `disable_reasoning=True` (default) sends OpenRouter's
    `reasoning: {enabled: false}` so hybrid CoT models (Qwen3, DeepSeek-R1,
    etc.) skip the thinking phase. Without this, reasoning tokens silently
    consume the `max_tokens` budget and `message.content` comes back null
    with `finish_reason='length'` — which is what bricked metadata backfill
    in production. JSON-extraction tasks don't benefit from CoT anyway.
    """
    if not settings.llm_base_url:
        raise LLMError("llm_base_url not configured")

    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if disable_reasoning:
        payload["reasoning"] = {"enabled": False}
    headers = {}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    client = http_pool.get_client()
    resp = await client.post(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        json=payload, headers=headers,
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()

    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"unexpected LLM response shape: {e}") from e

    finish_reason = choice.get("finish_reason")
    if content is None:
        # `length` is deterministic (same prompt + same max_tokens →
        # same truncation). Refusals via content filters are also
        # deterministic. Don't burn retries on either.
        if finish_reason in ("length", "content_filter"):
            raise LLMPermanentError(
                f"LLM returned null content (finish_reason={finish_reason!r})"
            )
        raise LLMError(
            f"LLM returned null content (finish_reason={finish_reason!r})"
        )

    return _parse_json_loose(content)


def _parse_json_loose(content: str) -> dict[str, Any]:
    """Tolerate code fences and leading/trailing chatter. Raises LLMError
    when there's nothing parseable."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Best-effort: find the first `{` and last `}` and try again.
        i, j = text.find("{"), text.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(text[i : j + 1])
            except json.JSONDecodeError as e:
                raise LLMError(f"LLM did not return JSON: {e}") from e
        raise LLMError("LLM did not return JSON")
