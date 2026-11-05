
import json
import os
import select
import sys
import time
import uuid


def request(method, params, deadline):
    request_id = uuid.uuid4().hex
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    with os.fdopen(3, "w", closefd=False) as output:
        output.write(json.dumps(message) + "\n")
        output.flush()

    line = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([4], [], [], remaining)[0]:
            raise TimeoutError(f"No matching response for {method}")
        char = os.read(4, 1)
        if not char:
            raise EOFError(f"Proxy closed before responding to {method}")
        if char != b"\n":
            line.extend(char)
            continue
        response = json.loads(line)
        line.clear()
        if response.get("id") == request_id:
            return response


def call(method, params, timeout):
    deadline = time.monotonic() + timeout
    response = request(method, params, deadline)
    if method != "tools/call":
        return response
    content = response.get("result", {}).get("content", [])
    text = next((item.get("text") for item in content if item.get("type") == "text"), None)
    try:
        result = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return response
    if not isinstance(result, dict) or result.get("code") != "vault_skill_required":
        return response

    guide = result["vault_skill"]
    body = guide["body"]
    acknowledgement = guide["ack_token"]
    if not isinstance(body, str) or not body.strip() or guide.get("truncated") is not False:
        raise ValueError("The fixture requires the complete vault write guide")
    if not isinstance(acknowledgement, str) or not acknowledgement:
        raise ValueError("The vault write guide has no acknowledgement")
    print(f"Vault write guide read before retry:\n{body}", file=sys.stderr)
    retry = {**params, "arguments": {**params["arguments"], "_vault_skill_ack": acknowledgement}}
    return request(method, retry, deadline)


if __name__ == "__main__":
    print(json.dumps(call(sys.argv[1], json.loads(sys.argv[2]), float(sys.argv[3]))))
