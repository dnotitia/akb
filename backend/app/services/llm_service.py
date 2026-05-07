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

import httpx

from app.config import settings
from app.services import http_pool

logger = logging.getLogger("akb.llm")


class LLMError(RuntimeError):
    """Raised when the LLM endpoint returns no usable result. Callers
    are expected to treat this as a transient failure (back off + retry).
    """


async def chat_json(
    *,
    system: str,
    user: str,
    timeout: float = 30.0,
    max_tokens: int = 800,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Call the configured chat endpoint and parse the response as JSON.

    Uses `response_format={"type": "json_object"}` when the upstream
    accepts it. If the body comes back as not-quite-JSON (some providers
    wrap with code fences) we strip and retry the parse before failing.
    """
    if not settings.llm_base_url:
        raise LLMError("llm_base_url not configured")

    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
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

    # Some providers (incl. OpenRouter under content filters / refusals)
    # return a choice whose message.content is null — downstream .strip()
    # would raise AttributeError and abort the entire worker batch.
    if content is None:
        raise LLMError(
            f"LLM returned null content (finish_reason={choice.get('finish_reason')!r})"
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
