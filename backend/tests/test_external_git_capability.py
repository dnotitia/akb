"""Tests for the external-git capability startup check.

Three layers:

1. **Version floor** — config validation refuses a min below 2.37;
   ``_effective_min_version`` floors to 2.37 and honours a higher operator value;
   a missing/old git fast-fails.
2. **Functional probes (real git)** — ``--config-env`` wires an env var into a
   config value, and ``http.curloptResolve`` actually routes a ``*.invalid`` host
   to a loopback listener (with a steering negative control proving the pin is
   the cause).
3. **Orchestration** — disabled ⇒ no-op; enabled + capable git ⇒ passes; each
   failure mode fast-fails with a clear ``ExternalGitCapabilityError``.

Zero external packets: every git invocation here carries the ``curloptResolve``
pin, so a pin-honouring build resolves from the injected cache entry and never
queries DNS — the traffic is loopback-only (``GIT_TERMINAL_PROMPT=0``). The
negative control pins the host to a *dead* loopback port (not an unpinned
``.invalid`` lookup), so it too stays on the host.
"""

from __future__ import annotations

import socket
import subprocess
import threading

import pydantic
import pytest

from app.config import EXTERNAL_GIT_MIN_GIT_VERSION_FLOOR, Settings, parse_git_version
import app.services.external_git_capability as cap
from app.services.external_git_capability import (
    ExternalGitCapabilityError,
    _effective_min_version,
    _installed_git_version,
    _probe_config_env,
    _probe_curlopt_resolve,
    check_external_git_capability,
)


# ── version parsing + config floor ───────────────────────────────────
@pytest.mark.parametrize(
    "text, expected",
    [
        ("git version 2.51.0", (2, 51, 0)),
        ("git version 2.39.3 (Apple Git-145)", (2, 39, 3)),
        ("2.37", (2, 37, 0)),
        ("nonsense", None),
    ],
)
def test_parse_git_version(text, expected) -> None:
    assert parse_git_version(text) == expected


def test_config_rejects_min_below_floor() -> None:
    with pytest.raises(pydantic.ValidationError) as ei:
        Settings(external_git_min_git_version="2.30")
    assert "2.37" in str(ei.value)


def test_config_rejects_unparseable_min() -> None:
    with pytest.raises(pydantic.ValidationError):
        Settings(external_git_min_git_version="latest")


def test_config_accepts_higher_min() -> None:
    s = Settings(external_git_min_git_version="2.40")
    assert _effective_min_version(s) == (2, 40, 0)


def test_effective_min_defaults_to_floor() -> None:
    s = Settings()
    assert _effective_min_version(s) == (*EXTERNAL_GIT_MIN_GIT_VERSION_FLOOR, 0)


# ── build-version gate ───────────────────────────────────────────────
def test_installed_git_version_is_recent() -> None:
    # The test host must have git >= 2.37 (the whole suite runs real git).
    assert _installed_git_version() >= (2, 37, 0)


def test_installed_git_version_missing_binary_raises() -> None:
    with pytest.raises(ExternalGitCapabilityError):
        _installed_git_version("/nonexistent/definitely/not/git")


def test_version_gate_fast_fails_below_floor(monkeypatch) -> None:
    monkeypatch.setattr(cap, "_installed_git_version", lambda *a, **k: (2, 36, 0))
    with pytest.raises(ExternalGitCapabilityError) as ei:
        check_external_git_capability(Settings(external_git_enabled=True))
    assert "below the required minimum" in str(ei.value)


# ── functional probes (real git) ─────────────────────────────────────
def test_config_env_probe_passes() -> None:
    _probe_config_env()  # must not raise on a capable git


def test_curlopt_resolve_pin_reaches_loopback_ipv4() -> None:
    assert _probe_curlopt_resolve(socket.AF_INET, "127.0.0.1", "127.0.0.1") is True


def test_curlopt_resolve_pin_to_dead_port_does_not_reach_live_listener() -> None:
    """Steering negative control (zero external packets): the SAME hermetic,
    *pinned* git invocation must reach our live listener ONLY when the
    curloptResolve pin points at it. Here the URL + pin target a DEAD loopback
    port (bound, captured, closed — nothing listens), so git connects to the dead
    port and the live listener is never touched — proving the hit in the positive
    test is caused by the pin steering the connection, not by ambient resolution.

    Unlike an unpinned `.invalid` lookup, this control keeps the pin, so libcurl
    resolves the host from the injected cache entry and issues no DNS query: no
    packet leaves the host on any build."""
    live = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    hit = threading.Event()
    live.bind(("127.0.0.1", 0))
    live.listen(1)  # bound to an ephemeral port we deliberately never target

    def _accept() -> None:
        try:
            live.settimeout(3.0)
            conn, _ = live.accept()
            hit.set()
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=_accept, daemon=True)
    t.start()

    # A dead loopback port: bind to capture a free port, then close it so nothing
    # is listening there. The pin routes the host to 127.0.0.1 on THIS port.
    dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()

    env, home = cap._probe_env()
    url = f"http://{cap._PROBE_HOST}:{dead_port}/probe.git"
    try:
        subprocess.run(
            [
                cap._GIT_BINARY,
                "-c", f"http.curloptResolve={cap._pin_value(cap._PROBE_HOST, dead_port, '127.0.0.1')}",
                "-c", "http.proxy=",
                "-c", "protocol.http.allow=always",
                "ls-remote", "--", url,
            ],
            env=env, capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        pass
    finally:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
        live.close()
    t.join(timeout=4)
    assert hit.is_set() is False


# ── orchestration ────────────────────────────────────────────────────
def test_disabled_is_noop(monkeypatch) -> None:
    # With the feature off, no probe (and no git subprocess) should run.
    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("probe ran while external_git disabled")

    monkeypatch.setattr(cap, "_installed_git_version", _boom)
    monkeypatch.setattr(cap, "_probe_config_env", _boom)
    monkeypatch.setattr(cap, "_probe_curlopt_resolve", _boom)
    check_external_git_capability(Settings(external_git_enabled=False))  # no raise


def test_enabled_capable_git_passes() -> None:
    # End-to-end on the real host git (>= 2.37 with a working curloptResolve).
    check_external_git_capability(Settings(external_git_enabled=True))


def test_config_env_probe_failure_fast_fails(monkeypatch) -> None:
    def _raise(*a, **k):
        raise ExternalGitCapabilityError("no --config-env")

    monkeypatch.setattr(cap, "_probe_config_env", _raise)
    with pytest.raises(ExternalGitCapabilityError):
        check_external_git_capability(Settings(external_git_enabled=True))


def test_curlopt_resolve_failure_fast_fails(monkeypatch) -> None:
    monkeypatch.setattr(cap, "_probe_curlopt_resolve", lambda *a, **k: False)
    with pytest.raises(ExternalGitCapabilityError) as ei:
        check_external_git_capability(Settings(external_git_enabled=True))
    assert "curloptResolve" in str(ei.value)
