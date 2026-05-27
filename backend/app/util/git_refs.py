"""Shared validators for git references surfaced through public APIs.

Both the REST layer (`api/routes/documents.py`) and the MCP layer
(`mcp_server/server.py`) accept a `version` parameter for fetching
historical content. Anything other than a literal commit hash is
rejected — symbolic refs like `HEAD~1`, `refs/...`, or branch names
would let a caller bypass the document's lifecycle (e.g. read a
deleted revision via the branch tip) and break audit trails.
"""

from __future__ import annotations

import re

HEX_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
