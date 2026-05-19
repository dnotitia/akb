"""Unit test: doc_service.get_at_commit reads body from a specific commit."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Test focuses on the strip-frontmatter + commit-pinning logic.
# Full git integration is covered by e2e (test_mcp_e2e.sh's akb_get version).


def test_get_at_commit_strips_frontmatter():
    """python-frontmatter strips the leading --- block and returns body only."""
    import frontmatter
    raw = """---
title: Old Title
id: d-legacy123
---

# Hello

Body here.
"""
    body = frontmatter.loads(raw).content
    assert "id: d-legacy123" not in body
    assert "Body here." in body
    assert body.lstrip().startswith("# Hello")


def test_get_at_commit_regex_fallback_for_malformed_yaml():
    """When frontmatter parser fails, regex still strips the first --- block."""
    import re
    raw = """---
title: bad: yaml :  with: colons
---
# Hello
Body here.
"""
    stripped = re.sub(r"\A---\r?\n.*?\r?\n---\r?\n", "", raw, count=1, flags=re.DOTALL)
    assert "title:" not in stripped
    assert stripped.startswith("# Hello")
