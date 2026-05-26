"""OpenRouter chat completions client (OpenAI-compatible)."""
from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any

import httpx


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # OpenRouter app-attribution headers (recommended, not required).
        self.app_headers = {
            "HTTP-Referer": "https://github.com/dnotitia/akb",
            "X-Title": "akb agentic-bench",
        }

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        max_tokens: int = 12000,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Return the full first-choice message dict + usage."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Qwen3-A3B thinking models burn reasoning tokens before
            # producing content; disable for tool-use latency/budget.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.app_headers,
        }
        # Retry on 429 / 5xx with exponential backoff + jitter. v4 hit
        # `HTTP 429: qwen/qwen-2.5-72b-instruct is temporarily rate-limited
        # upstream` (DeepInfra provider) when 8 worker processes called
        # OpenRouter concurrently — that surfaced as TaskGroup wrap and
        # was misdiagnosed as an MCP race.
        max_attempts = 5
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            for attempt in range(max_attempts):
                r = await cli.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
                if r.status_code == 200:
                    break
                if r.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                    delay = (2 ** attempt) + random.uniform(0, 1.5)
                    await asyncio.sleep(delay)
                    continue
                raise LLMError(f"HTTP {r.status_code} (attempt {attempt+1}): {r.text[:500]}")
            data = r.json()
        if not data.get("choices"):
            raise LLMError(f"no choices: {json.dumps(data)[:500]}")
        return {
            "message": data["choices"][0]["message"],
            "finish_reason": data["choices"][0].get("finish_reason"),
            "usage": data.get("usage", {}),
        }
