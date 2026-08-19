import pytest

from app.services.tool_usage import vault_of_call


def test_vault_of_call_public_name():
    assert vault_of_call("akb_get", {"uri": "akb://v1/doc/notes/a.md"}) == "v1"
    assert vault_of_call("akb_search", {"vault": "v2", "query": "x"}) == "v2"
    assert vault_of_call("akb_sql", {"vaults": ["a", "b"]}) is None
