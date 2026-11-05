
import json
from pathlib import Path
import select
import subprocess
import sys

import pytest


HELPER = Path(__file__).with_name("stdio_rpc_e2e.py")
PARAMS = {"name": "akb_put", "arguments": {"vault": "fixture", "title": "Example", "content": "Body"}}


@pytest.fixture
def proxy_call():
    with subprocess.Popen(
        [
            "bash", "-c", 'exec 3>&1 4<&0; exec "$@"', "stdio-rpc",
            sys.executable, str(HELPER), "tools/call", json.dumps(PARAMS), "2",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()


def read_request(process):
    assert select.select([process.stdout], [], [], 3)[0], "No fixture request received"
    return json.loads(process.stdout.readline())


def respond(process, request, result):
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n")
    process.stdin.flush()


def challenge():
    return {"content": [{"type": "text", "text": json.dumps({
        "code": "vault_skill_required",
        "vault_skill": {"body": "# Fixture guide\nUse this disposable vault.", "truncated": False, "ack_token": "exact-ack"},
    })}], "isError": True}


def test_notifications_and_unrelated_responses_do_not_replace_the_call_result(proxy_call):
    request = read_request(proxy_call)
    proxy_call.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}) + "\n")
    proxy_call.stdin.write(json.dumps({"jsonrpc": "2.0", "id": "unrelated", "result": {}}) + "\n")
    expected = {"content": [{"type": "text", "text": "done"}]}
    respond(proxy_call, request, expected)
    stdout, _ = proxy_call.communicate(timeout=3)
    assert proxy_call.returncode == 0
    assert json.loads(stdout)["result"] == expected


def test_complete_guide_allows_only_the_identical_operation_with_its_ack(proxy_call):
    original = read_request(proxy_call)
    respond(proxy_call, original, challenge())
    retry = read_request(proxy_call)
    assert retry["id"] != original["id"]
    assert retry["method"] == original["method"]
    assert retry["params"] == {**PARAMS, "arguments": {**PARAMS["arguments"], "_vault_skill_ack": "exact-ack"}}
    expected = {"content": [{"type": "text", "text": '{"uri":"akb://fixture/doc/example"}'}]}
    respond(proxy_call, retry, expected)
    stdout, stderr = proxy_call.communicate(timeout=3)
    assert proxy_call.returncode == 0
    assert json.loads(stdout)["result"] == expected
    assert "# Fixture guide\nUse this disposable vault." in stderr
    assert "exact-ack" not in stderr


def test_second_challenge_is_returned_without_a_third_submission(proxy_call):
    respond(proxy_call, read_request(proxy_call), challenge())
    retry = read_request(proxy_call)
    respond(proxy_call, retry, challenge())
    stdout, _ = proxy_call.communicate(timeout=3)
    assert proxy_call.returncode == 0
    assert json.loads(stdout)["result"] == challenge()


@pytest.mark.parametrize("result", [
    {"content": [{"type": "text", "text": '{"error":"HTTP 401"}'}], "isError": True},
    {"content": [{"type": "text", "text": '{"error":"backend unreachable"}'}], "isError": True},
])
def test_authentication_and_network_failures_are_never_retried(proxy_call, result):
    respond(proxy_call, read_request(proxy_call), result)
    stdout, _ = proxy_call.communicate(timeout=3)
    assert proxy_call.returncode == 0
    assert json.loads(stdout)["result"] == result


def test_partial_guide_is_not_acknowledged(proxy_call):
    result = challenge()
    body = json.loads(result["content"][0]["text"])
    body["vault_skill"]["truncated"] = True
    result["content"][0]["text"] = json.dumps(body)
    respond(proxy_call, read_request(proxy_call), result)
    stdout, stderr = proxy_call.communicate(timeout=3)
    assert proxy_call.returncode != 0
    assert stdout == ""
    assert "complete vault write guide" in stderr
