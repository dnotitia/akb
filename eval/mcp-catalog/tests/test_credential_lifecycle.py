from __future__ import annotations

from pathlib import Path

import pytest

from mcp_catalog.contracts import load_run_manifest
from mcp_catalog.runner import CredentialResolver, NeedsUserInput
from mcp_catalog.runtime import RuntimeContractError, RuntimeDescriptor
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


class _CredentialFixture:
    def __init__(self) -> None:
        self.mint_calls: list[tuple[str, str, list[str] | None]] = []
        self.revoke_calls: list[tuple[str, str, bool]] = []
        self.reset_calls = 0

    async def reset(self) -> None:
        self.reset_calls += 1

    async def mint_pat(self, username: str, password: str, *, scopes: list[str] | None = None) -> tuple[str, str]:
        self.mint_calls.append((username, password, scopes))
        serial = len(self.mint_calls)
        return f"fresh-token-{serial}", f"token-id-{serial}"

    async def revoke_pat(self, token: str, token_id: str, *, allow_absent: bool = False) -> None:
        self.revoke_calls.append((token, token_id, allow_absent))


class _CellCleanupFixture(_CredentialFixture):
    def __init__(self, label: str, *, failure: str | None = None, absent: bool = False) -> None:
        super().__init__()
        self.label = label
        self.failure = failure
        self.absent = absent

    async def mint_pat(self, username: str, password: str, *, scopes: list[str] | None = None) -> tuple[str, str]:
        self.mint_calls.append((username, password, scopes))
        return f"{self.label}-token", f"{self.label}-id"

    async def revoke_pat(self, token: str, token_id: str, *, allow_absent: bool = False) -> None:
        self.revoke_calls.append((token, token_id, allow_absent))
        if self.absent and allow_absent:
            return
        if self.failure is not None:
            raise RuntimeContractError(self.failure, stage="pat_cleanup")


def _resolver() -> CredentialResolver:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    return CredentialResolver(manifest, descriptor, ())


@pytest.mark.asyncio
async def test_each_reset_refreshes_http_credentials_with_read_only_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKB_E2E_PAT", raising=False)
    monkeypatch.delenv("MCP_BENCH_READ_ONLY_PAT", raising=False)
    monkeypatch.setenv("AKB_E2E_USERNAME", "fixture-user")
    monkeypatch.setenv("AKB_E2E_PASSWORD", "fixture-password")
    resolver = _resolver()
    fixture = _CredentialFixture()

    await resolver.prepare(fixture, ["default", "read_only"])
    initial = {profile: resolver.token_for(profile) for profile in ("default", "read_only")}

    await fixture.reset()
    refreshed_default = await resolver.refresh_after_reset(fixture, "default")
    await fixture.reset()
    refreshed_read_only = await resolver.refresh_after_reset(fixture, "read_only")

    assert refreshed_default != initial["default"]
    assert refreshed_read_only != initial["read_only"]
    assert fixture.reset_calls == 2
    assert fixture.mint_calls == [
        ("fixture-user", "fixture-password", None),
        ("fixture-user", "fixture-password", ["read"]),
        ("fixture-user", "fixture-password", None),
        ("fixture-user", "fixture-password", ["read"]),
    ]


@pytest.mark.asyncio
async def test_cleanup_allows_minted_tokens_removed_by_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKB_E2E_PAT", raising=False)
    monkeypatch.setenv("AKB_E2E_USERNAME", "fixture-user")
    monkeypatch.setenv("AKB_E2E_PASSWORD", "fixture-password")
    resolver = _resolver()
    fixture = _CredentialFixture()

    await resolver.prepare(fixture, ["default"])
    resolver.mark_reset_complete()

    await resolver.cleanup(fixture)
    await resolver.cleanup(fixture)

    assert fixture.revoke_calls == [("fresh-token-1", "token-id-1", True)]


@pytest.mark.asyncio
async def test_external_token_cannot_be_reused_after_reset_without_login_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_BENCH_READ_ONLY_PAT", "external-read-token")
    monkeypatch.delenv("AKB_E2E_USERNAME", raising=False)
    monkeypatch.delenv("AKB_E2E_PASSWORD", raising=False)
    resolver = _resolver()
    fixture = _CredentialFixture()

    await resolver.prepare(fixture, ["read_only"])
    resolver.mark_reset_complete()

    with pytest.raises(NeedsUserInput, match="refresh"):
        await resolver.refresh_after_reset(fixture, "read_only")

    assert resolver.token_for("read_only") == "external-read-token"
    assert fixture.mint_calls == []


@pytest.mark.asyncio
async def test_cleanup_routes_each_token_to_its_cell_and_allows_authorized_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AKB_E2E_PAT", raising=False)
    monkeypatch.setenv("AKB_E2E_USERNAME", "fixture-user")
    monkeypatch.setenv("AKB_E2E_PASSWORD", "fixture-password")
    resolver = _resolver()
    first = _CellCleanupFixture("first", absent=True)
    second = _CellCleanupFixture("second", absent=True)

    await resolver.prepare(first, ["default"])
    await resolver.refresh_after_reset(second, "default")
    resolver.mark_reset_complete()

    await resolver.cleanup(first)

    assert first.revoke_calls == [("first-token", "first-id", True)]
    assert second.revoke_calls == [("second-token", "second-id", True)]


@pytest.mark.asyncio
async def test_cleanup_does_not_swallow_a_real_cell_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AKB_E2E_PAT", raising=False)
    monkeypatch.setenv("AKB_E2E_USERNAME", "fixture-user")
    monkeypatch.setenv("AKB_E2E_PASSWORD", "fixture-password")
    resolver = _resolver()
    fixture = _CellCleanupFixture("failure", failure="transport unavailable")

    await resolver.prepare(fixture, ["default"])
    resolver.mark_reset_complete()

    with pytest.raises(RuntimeContractError, match="transport unavailable"):
        await resolver.cleanup(fixture)

    assert resolver.minted_tokens == [("failure-token", "failure-id")]
