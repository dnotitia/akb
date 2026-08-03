"""External-git capability startup check.

When the mirror feature is enabled, the hermetic runner's network-isolation
defense rests on a
handful of git/libcurl behaviours that a build can silently lack: the
``http.curloptResolve`` DNS-pin, an empty proxy actually disabling proxying, and
``--config-env`` wiring the auth header in from the environment. A stock
``git --version`` proves none of these — a git compiled against a libcurl that
ignores ``CURLOPT_RESOLVE`` would parse ``2.40`` and STILL leak the pin. So at
startup (before any worker or request), when ``external_git_enabled`` is true, we:

1. **Build-version gate** — parse the real ``git --version`` and fast-fail below
   the code-owned floor (2.37, where ``http.curloptResolve`` is documented). The
   floor is ``max(2.37, settings.external_git_min_git_version)``: an operator may
   demand newer, never older.
2. **Functional probe** — run the actual git binary, hermetically, against a
   loopback listener reached through a guaranteed-nonexistent ``*.invalid`` host
   pinned with ``http.curloptResolve``. If the pin is honoured (and no proxy
   intercepts), git connects to OUR loopback socket; if libcurl ignored the pin
   the ``.invalid`` host does not resolve and the socket is never touched. We also
   verify ``--config-env`` wires an env var into a config value.

   Network isolation: the probe never *connects* to any real host — it is
   fail-closed. On the pass path (a pin-honouring build) libcurl takes the
   address straight from the injected ``curloptResolve`` cache entry and issues
   NO DNS lookup at all; the traffic is loopback-only. The one path that can
   touch the OS resolver is a pin-IGNORING build — precisely the misconfiguration
   this probe exists to detect and reject — and even then the host is an RFC 6761
   ``.invalid`` special-use label, which a compliant resolver answers locally
   (NXDOMAIN) without forwarding, and the connection still fails closed
   (``GIT_TERMINAL_PROMPT=0``, no listener reached). The ``.invalid`` host is
   load-bearing: it is what lets a hit *prove* the pin is honoured (a
   locally-resolving name would connect to the listener even on a pin-ignoring
   build, turning the check into a false pass), so it cannot be swapped for a
   loopback name.

Any failure raises :class:`ExternalGitCapabilityError` (a ``RuntimeError``) so the
boot aborts with a clear message, exactly like ``_validate_required_settings``.
When ``external_git_enabled`` is false the whole check is skipped (no-op).

This module only READS the runner's contract; it never imports or mutates the
runner. It resolves the same ``git`` binary the runner uses (``shutil.which`` on
the launch PATH) and mirrors the runner's hermetic env for the probe so it tests
what the runner will actually run.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading

from app.config import EXTERNAL_GIT_MIN_GIT_VERSION_FLOOR, parse_git_version

logger = logging.getLogger("akb.external_git_capability")

# Resolve OUR trusted git once from the launch PATH and invoke it by absolute
# path, so the probe tests the very binary the runner will exec (the runner does
# the same). Falls back to the bare name only if `which` finds nothing.
_GIT_BINARY = shutil.which("git") or "git"
_GIT_BIN_DIR = os.path.dirname(_GIT_BINARY) or "/usr/bin"
# Minimal, fixed child PATH (git finds its transport helpers via the compiled
# GIT_EXEC_PATH, which we leave at its default). Mirrors the runner's _CHILD_PATH.
_CHILD_PATH = os.pathsep.join(
    dict.fromkeys([_GIT_BIN_DIR, "/usr/bin", "/bin", "/usr/local/bin"])
)

# A guaranteed-nonexistent host (RFC 6761 reserves `.invalid`). If curloptResolve
# is NOT honoured, resolving this fails closed and the probe socket is untouched —
# the probe can never accidentally reach a real host.
_PROBE_HOST = "akb-extgit-capability-probe.invalid"

# Env var the `--config-env` probe wires into a config value.
_CONFIG_ENV_VAR = "AKB_EXTGIT_CAP_PROBE"
_CONFIG_ENV_SENTINEL = "curloptResolve-ok"

# Bounds so a wedged probe can never hang the boot. Loopback round-trips are
# sub-100ms; these are generous ceilings, not expected waits.
_GIT_TIMEOUT_SECS = 10.0
_ACCEPT_TIMEOUT_SECS = 5.0


class ExternalGitCapabilityError(RuntimeError):
    """The git build cannot enforce the external-git network-isolation controls. Raised at
    startup to abort the boot (RuntimeError, matching the other startup fail-fasts
    in ``lifecycle``)."""


def _effective_min_version(settings) -> tuple[int, int, int]:
    """The required git version = ``max(hard floor, configured minimum)``. Config
    validation already guarantees the configured value parses and is >= the floor;
    this re-floors defensively so the minimum can never be lowered past 2.37."""
    configured = parse_git_version(settings.external_git_min_git_version) or (0, 0, 0)
    floor = (*EXTERNAL_GIT_MIN_GIT_VERSION_FLOOR, 0)
    return max(floor, configured)


def _installed_git_version(git_binary: str = _GIT_BINARY) -> tuple[int, int, int]:
    """Parse ``git --version`` for the installed git, or raise on failure."""
    try:
        out = subprocess.run(
            [git_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECS,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ExternalGitCapabilityError(
            f"external_git_enabled is true but the git binary could not be run "
            f"({git_binary!r}): {e}. Install git >= "
            f"{_fmt(EXTERNAL_GIT_MIN_GIT_VERSION_FLOOR)} or set "
            "external_git_enabled: false."
        ) from e
    if out.returncode != 0:
        raise ExternalGitCapabilityError(
            f"'git --version' failed (exit {out.returncode}); cannot verify the "
            "external-git capability floor."
        )
    version = parse_git_version(out.stdout)
    if version is None:
        raise ExternalGitCapabilityError(
            f"could not parse a version from 'git --version' output: {out.stdout!r}"
        )
    return version


def _probe_env() -> tuple[dict[str, str], str]:
    """A from-scratch child env mirroring the runner's hermetic env (no os.environ
    inheritance → no ambient ``*_PROXY``/``.netrc``), plus a fresh empty HOME.

    Returns ``(env, home_dir)``; the caller removes ``home_dir`` when done. http is
    allowed here because the probe target is our own loopback — this is internal
    verification, independent of the operator's ``external_git_allow_http``.
    """
    home = tempfile.mkdtemp(prefix="akb-extgit-cap-")
    env = {
        "PATH": _CHILD_PATH,
        "HOME": home,
        "XDG_CONFIG_HOME": home,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "http:https",
        "LC_ALL": "C",
        "LANG": "C",
    }
    return env, home


def _probe_config_env(git_binary: str = _GIT_BINARY) -> None:
    """Verify ``--config-env`` wires an env var into a config value (functional,
    no network). Fast-fails if the feature is missing or misbehaves."""
    env, home = _probe_env()
    env[_CONFIG_ENV_VAR] = _CONFIG_ENV_SENTINEL
    try:
        out = subprocess.run(
            [
                git_binary,
                f"--config-env=akb.capabilityprobe={_CONFIG_ENV_VAR}",
                "config",
                "--get",
                "akb.capabilityprobe",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECS,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ExternalGitCapabilityError(
            f"git '--config-env' capability probe could not run: {e}"
        ) from e
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if out.returncode != 0 or out.stdout.strip() != _CONFIG_ENV_SENTINEL:
        raise ExternalGitCapabilityError(
            "git does not support '--config-env' correctly (the external-git "
            "runner injects the auth header via --config-env). Got exit "
            f"{out.returncode}, output {out.stdout.strip()!r}."
        )


def _pin_value(host: str, port: int, ip: str) -> str:
    """libcurl ``HOST:PORT:ADDR`` — IPv6 address bracketed (matches the runner)."""
    addr = f"[{ip}]" if ":" in ip else ip
    return f"{host}:{port}:{addr}"


def _probe_curlopt_resolve(family: int, bind_addr: str, pin_ip: str) -> bool:
    """Run a hermetic ``git ls-remote`` for ``http://<probe-host>:<port>/`` pinned
    with ``http.curloptResolve`` to ``pin_ip``, and report whether our loopback
    listener received the connection.

    A hit proves libcurl honoured the pin AND no proxy intercepted (the probe env
    inherits no ``*_PROXY`` and passes an empty ``http.proxy``). We do NOT require
    git to exit 0 — our socket answers with a stub, so ls-remote fails; the
    connection itself is the signal. Returns False if the socket was never
    touched (pin ignored / proxied elsewhere / unreachable).

    Because the pin is always passed, a pin-honouring build serves the address
    from the ``curloptResolve`` cache and performs no DNS lookup — the successful
    probe is loopback-only, zero external packets. Only a build that IGNORES the
    pin falls back to resolving the ``.invalid`` host (fail-closed; see the module
    docstring's network-isolation note).
    """
    srv = socket.socket(family, socket.SOCK_STREAM)
    hit = threading.Event()
    env: dict[str, str] | None = None
    home: str | None = None
    try:
        srv.bind((bind_addr, 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def _accept_one() -> None:
            try:
                srv.settimeout(_ACCEPT_TIMEOUT_SECS)
                conn, _addr = srv.accept()
                hit.set()
                try:
                    conn.settimeout(2.0)
                    conn.recv(256)  # drain the request line; content is irrelevant
                    conn.sendall(b"HTTP/1.1 503 probe\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass
                finally:
                    conn.close()
            except (OSError, socket.timeout):
                pass

        listener = threading.Thread(target=_accept_one, daemon=True)
        listener.start()

        env, home = _probe_env()
        url = f"http://{_PROBE_HOST}:{port}/probe.git"
        argv = [
            _GIT_BINARY,
            "-c",
            f"http.curloptResolve={_pin_value(_PROBE_HOST, port, pin_ip)}",
            "-c",
            "http.proxy=",
            "-c",
            f"http.{url}.proxy=",
            "-c",
            "http.followRedirects=false",
            "-c",
            "protocol.http.allow=always",
            "ls-remote",
            "--",
            url,
        ]
        try:
            subprocess.run(
                argv,
                env=env,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SECS,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            pass  # a hang → treat as "not honoured"; hit stays unset
        listener.join(timeout=_ACCEPT_TIMEOUT_SECS + 1.0)
        return hit.is_set()
    finally:
        try:
            srv.close()
        except OSError:
            pass
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)


def check_external_git_capability(settings) -> None:
    """Verify the git build can enforce the external-git network-isolation controls, or abort.

    No-op when ``external_git_enabled`` is false. Otherwise: git-version floor +
    ``--config-env`` probe + curloptResolve DNS-pin probe (IPv4 mandatory, IPv6
    best-effort when ``::1`` is available). Raises
    :class:`ExternalGitCapabilityError` on any failure so startup fails fast.
    """
    if not settings.external_git_enabled:
        logger.info("external_git disabled — skipping git capability check")
        return

    # 1. Build-version gate.
    required = _effective_min_version(settings)
    installed = _installed_git_version()
    if installed < required:
        raise ExternalGitCapabilityError(
            f"external_git_enabled is true but git {_fmt(installed)} is below the "
            f"required minimum {_fmt(required)} (code-owned floor "
            f"{_fmt(EXTERNAL_GIT_MIN_GIT_VERSION_FLOOR)}; http.curloptResolve — the "
            "DNS pin — is documented from git 2.37). Upgrade git or set "
            "external_git_enabled: false."
        )

    # 2. `--config-env` functional probe.
    _probe_config_env()

    # 3. curloptResolve DNS-pin functional probe — IPv4 is mandatory.
    if not _probe_curlopt_resolve(socket.AF_INET, "127.0.0.1", "127.0.0.1"):
        raise ExternalGitCapabilityError(
            "git did not honour http.curloptResolve over IPv4: the loopback DNS-pin "
            "probe was not reached, so the DNS pin the external-git runner "
            "relies on is NOT effective on this build (libcurl without "
            "CURLOPT_RESOLVE, or an intercepting proxy). Refusing to start with "
            "external_git_enabled: true. Fix the git/libcurl build or set "
            "external_git_enabled: false."
        )

    # IPv6 leg — only when this host has an IPv6 loopback to bind. When present,
    # the pin must route to it (a real capability gap otherwise); when absent
    # (no IPv6 stack), IPv4 already proved curloptResolve is honoured, so skip.
    v6_probe = _try_probe_v6()
    if v6_probe is False:
        raise ExternalGitCapabilityError(
            "git did not honour http.curloptResolve over IPv6 ([::1]) even though "
            "an IPv6 loopback is available: the DNS-pin is not effective for "
            "IPv6-resolved mirrors on this build. Fix the git/libcurl build or set "
            "external_git_enabled: false."
        )

    logger.info(
        "external_git capability check passed (git %s; curloptResolve pin + "
        "--config-env verified%s)",
        _fmt(installed),
        "" if v6_probe is None else " for IPv4 and IPv6",
    )


def _try_probe_v6() -> bool | None:
    """Run the IPv6 pin probe, or return None if there is no IPv6 loopback to bind
    (probe not applicable). True/False when the probe actually ran."""
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return None
    try:
        probe.bind(("::1", 0))
    except OSError:
        return None  # no usable IPv6 loopback → not applicable
    finally:
        probe.close()
    return _probe_curlopt_resolve(socket.AF_INET6, "::1", "::1")


def _fmt(version: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in version)
