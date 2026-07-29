"""External-git kill-switch: metadata_worker must be inert when
external-git is disabled.

The lifecycle START gate (see test_lifecycle_workers) already prevents the worker
from starting when `external_git_enabled` is off. These cover the
defense-in-depth early-return inside `_process_once` itself, so a live config
flip (or any stray start) still does zero work — no row claim, no `cat_blob`
mirror read, no LLM outbound. All of the worker's rows are source='external_git',
so there is never legitimate work to do when the feature is disabled.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from app.services import metadata_worker


def test_process_once_returns_zero_when_external_git_disabled(monkeypatch):
    """Disabled → return 0 BEFORE touching the DB. `get_pool` is replaced with a
    landmine so any claim attempt (which would lead to cat_blob + the LLM call)
    is proven not to happen."""

    def _boom():
        raise AssertionError(
            "metadata_worker._process_once must not touch the DB when "
            "external_git is disabled"
        )

    monkeypatch.setattr(metadata_worker, "get_pool", _boom)
    monkeypatch.setattr(
        "app.config.settings", types.SimpleNamespace(external_git_enabled=False)
    )

    assert asyncio.run(metadata_worker._process_once()) == 0


def test_process_once_proceeds_when_external_git_enabled(monkeypatch):
    """The early-return is keyed strictly on the flag (no over-blocking): when
    enabled the worker proceeds past the guard into its normal claim path, proven
    here by reaching `get_pool`."""

    class _Reached(Exception):
        pass

    def _reached():
        raise _Reached

    monkeypatch.setattr(metadata_worker, "get_pool", _reached)
    monkeypatch.setattr(
        "app.config.settings", types.SimpleNamespace(external_git_enabled=True)
    )

    with pytest.raises(_Reached):
        asyncio.run(metadata_worker._process_once())
