"""Drive the installed native hosts without a real or shared model account."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from fixture import free_port


TOOL = "akb_list_vaults"
CLAUDE_TOOL = "mcp__akb__" + TOOL


def vault_list(value) -> bool:
    if isinstance(value, str):
        try:
            return vault_list(json.loads(value))
        except ValueError:
            return False
    if isinstance(value, list):
        return any(vault_list(item) for item in value)
    if not isinstance(value, dict):
        return False
    vaults = value.get("vaults")
    if isinstance(vaults, list):
        return value.get("returned") == len(vaults)
    return vault_list(value["text"]) if isinstance(value.get("text"), str) else False


class LocalModel:
    """Request one tool deterministically through Claude Code's Messages client."""

    def __init__(self):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.seen = []
        app = Starlette(routes=[Route("/v1/messages", self.messages, methods=["POST"])])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error", access_log=False))

    @asynccontextmanager
    async def serve(self):
        task = asyncio.create_task(self.server._serve(), name="isolated-claude-test-model")
        try:
            async with asyncio.timeout(6):
                while not self.server.started:
                    if task.done():
                        await task
                        raise RuntimeError("local test model did not start")
                    await asyncio.sleep(0.05)
            yield
        finally:
            self.server.should_exit = True
            await asyncio.wait_for(task, timeout=10)

    async def messages(self, request: Request):
        payload = await request.json()
        results = [
            block for message in payload["messages"] if isinstance(message.get("content"), list)
            for block in message["content"] if block.get("type") == "tool_result"
        ]
        if results:
            if not any(block.get("tool_use_id") == "toolu_akb_fixture" and not block.get("is_error")
                       and vault_list(block.get("content")) for block in results):
                self.seen.append("invalid_tool_result")
                return JSONResponse({"type": "error", "error": {"type": "invalid_request_error", "message": "AKB read failed"}}, 400)
            self.seen.append("vault_result")
            content = {"type": "text", "text": "AKB fixture read complete"}
            stop_reason = "end_turn"
        else:
            if not any(tool.get("name") == CLAUDE_TOOL for tool in payload.get("tools", [])):
                self.seen.append("missing_tool")
                return JSONResponse({"type": "error", "error": {"type": "invalid_request_error", "message": "AKB tool missing"}}, 400)
            self.seen.append("vault_request")
            content = {"type": "tool_use", "id": "toolu_akb_fixture", "name": CLAUDE_TOOL, "input": {}}
            stop_reason = "tool_use"
        message = {
            "id": "msg_akb_fixture", "type": "message", "role": "assistant",
            "model": payload["model"], "content": [content], "stop_reason": stop_reason,
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        if not payload.get("stream"):
            return JSONResponse(message)
        events = [
            ("message_start", {"message": {**message, "content": [], "stop_reason": None}}),
            ("content_block_start", {"index": 0, "content_block": {**content, **({"text": ""} if "text" in content else {})}}),
            ("content_block_delta", {"index": 0, "delta": (
                {"type": "text_delta", "text": content["text"]} if "text" in content
                else {"type": "input_json_delta", "partial_json": "{}"}
            )}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 1}}),
            ("message_stop", {}),
        ]
        body = "".join(f"event: {event}\ndata: {json.dumps({'type': event, **data})}\n\n" for event, data in events)
        return Response(body, media_type="text/event-stream")


def isolated_environment(root: Path, proxy_environment: dict) -> dict:
    root.mkdir(mode=0o700)
    working = root / "workspace"
    working.mkdir(mode=0o700)
    for directory in ("home", "tmp", "config", "cache"):
        (root / directory).mkdir(mode=0o700)
    return {
        "PATH": proxy_environment["PATH"],
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "LANG": "en_US.UTF-8",
    }


async def spawn(binary, arguments, root, environment, *, interactive=False):
    log = root / "stderr.log"
    log.touch(mode=0o600)
    with log.open("ab") as handle:
        return await asyncio.create_subprocess_exec(
            binary, *arguments, cwd=root / "workspace", env=environment,
            stdin=asyncio.subprocess.PIPE if interactive else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=handle, start_new_session=True, limit=2**22,
        )


async def finish(process):
    from e2e_runtime import terminate_process

    if process.stdin is not None:
        process.stdin.close()
    await terminate_process(process, grace_seconds=5)


async def version(binary: str, root: Path, environment: dict) -> str:
    process = await spawn(binary, ["--version"], root, environment)
    try:
        async with asyncio.timeout(10):
            output, _ = await process.communicate()
            assert process.returncode == 0 and output.strip(), "native CLI is available"
            return output.decode().strip()
    finally:
        await finish(process)


