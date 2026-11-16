"""Exercise the hook and stdio command together with interleaved prompts."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4


class ClaudeTurnTest(unittest.TestCase):
    def test_exact_call_binding_and_passthrough(self):
        adapter = Path(__file__).resolve().parents[2] / "scripts/integrations/claude_mcp_turn.py"
        thread, first, second = (str(uuid4()) for _ in range(3))
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "XDG_CACHE_HOME": tmp}
            for call_id, prompt in (("A", first), ("B", second), ("collision", first), ("collision", second)):
                hook = {"hook_event_name": "PreToolUse", "tool_use_id": call_id, "session_id": thread, "prompt_id": prompt}
                result = subprocess.run([sys.executable, adapter, "hook"], input=json.dumps(hook),
                                        text=True, capture_output=True, check=True, env=env)
                self.assertEqual(result.stdout, "")
            frames = []
            for call_id in ("B", "A", "missing", "collision", "A"):
                meta = {"claudecode/toolUseId": call_id}
                if len(frames) == 4:
                    meta["traceparent"] = "00-" + uuid4().hex + "-1111111111111111-01"
                frames.append(json.dumps({"method": "tools/call", "params": {"_meta": meta}}))
            frames.extend(['{"method":"tools/list"}', "unrecognized frame"])
            result = subprocess.run(
                [sys.executable, adapter, "--", sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                input="\n".join(frames) + "\n", text=True, capture_output=True, check=True, env=env, timeout=15,
            )
            lines = result.stdout.splitlines()
            for line, prompt in zip(lines[:2], (second, first), strict=True):
                self.assertEqual(json.loads(line)["params"]["_meta"]["com.dnotitia.akb/turn"],
                                 {"thread_id": thread, "turn_id": prompt})
            self.assertEqual(lines[2:], frames[2:])


if __name__ == "__main__":
    unittest.main()
