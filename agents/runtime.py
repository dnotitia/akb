"""AKB Agent Runtime — Think & Act loop with MCP tool calling.

A lightweight agent runtime that:
1. Connects to AKB MCP server via HTTP
2. Uses OpenAI-compatible LLM for reasoning
3. Executes tool calls in a loop until the LLM is done

Usage:
    from agents.runtime import AgentRuntime

    agent = AgentRuntime(
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
        llm_api_key="sk-...",
        akb_base_url="http://localhost:8000",
        akb_pat="akb_...",
    )
    result = await agent.run("Summarise my open todos")
"""

from __future__ import annotations

import json
import logging
import ssl
import uuid
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from openai import AsyncOpenAI

logger = logging.getLogger("akb.agent")


@dataclass
class AgentConfig:
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = ""
    akb_base_url: str = "http://localhost:8000"
    akb_pat: str = ""
    max_turns: int = 15
    temperature: float = 0.3
    system_prompt: str = ""


class MCP:
    """Minimal MCP client over Streamable HTTP."""

    def __init__(self, base_url: str, pat: str):
        self.url = f"{base_url.rstrip('/')}/mcp/"
        self.pat = pat
        self.session_id: str | None = None
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE
        self._http: aiohttp.ClientSession | None = None
        self._tools: list[dict] = []
        self._msg_id = 0

    async def connect(self) -> list[dict]:
        """Initialize MCP session and fetch tools."""
        self._http = aiohttp.ClientSession()

        # Initialize
        resp = await self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "akb-agent-runtime", "version": "1.0"},
        })
        self.session_id = resp.get("_session_id")

        # List tools
        result = await self._rpc("tools/list", {})
        self._tools = result.get("tools", [])
        logger.info("MCP connected: %d tools, session=%s", len(self._tools), self.session_id)
        return self._tools

    async def call_tool(self, name: str, arguments: dict) -> Any:
        """Call an MCP tool and return the result."""
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        # Extract text content
        if isinstance(result, dict) and "content" in result:
            texts = [c["text"] for c in result["content"] if c.get("type") == "text"]
            if len(texts) == 1:
                try:
                    return json.loads(texts[0])
                except json.JSONDecodeError:
                    return texts[0]
            return texts
        return result

    async def close(self):
        if self._http:
            await self._http.close()

    async def _rpc(self, method: str, params: dict) -> dict:
        self._msg_id += 1
        headers = {
            "Authorization": f"Bearer {self.pat}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id

        body = {
            "jsonrpc": "2.0",
            "id": self._msg_id,
            "method": method,
            "params": params,
        }

        async with self._http.post(self.url, json=body, headers=headers, ssl=self._ssl) as resp:
            # Capture session ID from response headers
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid

            data = await resp.json()
            if "error" in data:
                raise RuntimeError(f"MCP error: {data['error']}")
            result = data.get("result", {})
            if sid:
                result["_session_id"] = sid
            return result

    def get_openai_tools(self) -> list[dict]:
        """Convert MCP tools to OpenAI function calling format."""
        openai_tools = []
        for tool in self._tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                },
            })
        return openai_tools


class AgentRuntime:
    """Think & Act loop agent with MCP tool calling."""

    def __init__(self, config: AgentConfig | None = None, **kwargs):
        if config:
            self.config = config
        else:
            self.config = AgentConfig(**kwargs)

        self.mcp = MCP(self.config.akb_base_url, self.config.akb_pat)
        self.llm = AsyncOpenAI(
            base_url=self.config.llm_base_url,
            api_key=self.config.llm_api_key,
        )
        self._connected = False

    async def connect(self):
        """Initialize MCP connection."""
        await self.mcp.connect()
        self._connected = True

    async def close(self):
        await self.mcp.close()

    async def run(self, prompt: str, system: str | None = None) -> str:
        """Execute a think & act loop until the LLM produces a final response.

        Returns the final text response from the LLM.
        """
        if not self._connected:
            await self.connect()

        system_msg = system or self.config.system_prompt or self._default_system()
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": prompt},
        ]
        tools = self.mcp.get_openai_tools()

        for turn in range(self.config.max_turns):
            logger.info("Turn %d/%d", turn + 1, self.config.max_turns)

            # THINK — ask LLM
            response = await self.llm.chat.completions.create(
                model=self.config.llm_model,
                messages=messages,
                tools=tools if tools else None,
                temperature=self.config.temperature,
            )

            choice = response.choices[0]
            message = choice.message

            # Add assistant message to history
            messages.append(message.model_dump(exclude_none=True))

            # No tool calls → final response
            if not message.tool_calls:
                logger.info("Final response (turn %d)", turn + 1)
                return message.content or ""

            # ACT — execute tool calls
            for tc in message.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                logger.info("Tool call: %s(%s)", fn_name, json.dumps(fn_args, ensure_ascii=False)[:200])

                try:
                    result = await self.mcp.call_tool(fn_name, fn_args)
                    result_str = json.dumps(result, ensure_ascii=False, default=str)
                except Exception as e:
                    result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
                    logger.error("Tool error: %s — %s", fn_name, e)

                # Truncate very large results
                if len(result_str) > 4000:
                    result_str = result_str[:4000] + "... (truncated)"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })

        return "(max turns reached)"

    def _default_system(self) -> str:
        return (
            "You are an AKB agent — an AI assistant connected to the AKB knowledge base. "
            "You have access to MCP tools for managing documents, searching, creating todos, and more. "
            "Use akb_help() if you need to learn how to use the tools. "
            "Be concise and action-oriented. When you're done, provide a clear summary."
        )


async def main():
    """Quick demo — run a prompt through the agent.

    Required arguments (CLI):
        --llm-base-url   OpenAI-compatible /v1 endpoint
        --llm-model      Model name (e.g. gpt-4o-mini)
        --llm-api-key    LLM API key
        --akb-base-url   AKB backend URL (e.g. http://localhost:8000)
        --akb-pat        AKB Personal Access Token
        prompt           Trailing positional args, joined as the user prompt
    """
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="AKB agent runtime demo")
    parser.add_argument("--llm-base-url", required=True)
    parser.add_argument("--llm-model", required=True)
    parser.add_argument("--llm-api-key", required=True)
    parser.add_argument("--akb-base-url", required=True)
    parser.add_argument("--akb-pat", required=True)
    parser.add_argument("prompt", nargs="+")
    args = parser.parse_args()

    agent = AgentRuntime(
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        llm_api_key=args.llm_api_key,
        akb_base_url=args.akb_base_url,
        akb_pat=args.akb_pat,
    )

    try:
        result = await agent.run(" ".join(args.prompt))
        print("\n" + "=" * 60)
        print("AGENT RESULT:")
        print("=" * 60)
        print(result)
    finally:
        await agent.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