async def codex_probe(binary, root, proxy, proxy_environment, model_url):
    environment = isolated_environment(root, proxy_environment)
    codex_home = root / "codex-home"
    codex_home.mkdir(mode=0o700)
    environment["CODEX_HOME"] = str(codex_home)
    config = codex_home / "config.toml"
    config.touch(mode=0o600)
    config.write_text(
        'model = "gpt-5.4"\n'
        'model_provider = "fixture"\n'
        'check_for_update_on_startup = false\n'
        '[model_providers.fixture]\nname = "Local test model"\nwire_api = "responses"\n'
        f'base_url = {json.dumps(model_url + "/v1")}\n'
        '[mcp_servers.akb]\n'
        f'command = {json.dumps(proxy[0])}\nargs = {json.dumps(proxy[1:])}\n'
        '[mcp_servers.akb.env]\n' +
        "".join(f"{key} = {json.dumps(value)}\n" for key, value in proxy_environment.items())
    )
    host_version = await version(binary, root, environment)
    process = await spawn(binary, ["app-server", "--listen", "stdio://"], root, environment, interactive=True)
    next_id = 0

    async def send(message):
        process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
        await process.stdin.drain()

    async def request(method, params):
        nonlocal next_id
        next_id += 1
        await send({"method": method, "id": next_id, "params": params})
        async with asyncio.timeout(35):
            while line := await process.stdout.readline():
                response = json.loads(line)
                if response.get("id") == next_id:
                    assert "result" in response, f"Codex request {method} succeeded"
                    return response["result"]
        raise RuntimeError(f"Codex closed before {method}")

    try:
        await request("initialize", {"clientInfo": {"name": "akb_fixture", "version": "1"}})
        await send({"method": "initialized"})
        thread = await request("thread/start", {"cwd": str(root / "workspace"), "ephemeral": True})
        thread_id = thread["thread"]["id"]
        inventory = await request("mcpServerStatus/list", {"serverName": "akb", "threadId": thread_id})
        server, = inventory["data"]
        assert server["name"] == "akb" and TOOL in server["tools"], "Codex discovered the ordinary MCP catalog"
        result = await request("mcpServer/tool/call", {"threadId": thread_id, "server": "akb", "tool": TOOL, "arguments": {}})
        assert not result.get("isError") and vault_list(result["content"]), "Codex received the real AKB Vault response"
        return {"version": host_version, "catalog_tools": len(server["tools"]), "read": True, "driver": "Codex app-server"}
    finally:
        await finish(process)


async def claude_probe(binary, root, proxy, proxy_environment, model):
    environment = isolated_environment(root, proxy_environment)
    settings = root / "claude-config"
    settings.mkdir(mode=0o700)
    environment.update({
        "CLAUDE_CONFIG_DIR": str(settings),
        "ANTHROPIC_BASE_URL": model.url,
        "ANTHROPIC_API_KEY": "local-test-model-key",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    config = root / "mcp.json"
    config.touch(mode=0o600)
    config.write_text(json.dumps({"mcpServers": {"akb": {
        "type": "stdio", "command": proxy[0], "args": proxy[1:], "env": proxy_environment,
    }}}))
    host_version = await version(binary, root, environment)
    process = await spawn(binary, [
        "--bare", "--print", "--strict-mcp-config", "--mcp-config", str(config),
        "--model", "claude-sonnet-4-6", "--output-format", "json", "--no-session-persistence",
        "--tools", "", "--allowedTools", CLAUDE_TOOL,
        "--", "Call the configured AKB list Vaults tool exactly once.",
    ], root, environment)
    try:
        async with asyncio.timeout(45):
            output, _ = await process.communicate()
            (root / "result.json").touch(mode=0o600)
            (root / "result.json").write_bytes(output)
            assert process.returncode == 0, "Claude Code completed its local model turn"
            result = json.loads(output)
            assert not result.get("is_error") and result.get("result") == "AKB fixture read complete"
            assert model.seen == ["vault_request", "vault_result"], "Claude Code passed real MCP output back to its model"
            return {"version": host_version, "read": True, "driver": "Claude Code print mode with a local test model"}
    finally:
        await finish(process)


async def exercise_hosts(acceptance) -> dict:
    binaries = {name: shutil.which(name) for name in ("codex", "claude")}
    assert all(binaries.values()), "both native CLI executables must be installed"
    proxy = [*acceptance.cli, "--url", acceptance.resource]
    model = LocalModel()
    receipts = {}
    async with model.serve():
        for name, probe, provider in (("codex", codex_probe, model.url), ("claude", claude_probe, model)):
            authority_requests = len(acceptance.issuer_relay.events)
            resource_requests = len(acceptance.resource_relay.events)
            receipts[name] = await probe(binaries[name], acceptance.root / name, proxy, acceptance.environment, provider)
            assert any(event.get("rpc_tool") == TOOL and event.get("status") == 200
                       for event in acceptance.resource_relay.events[resource_requests:]), f"{name} executed a real AKB read"
            assert not acceptance.capture.exists(), f"{name} never opened a browser during startup"
            assert not any(event["kind"] in {"dcr", "authorize", "authorization_code"}
                           for event in acceptance.issuer_relay.events[authority_requests:]), f"{name} reused the saved OAuth session"
    return receipts
