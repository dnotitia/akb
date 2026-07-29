"""Hermetic execution boundary for every external-mirror git command.

``ExternalGitRunner`` is the SOLE place any git command touches an external
mirror — the three network sinks (clone / fetch / ls-remote) *and* every local
read on a mirror bare repo (ls-tree / cat-file / log / rev-parse / update-ref /
health). Routing all of them through one boundary is the precondition for
closing the "ambient / local git bypass" class.

Why a bespoke executor instead of GitPython convenience methods
---------------------------------------------------------------
``git.cmd.Git.execute`` builds the child environment as ``os.environ.copy()``
and then *overlays* any ``env=`` you pass. So passing ``env=`` can only ADD or
overwrite named vars — it can never guarantee the ABSENCE of an unwanted ambient
var (``HTTPS_PROXY``, ``GIT_CONFIG_COUNT``, ``NETRC``, ``GIT_ASKPASS`` …). This
runner instead spawns git with :class:`subprocess.Popen` and an ``env`` dict
built *from scratch* (subprocess replaces, never merges, the environment), so
the child inherits nothing we did not explicitly grant.

GitPython's ``kill_after_timeout`` is also Linux-only (``ps --ppid``) and only
signals direct children. This runner uses ``start_new_session=True`` +
``os.killpg`` so a timeout reaps the whole process group on macOS and Linux
alike, preserving the existing "kill a wedged git" contract.

Controls applied on every invocation
-------------------------------------
* **Sealed env**: fixed minimal ``PATH``; empty ``HOME`` /
  ``XDG_CONFIG_HOME`` (no ambient ``.netrc`` / ``.gitconfig``);
  ``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` = ``/dev/null`` +
  ``GIT_CONFIG_NOSYSTEM=1`` + ``GIT_CONFIG_COUNT=0``; ``GIT_TERMINAL_PROMPT=0``;
  ``GIT_ALLOW_PROTOCOL`` = ``https`` (or ``http:https`` iff opted in) — this is
  the authoritative scheme gate and, unlike ``protocol.*.allow`` config, cannot
  be re-opened by a stray ``GIT_CONFIG_*``; ``GIT_NO_LAZY_FETCH=1``,
  ``GIT_NO_REPLACE_OBJECTS=1``, ``GIT_LITERAL_PATHSPECS=1``, ``GIT_TRACE_REDACT=1``.
  Everything else (``NETRC``, ``GIT_ASKPASS``, ``*_PROXY``, ``GIT_EXEC_PATH``,
  all ``GIT_TRACE*`` …) is simply never placed in the child env.
* **Belt ``-c`` before the subcommand**: ``protocol.allow=never`` +
  ``protocol.https.allow=always`` (+ http iff opted in), empty
  ``credential.helper`` / ``core.askPass``, ``core.hooksPath`` → a sealed empty
  dir, empty url-scoped ``http.<url>.proxy``, ``http.<url>.followRedirects=false``,
  ``http.sslVerify=true``, ``fetch.recurseSubmodules=no``, and the DNS pin
  ``http.curloptResolve=<host>:<port>:<ip>[,<ip>]`` over every validated address.
* **Credential as a header, never in argv/URL/config**: via
  ``--config-env`` referencing an env var holding
  ``Authorization: Basic <b64(x-access-token:TOKEN)>``. The inherited
  ``extraHeader`` list is reset (empty value) *before* the set so a stale header
  can never survive, and the reset must precede the set for the multi-valued key.

The runner does NOT own the storage layout, the per-vault lock, or the DB — those
stay in :class:`app.services.git_service.GitService`, which composes this runner.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from git import Git as _GitPython

from app.exceptions import AKBError
from app.services.external_git_validation import (
    ExternalGitPolicyError,
    ExternalGitTransientError,
    HostResolver,
    parse_and_canonicalize,
    validate,
)

# Refs are almost always constructed from validated inputs (a validated branch
# name, a materialized 40-hex SHA), but ``update_ref`` / ``delete_ref_quiet`` also
# accept caller-supplied refs. A ref that begins with ``-`` or embeds
# whitespace/NUL would let a ref be parsed as an option; ``_safe_ref`` rejects
# those shapes so that confusion is structurally impossible without relying on the
# ``--end-of-options`` marker being honored by every git ≥ 2.37 subcommand.
_REF_FORBIDDEN_CHARS = ("\x00", "\n", "\r", "\t", " ", "\x7f", "~", "^", ":", "?", "*", "[", "\\")

logger = logging.getLogger("akb.external_git_runner")

# Silence GitPython's own command tracing process-wide. It is an import-time
# class attribute and the string "0"/"off" are truthy, so it must be set to the
# bool False explicitly. We never route a tokened command through
# GitPython, but this closes the leak defensively for every other GitPython use.
_GitPython.GIT_PYTHON_TRACE = False

# Resolve OUR trusted git once from the launch PATH, then invoke it by absolute
# path so the child's (minimal) PATH cannot influence which git binary runs.
_GIT_BINARY = shutil.which("git") or "git"
_GIT_BIN_DIR = os.path.dirname(_GIT_BINARY) or "/usr/bin"
# Fixed minimal PATH for the child. git finds its own helpers via the compiled
# GIT_EXEC_PATH (which we deliberately leave unset so the built-in default is
# used), so this only needs the git binary dir + the standard system bins.
_CHILD_PATH = os.pathsep.join(
    dict.fromkeys([_GIT_BIN_DIR, "/usr/bin", "/bin", "/usr/local/bin"])
)

# Env var name that carries the Authorization header value into the child for
# `--config-env`. The NAME is public (it appears in argv); the VALUE (the token)
# lives only in the child env, never in argv/URL/on-disk config.
_CRED_ENV = "AKB_EXTGIT_EXTRAHEADER"

# A materialized git object id (SHA-1; SHA-256 deferred).
_OID_RE = re.compile(r"^[0-9a-f]{40}$")
# An abbreviated-or-full hex commit id (a reader may be handed a 12-hex short
# hash, e.g. from a vault_log listing). 7..40 hex; resolved to a full OID via a
# hermetic rev-parse before use.
_HEX_ABBREV_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Field/record separators for parsing `git log --format=` output. US (0x1f)
# between fields, RS (0x1e) between records — control chars that never occur in
# commit metadata, so no quoting/escaping games are possible.
_US = "\x1f"
_RS = "\x1e"

# A git-config section header: `[section]`, `[section "subsection"]`, or the
# legacy dotted `[section.subsection]` (git parses `[remote.origin]` exactly like
# `[remote "origin"]`, so the inspector must too).
_CONFIG_SECTION_RE = re.compile(r'^\s*\[([A-Za-z0-9._-]+)(?:\s+"(.*)")?\]\s*$')

# ── Structure default-deny allowlist ───────────
# Only the exact sections/keys/VALUES a fresh `git clone --bare --single-branch
# --no-tags` legitimately writes are ALLOWED; every other entry — and every
# wrong VALUE on an allowed key — is a finding. Findings are strictly VALUE-LESS
# descriptors drawn from a FIXED vocabulary (never the caller-controlled key
# name, subsection, or value, any of which can carry a URL/token), so they are
# safe to log and persist.

# The object format a fresh clone yields in the SHA-1 world this reader assumes
# (materialized OIDs are 40-hex; SHA-256 is deferred). A clean SHA-1
# clone writes NO `[extensions]` section at all, so an `extensions.objectFormat`
# that is present and not this value is a finding.
_EXPECTED_OBJECT_FORMAT = "sha1"

# The `[core]` keys a fresh bare clone writes, grouped by how their VALUE is
# checked. Verified empirically (git 2.37–2.51): `git clone --bare
# --single-branch --no-tags` and `git init --bare` write repositoryformatversion
# + filemode + bare plus the platform-detection booleans, and NOTHING else — no
# logallrefupdates, no fetch refspec, no `[branch]` section (bare repos never
# persist reflogs or branch tracking).
#   * repositoryformatversion — must be exactly `0` (a `1` implies extensions:
#     a non-SHA-1 / reftable repo this reader does not support).
#   * bare — must be boolean-true (a bare repo that claims `bare = false` is
#     a structure-mismatch finding).
#   * the platform-detection booleans — value must parse as a git boolean; they
#     never carry a command or a path.
# Every other `[core]` key (sshCommand, fsmonitor, pager, hooksPath, …) is a
# finding: a fresh bare clone never writes one.
_CORE_BOOL_KEYS = frozenset({"filemode", "ignorecase", "precomposeunicode", "symlinks"})

# git boolean spellings (config truthiness), lower-cased; a valueless key
# (`bare` with no `=`) is git-true.
_GIT_BOOL_TRUE = frozenset({"true", "yes", "on", "1", ""})
_GIT_BOOL_FALSE = frozenset({"false", "no", "off", "0"})

# A `.git/config` (and the special files below) must be a regular non-symlink
# under this size cap; anything else is fail-closed (a symlink out of the repo,
# or an absurdly large planted config).
_MAX_CONFIG_BYTES = 1 * 1024 * 1024

# On-disk files that redirect the object graph / worktree / transport. ANY
# presence (regular file OR symlink) is a finding — these never appear in a
# clean single-branch bare clone.
_SPECIAL_FILES = (
    ("config.worktree", "config.worktree present (extensions.worktreeConfig)"),
    ("commondir", "commondir present (linked-worktree indicator)"),
    ("shallow", "unexpected shallow marker"),
    ("info/grafts", "info/grafts present"),
    ("objects/info/alternates", "objects/info/alternates present"),
    # Dumb-HTTP alternate — a remote can point this off-origin; git
    # exposes no config knob to disable following it (see the deferred egress-NetworkPolicy note in
    # _net_global_args), so the structure default-deny is the closure here.
    ("objects/info/http-alternates", "objects/info/http-alternates present"),
)

# Re-guard limits for the auth token before it is base64'd into a header. The
# validator already enforces these; this is defense-in-depth at the exec seam.
_MAX_TOKEN_LEN = 4096
_FORBIDDEN_TOKEN_CHARS = ("\r", "\n", "\x00")


# ── Errors ───────────────────────────────────────────────────────────
class ExternalGitCommandError(AKBError):
    """A mirror git command exited non-zero. Message is pre-sanitized (no raw
    URL / token / base64 header) so it is safe to log or persist to
    ``vault_external_git.last_error``."""

    def __init__(self, message: str, *, returncode: int | None = None):
        super().__init__(message, status_code=502, code="external_git_command_failed")
        self.returncode = returncode


class ExternalGitOutputCapError(AKBError):
    """A content/diff read produced more bytes than the caller's cap; the process
    group was SIGKILLed BEFORE the full (oversized) output was materialized
    (streaming backstop).

    This is the general memory-growth guard behind the per-blob size pre-check: even
    when a symbolic rev is promoted between the size check and the read, or a
    ``diff-tree -p`` would buffer a large pre-image, the streaming reader aborts
    at ``max_output_bytes`` instead of letting git buffer the whole thing. It is
    surfaced as a 413 like ``git_service.ExternalGitOversizedError`` (same code),
    but is DELIBERATELY NOT a subclass of :class:`ExternalGitCommandError` so the
    ``except ExternalGitCommandError`` handlers that map a genuinely-missing path
    to ``None`` (``cat_path`` / ``path_size`` / ``file_diff_entry``) cannot swallow
    an oversized abort into a silent "not found". The message is value-less (a
    byte count only) — safe to log or persist."""

    def __init__(self, cap: int, *, observed: int | None = None):
        detail = f" (read >= {observed} bytes)" if observed is not None else ""
        super().__init__(
            f"external-git mirror read exceeded the {cap}-byte output cap{detail}; "
            "refusing to materialize it",
            status_code=413,
            code="external_git_blob_oversized",
        )
        self.cap = cap
        self.observed = observed


# Streaming-read tunables for the bounded (``max_output_bytes``) content/diff
# path. The stdout drain reads in these chunks; the stderr sidecar keeps at most
# this many bytes (git errors are short — bounded so a remote cannot exhaust
# memory through the error channel either).
_STDOUT_READ_CHUNK = 65536
_STDERR_KEEP_BYTES = 65536


# ── Sealed scratch dirs (empty HOME + empty hooks dir), created once ──
_sealed_lock = threading.Lock()
_sealed_dirs_cache: tuple[str, str] | None = None


def _sealed_dirs() -> tuple[str, str]:
    """Return ``(home, hooks)`` — two empty, process-lifetime directories.

    ``home`` is the sealed ``HOME`` / ``XDG_CONFIG_HOME`` (so git finds no
    ambient ``.gitconfig`` / ``.netrc``); ``hooks`` is the ``core.hooksPath``
    target (so no repo-local or ambient hook can execute). Both are empty and
    read-only in practice, so a single shared pair is safe for all vaults.
    """
    global _sealed_dirs_cache
    if _sealed_dirs_cache is None:
        with _sealed_lock:
            if _sealed_dirs_cache is None:
                # realpath so GIT_CEILING_DIRECTORIES (a textual, symlink-sensitive
                # comparison) matches the path git derives for cwd on hosts where
                # $TMPDIR is a symlink (macOS /var → /private/var).
                base = os.path.realpath(tempfile.mkdtemp(prefix="akb-extgit-sealed-"))
                home = os.path.join(base, "home")
                hooks = os.path.join(base, "hooks")
                os.makedirs(home, exist_ok=True)
                os.makedirs(hooks, exist_ok=True)
                _sealed_dirs_cache = (home, hooks)
    return _sealed_dirs_cache


# ── Small pure helpers ───────────────────────────────────────────────
def _bracket_ip(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


def _is_ip_literal(host: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _curlopt_resolve(host: str, port: int, pinned_ips: tuple[str, ...]) -> str | None:
    """Build the ``http.curloptResolve`` value (libcurl ``HOST:PORT:ADDR[,ADDR]``).

    Returns None for an IP-literal host: there is no name to override, and the
    literal is already the validated, pinned target in the URL itself.
    """
    if _is_ip_literal(host) or not pinned_ips:
        return None
    addrs = ",".join(_bracket_ip(ip) for ip in pinned_ips)
    return f"{host}:{port}:{addrs}"


def _cred_header(token: str | None) -> str | None:
    """``Authorization: Basic <b64(x-access-token:TOKEN)>`` or None.

    Re-guards CR/LF/NUL and length even though the validator already did — a
    stray newline here would break out of the header at the very last step."""
    if not token:
        return None
    if len(token) > _MAX_TOKEN_LEN or any(c in token for c in _FORBIDDEN_TOKEN_CHARS):
        raise ExternalGitPolicyError("external_git auth token failed exec-time re-validation")
    b64 = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {b64}"


def _require_oid(value: str, *, what: str) -> str:
    if not isinstance(value, str) or not _OID_RE.fullmatch(value):
        raise ExternalGitPolicyError(f"external_git {what} is not a 40-hex object id")
    return value


def _safe_ref(value: str, *, what: str) -> str:
    """Reject a ref/refspec-operand shape that could be parsed as an option or
    carry control characters. Not a full ``check-ref-format`` — just enough
    that the value cannot begin with ``-`` (would be parsed as an option) or
    carry whitespace/NUL/ref-magic that has no place in a literal ref we build
    ourselves."""
    if not isinstance(value, str) or not value:
        raise ExternalGitPolicyError(f"external_git {what} must be a non-empty string")
    if value.startswith("-"):
        raise ExternalGitPolicyError(f"external_git {what} must not start with '-'")
    if any(c in value for c in _REF_FORBIDDEN_CHARS):
        raise ExternalGitPolicyError(f"external_git {what} contains a disallowed character")
    return value


def _blob_oid_from_ls_tree(out: str, path: str) -> str | None:
    """Classify ``ls-tree -r -z --full-tree <commit> -- <path>`` stdout into a
    blob OID / genuine-absent / real-anomaly (fail-closed typed lookup).

    Returns the 40-hex OID of the blob row whose path equals ``path``; ``None``
    when the listing is EMPTY (the path is genuinely absent at that commit) or the
    exact path names a non-blob (a tree / submodule — no blob to size / read);
    raises :class:`ExternalGitCommandError` on a MALFORMED row or a row whose
    object field is NOT a 40-hex OID — a real anomaly is never a silent miss. The
    caller runs ls-tree against an already-resolved commit OID with ``check=True``,
    so a NONZERO exit (a corrupt root tree / IO error) has already propagated
    before this ever parses stdout."""
    entries = [e for e in out.split("\0") if e]
    if not entries:
        return None  # empty tree listing at this pathspec → genuinely absent
    for entry in entries:
        meta, tab, entry_path = entry.partition("\t")
        parts = meta.split()
        # `<mode> <type> <object>` TAB `<path>`. A row missing the TAB or any of
        # the three metadata fields is malformed — fail closed rather than guess.
        if not tab or len(parts) < 3:
            raise ExternalGitCommandError("git ls-tree returned a malformed row")
        obj_type, oid = parts[1], parts[2]
        if entry_path != path:
            continue  # a sibling under a directory pathspec — not our blob
        if obj_type != "blob":
            return None  # the exact path names a tree / submodule, not a blob
        if not _OID_RE.fullmatch(oid):
            raise ExternalGitCommandError("git ls-tree returned a non-OID object field")
        return oid
    return None  # rows present but none is the exact literal path → no blob here


class ExternalGitRunner:
    """The hermetic git execution boundary for external mirrors.

    ``settings`` defaults to the process ``app.config.settings`` (read lazily so
    tests and config reloads see the current object). ``resolver`` is the
    disposable host resolver used by Layer-2 re-validation; injectable so tests
    can pin a fixture host without real DNS.
    """

    def __init__(
        self,
        *,
        settings=None,
        resolver: HostResolver | None = None,
        git_binary: str | None = None,
    ):
        self._settings = settings
        self._resolver = resolver
        self._git = git_binary or _GIT_BINARY

    # ── settings/env plumbing ────────────────────────────────────────
    def _settings_now(self):
        if self._settings is not None:
            return self._settings
        from app.config import settings as s

        return s

    def _allow_http(self) -> bool:
        return bool(self._settings_now().external_git_allow_http)

    def _base_env(self) -> dict[str, str]:
        """Child env built FROM SCRATCH — nothing is inherited from os.environ.

        The excluded vars (NETRC, GIT_ASKPASS, SSH_ASKPASS, GIT_COMMON_DIR,
        GIT_OBJECT_DIRECTORY, GIT_ALTERNATE_OBJECT_DIRECTORIES, GIT_EXEC_PATH,
        GIT_TRACE*, GIT_CURL_VERBOSE, GIT_PYTHON_TRACE, *_PROXY, TLS overrides)
        are absent simply because we never add them.
        """
        home, _hooks = _sealed_dirs()
        return {
            "PATH": _CHILD_PATH,
            "HOME": home,
            "XDG_CONFIG_HOME": home,
            # The no-GIT_DIR ops (clone / ls-remote) run with cwd = the sealed
            # empty HOME. Without a ceiling, git repository *discovery* would walk
            # up out of the sealed dir (/tmp, /, …) and could adopt an ambient
            # repo-local config. Ceiling at the sealed base
            # stops the walk immediately ("runner fixes cwd too"). The
            # GIT_DIR ops set GIT_DIR explicitly, which short-circuits discovery,
            # so this only ever tightens.
            "GIT_CEILING_DIRECTORIES": os.path.dirname(home),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": "http:https" if self._allow_http() else "https",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_TRACE_REDACT": "1",
            # Stable, parseable output regardless of the host locale.
            "LC_ALL": "C",
            "LANG": "C",
        }

    def _common_global_args(self) -> list[str]:
        """Belt ``-c`` options safe for every op (local and network)."""
        _home, hooks = _sealed_dirs()
        return [
            "-c",
            f"core.hooksPath={hooks}",
            "-c",
            "core.askPass=",
            "-c",
            "credential.helper=",
        ]

    def _net_global_args(
        self,
        canonical_url: str,
        host: str,
        port: int,
        pinned_ips: tuple[str, ...],
        *,
        has_cred: bool,
    ) -> list[str]:
        """Global ``-c`` / ``--config-env`` options for a NETWORK op — emitted
        BEFORE the subcommand (never as a clone subcommand ``-c``, which would
        persist into the repo config)."""
        args = list(self._common_global_args())
        # Reset the (possibly inherited) extraHeader list FIRST; an empty value
        # clears the multi-valued key. Only then set the credential from env —
        # a reset placed AFTER the set would wipe the header we just added.
        args += ["-c", f"http.{canonical_url}.extraHeader="]
        if has_cred:
            args += [f"--config-env=http.{canonical_url}.extraHeader={_CRED_ENV}"]
        args += ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
        if self._allow_http():
            args += ["-c", "protocol.http.allow=always"]
        args += [
            "-c",
            f"http.{canonical_url}.proxy=",
            "-c",
            f"http.{canonical_url}.followRedirects=false",
            # GLOBAL redirect-follow off, not just url-scoped: a url-scoped
            # http.<canonical>.followRedirects only governs requests to the
            # canonical URL, so a redirect to a DIFFERENT (secondary) host would
            # not be covered. The global switch refuses redirects on every host.
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.sslVerify=true",
            "-c",
            "fetch.recurseSubmodules=no",
            # Disable server-advertised bundle-URI and packfile-URI fetching.
            # Both would pull objects from a URI the remote chooses
            # — a host NOT covered by our curloptResolve pin.
            # ``fetch.bundleURI`` is reset to empty FIRST so a value planted in an
            # inherited repo config cannot survive; ``transfer.bundleURI=false``
            # and an empty ``fetch.uriProtocols`` (deny every protocol for
            # packfile-URIs) pin the capabilities off so a future default flip
            # cannot re-open the hole.
            "-c",
            "fetch.bundleURI=",
            "-c",
            "transfer.bundleURI=false",
            "-c",
            "fetch.uriProtocols=",
        ]
        # DNS pin. curloptResolve is a MULTI-valued config key: our pin
        # would be APPENDED to any value planted in an inherited repo config,
        # leaving a stale resolve entry live. Reset the list to empty
        # FIRST (git treats an empty value as "clear the list"), then append only
        # our validated pin.
        args += ["-c", "http.curloptResolve="]
        resolve = _curlopt_resolve(host, port, pinned_ips)
        if resolve:
            args += ["-c", f"http.curloptResolve={resolve}"]
        # NOTE (egress NetworkPolicy — DEFERRED): the controls above
        # close the secondary-host egress reachable via git *config* (bundle-URI,
        # packfile-URI, redirects, the DNS pin). They do NOT constrain ARBITRARY
        # secondary-host egress that git may perform with no config lever — most
        # notably dumb-HTTP ``objects/info/http-alternates``, which a
        # remote can point off-origin and for which git exposes no disable knob.
        # Closing that class requires forcing every git HTTP request through an
        # allowlisting egress proxy (egress proxy shim) plus a packet-level
        # egress NetworkPolicy (isolation P0). Until then it is mitigated by (a)
        # the structure default-deny, which quarantines/re-clones any repo that
        # carries an off-origin ``objects/info/http-alternates`` or a secondary
        # fetch key, and (b) single-branch clone/fetch over the smart protocol,
        # which does not consult served alternates.
        return args

    # ── the executor ─────────────────────────────────────────────────
    def _exec(
        self,
        args: list[str],
        *,
        cwd: str,
        timeout: float,
        git_dir: str | None = None,
        cred_header: str | None = None,
        want_bytes: bool = False,
        check: bool = True,
        redact_url: str | None = None,
        redact_cred: str | None = None,
        redact_token: str | None = None,
        max_output_bytes: int | None = None,
    ) -> bytes | str:
        """Run ``git <args>`` with the sealed env + process-group kill.

        On timeout the whole process group is SIGKILLed and an
        ``ExternalGitTransientError`` (backoff, not quarantine) is raised. On a
        non-zero exit a sanitized ``ExternalGitCommandError`` is raised.

        ``max_output_bytes`` bounds a CONTENT/DIFF read:
        when set, stdout is STREAMED rather than fully buffered by
        ``communicate()``, and the moment it exceeds the cap the process group is
        killed and :class:`ExternalGitOutputCapError` is raised — so an oversized
        blob or a large diff pre-image can never be materialized into memory,
        even across the size-check↔read TOCTOU. Leave it ``None`` for metadata
        commands (ls-tree / log / rev-parse / cat-file -s), whose output is
        small and whose full-buffer ``communicate()`` path is unchanged.
        """
        env = self._base_env()
        if cred_header is not None:
            env[_CRED_ENV] = cred_header
        if git_dir is not None:
            # In the sealed env GIT_DIR is the only way an inherited GIT_DIR
            # could have leaked in; we set it explicitly and also
            # pass --git-dir in argv so the two agree on exactly this repo.
            env["GIT_DIR"] = str(git_dir)

        argv = [self._git, *args]
        proc = subprocess.Popen(  # noqa: S603 — fixed absolute git, no shell, list argv
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group → killpg reaps the tree
        )
        if max_output_bytes is not None:
            out, err = self._communicate_capped(
                proc, timeout=timeout, max_output_bytes=max_output_bytes
            )
        else:
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_group(proc)
                try:
                    out, err = proc.communicate(timeout=5)
                except Exception:  # noqa: BLE001 — already killed; give up reaping detail
                    out, err = b"", b""
                finally:
                    # Reap even if the second communicate() raised, so a killed
                    # child never leaks a zombie or an open pipe FD.
                    self._reap(proc)
                raise ExternalGitTransientError(
                    f"git command timed out after {timeout:g}s"
                ) from None

        if check and proc.returncode != 0:
            safe = _sanitize(err, url=redact_url, cred=redact_cred, token=redact_token)
            raise ExternalGitCommandError(
                f"git command failed (exit {proc.returncode}): {safe}",
                returncode=proc.returncode,
            )
        if want_bytes:
            return out
        return out.decode("utf-8", "replace")

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        import signal

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        """Best-effort reap after a kill: close every pipe FD and ``wait()`` so a
        killed child leaves no zombie or leaked descriptor, even when the second
        ``communicate()`` itself raised. Never propagates — reaping
        must not mask the timeout error being raised by the caller."""
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 — truly wedged (D-state); nothing more to do
            pass

    def _communicate_capped(
        self, proc: subprocess.Popen, *, timeout: float, max_output_bytes: int
    ) -> tuple[bytes, bytes]:
        """Read ``proc``'s stdout with a hard byte cap instead of buffering it
        whole. Returns ``(stdout, stderr)`` on a clean
        finish; raises :class:`ExternalGitOutputCapError` the instant stdout
        passes ``max_output_bytes`` (process group killed + reaped first), and
        :class:`ExternalGitTransientError` on the wall-clock ``timeout`` — the
        SAME kill/backoff contract as the uncapped path.

        A SINGLE deadline (``timeout`` measured from entry) governs the WHOLE
        operation — the stdout/stderr drain AND the final process wait — so a
        child that lingers AFTER closing its pipes cannot stretch the wall clock
        past ``timeout`` (the old code added a fixed +5s ``wait`` after
        EOF, overshooting the caller's deadline).

        stdout and stderr are drained together through ONE :mod:`selectors`
        readiness loop (``DefaultSelector`` → kqueue/epoll, which — unlike
        ``select.select`` — has no ``FD_SETSIZE`` ceiling, so a high-numbered FD
        is polled rather than raising), so there is no background thread and no
        FD whose lifetime outlives this call. The ENTIRE drain+wait runs under one
        ``try/finally``: on EVERY abnormal exit — cap, timeout, OR an
        ``OSError``/``ValueError`` out of selector registration, ``sel.select``,
        or ``os.read`` (a bad / out-of-range FD) — the ``finally`` SIGKILLs the
        process group, closes both pipes, and reaps the child, so no
        orphan / zombie / leaked descriptor can survive any fault. On
        the clean path the child is already reaped and only the pipe ends are
        closed. stderr is bounded to ``_STDERR_KEEP_BYTES`` — only its head feeds
        the sanitized error message — but is still read to EOF so a chatty error
        channel can neither dead-lock the stdout read (git blocking on a full
        stderr pipe while we block on stdout) nor OOM us in its own right.
        """
        out = bytearray()
        stderr_buf = bytearray()
        stdout_fd = proc.stdout.fileno() if proc.stdout is not None else -1
        deadline = time.monotonic() + timeout
        sel = selectors.DefaultSelector()
        reaped = False  # set only after a clean drain + in-deadline wait
        try:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    sel.register(stream, selectors.EVENT_READ)
            while sel.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExternalGitTransientError(
                        f"git command timed out after {timeout:g}s"
                    )
                for key, _mask in sel.select(remaining):
                    try:
                        chunk = os.read(key.fd, _STDOUT_READ_CHUNK)
                    except OSError:
                        sel.unregister(key.fileobj)  # pipe torn down → EOF
                        continue
                    if not chunk:
                        sel.unregister(key.fileobj)  # EOF — child closed stream
                        continue
                    if key.fd == stdout_fd:
                        out.extend(chunk)
                        if len(out) > max_output_bytes:
                            raise ExternalGitOutputCapError(
                                max_output_bytes, observed=len(out)
                            )
                    elif len(stderr_buf) < _STDERR_KEEP_BYTES:
                        # Keep only the head; the read above still drains the rest
                        # so the child never blocks on a full stderr pipe.
                        stderr_buf.extend(
                            chunk[: _STDERR_KEEP_BYTES - len(stderr_buf)]
                        )
            # Both pipes hit EOF. Reap the exit status under the SAME deadline — a
            # child that wrote all its output but lingers before exiting cannot
            # push the wall clock past ``timeout`` (single deadline).
            remaining = deadline - time.monotonic()
            try:
                proc.wait(timeout=max(0.0, remaining))
            except subprocess.TimeoutExpired:
                raise ExternalGitTransientError(
                    f"git command timed out after {timeout:g}s"
                ) from None
            reaped = True
            return bytes(out), bytes(stderr_buf)
        finally:
            sel.close()
            if not reaped:
                # ANY abnormal exit reaches here with the child possibly still
                # alive and the pipes open — a cap / timeout raise, or an
                # OSError / ValueError out of selector registration, ``sel.select``,
                # or ``os.read`` (a bad / out-of-range FD). Kill the whole process
                # group, then close every pipe + wait, so nothing leaks.
                self._kill_group(proc)
                self._reap(proc)
            else:
                # Clean finish: the child is already reaped; close our pipe ends.
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except OSError:
                        pass

    # ── Layer-2 re-validation (per network op, TOCTOU / stale resolution) ──
    def _validate(self, remote_url: str, branch: str, auth_token: str | None):
        return validate(
            {"remote_url": remote_url, "remote_branch": branch, "auth_token": auth_token},
            settings=self._settings_now(),
            resolve=True,
            resolver=self._resolver,
        )

    # ── Network ops ──────────────────────────────────────────────────
    def clone_bare(
        self,
        remote_url: str,
        branch: str,
        auth_token: str | None,
        dest_bare_path: str | os.PathLike[str],
        *,
        timeout: float | None = None,
    ) -> str:
        """Clone the validated remote branch into ``dest_bare_path`` (which must
        NOT yet exist) and return the LOCAL materialized SHA of the branch.

        A clone into a fresh dir is inherently sterile: there is no pre-existing
        repo config, so the ``<repository>``-as-remote-name redirection
        is impossible on this path.
        """
        vr = self._validate(remote_url, branch, auth_token)
        cred = _cred_header(auth_token)
        timeout = timeout or self._settings_now().external_git_clone_timeout
        home, _ = _sealed_dirs()
        globals_ = self._net_global_args(
            vr.canonical_url, vr.host, vr.port, vr.pinned_ips, has_cred=cred is not None
        )
        argv = [
            *globals_,
            "clone",
            "--bare",
            "--single-branch",
            "--no-tags",  # never fetch tags — only the one branch tip
            "--branch",
            vr.branch,
            "--",
            vr.canonical_url,
            str(dest_bare_path),
        ]
        self._exec(
            argv,
            cwd=home,
            timeout=timeout,
            cred_header=cred,
            redact_url=vr.canonical_url,
            redact_cred=cred,
            redact_token=auth_token,
        )
        return self.rev_parse(str(dest_bare_path), f"refs/heads/{vr.branch}")

    def fetch_to_ref(
        self,
        bare_path: str | os.PathLike[str],
        remote_url: str,
        branch: str,
        auth_token: str | None,
        tmp_ref: str,
        *,
        timeout: float | None = None,
    ) -> None:
        """Fetch the validated remote branch into ``tmp_ref`` in the existing
        bare repo. Network I/O only — the caller promotes ``tmp_ref`` onto
        ``refs/heads/<branch>`` under its lock and reads the materialized SHA.
        """
        vr = self._validate(remote_url, branch, auth_token)
        cred = _cred_header(auth_token)
        timeout = timeout or self._settings_now().external_git_fetch_timeout
        globals_ = self._net_global_args(
            vr.canonical_url, vr.host, vr.port, vr.pinned_ips, has_cred=cred is not None
        )
        # Exact contract: `fetch -- <url> +refs/heads/<branch>:<tmp_ref>`.
        # Submodule recursion is already disabled by the belt
        # `fetch.recurseSubmodules=no`; the explicit single-branch refspec means
        # only the requested head is written, into the caller-owned tmp ref.
        _safe_ref(tmp_ref, what="fetch tmp ref")
        argv = [
            f"--git-dir={bare_path}",
            *globals_,
            "fetch",
            # Explicit minimal-surface contract: no tags, no submodule
            # recursion (the belt fetch.recurseSubmodules=no already forbids it;
            # the flag makes the contract explicit in argv).
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            vr.canonical_url,
            f"+refs/heads/{vr.branch}:{tmp_ref}",
        ]
        self._exec(
            argv,
            cwd=str(bare_path),
            git_dir=str(bare_path),
            timeout=timeout,
            cred_header=cred,
            redact_url=vr.canonical_url,
            redact_cred=cred,
            redact_token=auth_token,
        )

    def ls_remote_sha(
        self,
        remote_url: str,
        branch: str,
        auth_token: str | None,
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Return the remote branch tip SHA (a change-detection HINT only, never
        a materialized cursor), or None if the branch is absent.

        Runs with NO local repo (cwd = sealed empty dir, no GIT_DIR) so no repo
        config is ever consulted; the URL cannot be reinterpreted as a
        repo-local remote name here.
        """
        vr = self._validate(remote_url, branch, auth_token)
        cred = _cred_header(auth_token)
        timeout = timeout or self._settings_now().external_git_lsremote_timeout
        home, _ = _sealed_dirs()
        globals_ = self._net_global_args(
            vr.canonical_url, vr.host, vr.port, vr.pinned_ips, has_cred=cred is not None
        )
        argv = [
            *globals_,
            "ls-remote",
            "--",
            vr.canonical_url,
            f"refs/heads/{vr.branch}",
        ]
        out = self._exec(
            argv,
            cwd=home,
            timeout=timeout,
            cred_header=cred,
            redact_url=vr.canonical_url,
            redact_cred=cred,
            redact_token=auth_token,
        )
        assert isinstance(out, str)
        want = f"refs/heads/{vr.branch}"
        for line in out.splitlines():
            sha, _tab, ref = line.partition("\t")
            if ref == want:  # EXACT ref match, not endswith
                sha = sha.strip()
                if _OID_RE.fullmatch(sha):
                    return sha
                return None
        return None

    # ── Local reads (repo-scoped, no network, no credential) ─────────
    def _local(
        self,
        bare_path: str | os.PathLike[str],
        sub_args: list[str],
        *,
        want_bytes: bool = False,
        check: bool = True,
        max_output_bytes: int | None = None,
    ) -> bytes | str:
        timeout = self._settings_now().git_write_timeout_secs
        argv = [f"--git-dir={bare_path}", *self._common_global_args(), *sub_args]
        return self._exec(
            argv,
            cwd=str(bare_path),
            git_dir=str(bare_path),
            timeout=timeout,
            want_bytes=want_bytes,
            check=check,
            max_output_bytes=max_output_bytes,
        )

    def rev_parse(self, bare_path: str | os.PathLike[str], rev: str) -> str:
        out = self._local(bare_path, ["rev-parse", "--verify", "--end-of-options", rev])
        assert isinstance(out, str)
        return out.strip()

    def head_commit_present(self, bare_path: str | os.PathLike[str]) -> bool:
        try:
            self.rev_parse(bare_path, "HEAD^{commit}")
            return True
        except Exception:  # noqa: BLE001 — any failure means "no sound HEAD"
            return False

    def ls_tree(self, bare_path: str | os.PathLike[str], sha: str) -> dict[str, str]:
        """``{path: blob_sha}`` for every blob reachable from ``sha`` (a commit
        or tree). NUL-delimited so paths with special chars are never quoted."""
        _require_oid(sha, what="tree sha")
        out = self._local(
            bare_path, ["ls-tree", "-r", "-z", "--full-tree", "--end-of-options", sha]
        )
        assert isinstance(out, str)
        result: dict[str, str] = {}
        for entry in out.split("\0"):
            if not entry:
                continue
            meta, _tab, path = entry.partition("\t")
            parts = meta.split()
            # `<mode> <type> <object>` — keep blobs; skip submodule commits/trees.
            if len(parts) >= 3 and parts[1] == "blob" and path:
                result[path] = parts[2]
        return result

    def cat_blob(
        self,
        bare_path: str | os.PathLike[str],
        blob_sha: str,
        *,
        max_output_bytes: int | None = None,
    ) -> bytes:
        """Raw bytes of the blob ``blob_sha`` (an EXACT, immutable 40-hex OID).

        ``max_output_bytes`` streams the read with a hard cap:
        the caller sizes the OID with :meth:`blob_size` and refuses an
        over-cap blob first, so this bound is a defence-in-depth backstop the
        normal path never trips (the OID is content-addressed, so the read is the
        exact size that was checked)."""
        _require_oid(blob_sha, what="blob sha")
        out = self._local(
            bare_path,
            ["cat-file", "blob", "--end-of-options", blob_sha],
            want_bytes=True,
            max_output_bytes=max_output_bytes,
        )
        assert isinstance(out, bytes)
        return out

    def resolve_blob_oid(
        self, bare_path: str | os.PathLike[str], rev: str, path: str
    ) -> str | None:
        """Resolve ``<rev>:<path>`` to its immutable 40-hex BLOB oid (exact-OID
        binding), or None ONLY when the path is
        genuinely ABSENT at a resolvable rev. Binding the oversized SIZE check and
        the READ to this ONE oid closes the "HEAD promoted between the size check
        and the read" TOCTOU: a concurrent fetch cannot swap a small sized blob
        for a large materialized one, because both operate on the exact oid
        resolved here. ``rev`` is a materialized 40-hex OID or ``HEAD``.

        Fail-closed typed lookup. git's ``rev-parse --quiet``
        cannot tell a genuine absence from object-store CORRUPTION — a missing
        commit / tree surfaces as the SAME ``exit 1`` + empty stderr as an absent
        path — so masking every such exit as None folded real corruption into
        "absent", and the diff / oversized gate then read it as "image absent" and
        skipped the per-image blob cap. Instead this resolves in two hermetic,
        typed steps, each on the sealed ``_local`` boundary (GIT_LITERAL_PATHSPECS):

          1. ``rev-parse --verify <rev>^{commit}`` pins ``rev`` to a full commit
             OID. A missing / corrupt commit fails HERE and PROPAGATES as
             :class:`ExternalGitCommandError` (never a silent None).
          2. ``ls-tree -r -z --full-tree <commit-oid> -- <literal-path>`` reads
             the blob row: rc0 + the exact blob row → its 40-hex OID; rc0 + EMPTY
             → the path is genuinely absent → None; a NONZERO exit (a corrupt root
             tree / IO error) PROPAGATES via ``_local`` (check=True); a malformed
             row or a non-40-hex object field PROPAGATES
             (:func:`_blob_oid_from_ls_tree`). ``path`` is a literal pathspec after
             ``--`` so pathspec magic never applies — the same sealed boundary as
             :meth:`cat_path`.

        Net: ONLY a genuinely-absent path at a resolvable commit collapses to
        None; every corruption / anomaly fails closed, so :meth:`path_size` and
        its diff caller can no longer misread damage as "not oversized, proceed"."""
        if rev != "HEAD":
            _require_oid(rev, what="rev")
        # Step 1: pin rev → full commit OID (a missing / corrupt commit propagates
        # rather than being mistaken for an absent path).
        commit_oid = self.rev_parse(bare_path, f"{rev}^{{commit}}")
        # Step 2: hermetic typed tree lookup against that EXACT commit OID. A
        # nonzero exit (corrupt tree / IO error) raises via _local (check=True) and
        # PROPAGATES; rc0 stdout is classified by _blob_oid_from_ls_tree (exact
        # blob row → oid, empty → None, malformed / non-OID → propagate).
        out = self._local(
            bare_path,
            [
                "ls-tree", "-r", "-z", "--full-tree",
                "--end-of-options", commit_oid, "--", path,
            ],
        )
        assert isinstance(out, str)
        return _blob_oid_from_ls_tree(out, path)

    def first_parent_oid(
        self, bare_path: str | os.PathLike[str], commit: str
    ) -> str | None:
        """40-hex OID of ``commit``'s FIRST parent, or None if ``commit`` is a
        root commit (no parent) or does not resolve. Lets a caller size a diff's
        PRE-image blob (``<parent>:<path>``) before it is materialized
        (diff parent+post). ``commit`` is a materialized 40-hex
        OID; ``<commit>^`` selects the first parent, matching ``diff-tree``'s
        default first-parent diff."""
        _require_oid(commit, what="commit")
        try:
            out = self._local(
                bare_path,
                ["rev-parse", "--verify", "--end-of-options", f"{commit}^"],
            )
        except ExternalGitCommandError:
            return None  # root commit (no parent) or unresolvable
        assert isinstance(out, str)
        oid = out.strip()
        return oid if _OID_RE.fullmatch(oid) else None

    def blob_size(self, bare_path: str | os.PathLike[str], blob_sha: str) -> int:
        """Byte size of a blob without materializing it (``cat-file -s``).

        The bounded-read primitive behind the oversized gate:
        ``git_service.blob_exceeds_max`` sizes a blob with this BEFORE
        ``cat_blob`` so an over-cap blob is never read into memory — on the
        reconcile skip/tombstone path AND on the direct-read backstop
        (``git_service.cat_blob``). Returns a size only; the cap
        comparison lives in ``git_service`` (which owns ``settings``)."""
        _require_oid(blob_sha, what="blob sha")
        out = self._local(bare_path, ["cat-file", "-s", "--end-of-options", blob_sha])
        assert isinstance(out, str)
        return int(out.strip())

    def path_size(
        self, bare_path: str | os.PathLike[str], rev: str, path: str
    ) -> int | None:
        """Byte size of the blob at ``<rev>:<path>`` WITHOUT materializing it, or
        None ONLY when the path is genuinely absent at a resolvable rev.

        The bounded-read primitive for the oversized gate on the object-name READ
        paths: ``git_service`` sizes ``<rev>:<path>`` with this
        before materializing content, so an over-cap blob is refused before it is
        read (a historical document read, or a diff pre/post image). ``rev`` is a
        materialized 40-hex OID or ``HEAD``.

        Implemented as ``resolve_blob_oid`` → :meth:`blob_size` so ONLY a
        genuinely-absent path at a resolvable commit collapses to None:
        the typed resolve returns None when the path is absent in the
        tree, but PROPAGATES a corrupt / missing commit or tree; and
        ``blob_size`` sizes the EXACT resolved oid — a real failure sizing an oid
        that DID resolve propagates as :class:`ExternalGitCommandError` rather
        than being masked as "not present", which the diff caller would otherwise
        read as "not oversized, proceed to materialize"."""
        oid = self.resolve_blob_oid(bare_path, rev, path)
        if oid is None:
            return None
        return self.blob_size(bare_path, oid)

    def last_commit_for_path(
        self, bare_path: str | os.PathLike[str], path: str, rev: str | None = None
    ) -> str | None:
        """Hex SHA of the most recent commit touching ``path`` at ``rev`` (a
        materialized SHA), or None. ``path`` is a literal pathspec
        (GIT_LITERAL_PATHSPECS=1), so upstream names with pathspec-magic
        characters cannot be reinterpreted."""
        rev = rev or "HEAD"
        if rev != "HEAD":
            _require_oid(rev, what="rev")
        try:
            out = self._local(
                bare_path,
                ["log", "-1", "--format=%H", rev, "--", path],
            )
        except ExternalGitCommandError:
            return None
        assert isinstance(out, str)
        out = out.strip()
        return out or None

    def update_ref(
        self, bare_path: str | os.PathLike[str], ref: str, new_value: str
    ) -> None:
        _safe_ref(ref, what="ref")
        _safe_ref(new_value, what="ref value")
        self._local(bare_path, ["update-ref", "--end-of-options", ref, new_value])

    def delete_ref_quiet(self, bare_path: str | os.PathLike[str], ref: str) -> None:
        # Best-effort; a leftover tmp ref is harmless.
        _safe_ref(ref, what="ref")
        self._local(
            bare_path, ["update-ref", "-d", "--end-of-options", ref], check=False
        )

    # ── Hermetic mirror READ paths ──
    # A mirror bare repo can carry a planted promisor/partial-clone config or a
    # repo-local rewrite that GitPython (which inherits os.environ and reads
    # repo config) would honor — re-opening lazy-fetch on a plain public
    # read. These route the reader paths (document GET / history / diff) through
    # the same sealed env + GIT_NO_LAZY_FETCH / GIT_NO_REPLACE_OBJECTS /
    # GIT_LITERAL_PATHSPECS as the network sinks, so a public read performs ZERO
    # outbound or lazy-fetch process.
    def cat_path(
        self,
        bare_path: str | os.PathLike[str],
        rev: str,
        path: str,
        *,
        max_output_bytes: int | None = None,
    ) -> bytes | None:
        """Raw bytes of ``<rev>:<path>`` (a file's content at a commit), or None
        if the rev or path is absent. ``rev`` is a materialized 40-hex OID or
        ``HEAD``. ``<rev>:<path>`` is object-name syntax: a single operand whose
        ``:`` prefix means ``path`` is never parsed as an option and pathspec
        magic never applies. ``max_output_bytes`` streams the read with a hard
        cap so an over-cap blob is aborted mid-read
        rather than buffered whole."""
        if rev != "HEAD":
            _require_oid(rev, what="rev")
        try:
            out = self._local(
                bare_path,
                ["cat-file", "blob", "--end-of-options", f"{rev}:{path}"],
                want_bytes=True,
                max_output_bytes=max_output_bytes,
            )
        except ExternalGitCommandError:
            return None  # unknown rev or path missing at that rev
        assert isinstance(out, bytes)
        return out

    def log_for_path(
        self, bare_path: str | os.PathLike[str], path: str, max_count: int
    ) -> list[dict]:
        """Commits touching ``path`` (newest first), each as
        ``{hash, author, committed_epoch, message}``. Empty on any error (no
        HEAD, unknown path). ``path`` is a literal pathspec after ``--``."""
        fmt = f"%H{_US}%an{_US}%ct{_US}%B{_RS}"
        try:
            out = self._local(
                bare_path,
                ["log", f"--max-count={int(max_count)}", f"--format={fmt}", "--", path],
            )
        except ExternalGitCommandError:
            return []
        assert isinstance(out, str)
        entries: list[dict] = []
        for record in out.split(_RS):
            rec = record.strip("\n")
            if not rec.strip():
                continue
            parts = rec.split(_US)
            if len(parts) < 4:
                continue
            sha, author, ct, message = parts[0], parts[1], parts[2], parts[3]
            if not _OID_RE.fullmatch(sha.strip()):
                continue
            entries.append(
                {
                    "hash": sha.strip(),
                    "author": author,
                    "committed_epoch": _safe_int(ct),
                    "message": message,
                }
            )
        return entries

    def vault_log_entries(
        self,
        bare_path: str | os.PathLike[str],
        *,
        max_count: int,
        since: str | None = None,
        path: str | None = None,
    ) -> list[dict]:
        """Vault commit log (newest first) with per-commit changed files, each as
        ``{hash, author, committed_epoch, subject, body, files:[{path,change}]}``.
        Empty on any error. ``since`` is a git date operand (bound to ``--since=``
        so it is never option-parsed); ``path`` is a literal pathspec."""
        fmt = f"{_RS}%H{_US}%an{_US}%ct{_US}%s{_US}%b{_RS}"
        args = [
            "log",
            f"--max-count={int(max_count)}",
            "--root",
            "--name-status",
            f"--format={fmt}",
        ]
        if since:
            args.append(f"--since={since}")
        args.append("--")
        if path:
            args.append(path)
        try:
            out = self._local(bare_path, args)
        except ExternalGitCommandError:
            return []
        assert isinstance(out, str)
        return _parse_vault_log(out)

    def file_diff_entry(
        self,
        bare_path: str | os.PathLike[str],
        commit: str,
        path: str,
        *,
        max_output_bytes: int | None = None,
    ) -> dict:
        """Single-file diff at ``commit`` → ``{type, diff}`` where type ∈
        added/deleted/modified/unchanged/unknown. Accepts an abbreviated hex
        commit (resolved via hermetic rev-parse). ``path`` is a literal pathspec.

        ``max_output_bytes`` bounds BOTH the root-commit full-content addition
        (``cat_path``) and the ``diff-tree -p`` patch (streaming
        backstop): a commit that shrinks or deletes a large file has a
        large PRE-image that ``diff-tree -p`` would otherwise buffer whole, so
        the streaming cap aborts it. The caller (``git_service``) also size-checks
        the pre/post-image blobs up front, so this is the defence-in-depth
        backstop for anything the per-blob check cannot bound (e.g. a
        many-small-hunk patch)."""
        if not isinstance(commit, str) or not _HEX_ABBREV_RE.fullmatch(commit):
            return {"type": "unknown", "diff": ""}
        try:
            full = self.rev_parse(bare_path, f"{commit}^{{commit}}")
        except ExternalGitCommandError:
            return {"type": "unknown", "diff": ""}
        try:
            parents = self._local(
                bare_path,
                ["rev-list", "--parents", "-n", "1", "--end-of-options", full],
            )
        except ExternalGitCommandError:
            return {"type": "unknown", "diff": ""}
        assert isinstance(parents, str)
        is_root = len(parents.split()) <= 1
        if is_root:
            content = self.cat_path(
                bare_path, full, path, max_output_bytes=max_output_bytes
            )
            if content is None:
                return {"type": "unknown", "diff": ""}
            text = content.decode("utf-8", "replace")
            return {
                "type": "added",
                "diff": "\n".join(f"+{line}" for line in text.split("\n")),
            }
        status = self._local(
            bare_path,
            [
                "diff-tree",
                "-r",
                "--no-commit-id",
                "--name-status",
                "--end-of-options",
                full,
                "--",
                path,
            ],
        )
        assert isinstance(status, str)
        change_type = "unchanged"
        first = status.strip().split("\t", 1)[0].strip() if status.strip() else ""
        if first[:1] == "A":
            change_type = "added"
        elif first[:1] == "D":
            change_type = "deleted"
        elif first:
            change_type = "modified"
        if change_type == "unchanged":
            return {"type": "unchanged", "diff": ""}
        patch = self._local(
            bare_path,
            ["diff-tree", "-p", "--no-commit-id", "--end-of-options", full, "--", path],
            max_output_bytes=max_output_bytes,
        )
        assert isinstance(patch, str)
        return {"type": change_type, "diff": patch}

    # ── Repo integrity (strict structure default-deny) ───
    def inspect_structure(
        self,
        bare_path: str | os.PathLike[str],
        remote_url: str | None = None,
        branch: str | None = None,
    ) -> list[str]:
        """Return a list of VALUE-LESS structural findings that make the bare
        repo untrusted for a network fetch (empty list = clean).

        Reads on-disk state directly (never ``git config``, which would itself
        expand ``include.*``). The config is parsed with a strict DEFAULT-DENY
        allowlist: only the exact sections/keys/values a fresh
        single-branch bare clone writes are permitted; everything else is a
        finding. ``branch`` lets the origin fetch refspec be validated exactly.
        On any finding the caller does a sterile re-clone rather than fetching
        into a suspicious repo. The runtime ``GIT_NO_REPLACE_OBJECTS`` /
        ``GIT_NO_LAZY_FETCH`` env vars and the belt ``-c core.hooksPath`` are the
        backstops for anything this misses.
        """
        bare = Path(bare_path)
        findings: list[str] = []
        if not bare.exists():
            return findings

        expected_url: str | None = None
        if remote_url:
            try:
                expected_url = parse_and_canonicalize(
                    remote_url, settings=self._settings_now()
                ).canonical_url
            except ExternalGitPolicyError:
                expected_url = None  # unparseable → any remote url is a finding

        cfg = bare / "config"
        cfg_finding = _regular_file_finding(cfg, what="config")
        if cfg_finding is not None:
            # A symlink / non-regular / oversized config: its shape is already a
            # finding and its bytes are not trustworthy to parse, so stop here.
            findings.append(cfg_finding)
        else:
            # A clean regular file OR no config file at all. An ABSENT config is
            # read as "" so ``_inspect_config_text`` flags it as `config is empty
            # or absent` — a bare repo always has a populated config, so its
            # absence (like a blank one) is a finding, not clean. This
            # closes the "no config file ⇒ default-allow" hole.
            try:
                text = cfg.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                text = ""
            except OSError:
                findings.append("config unreadable")
                text = None
            if text is not None:
                findings += _inspect_config_text(
                    text, expected_url=expected_url, branch=branch
                )

        # Files that redirect the object graph / worktree / transport. ANY
        # presence — regular file OR symlink — is a finding.
        for rel, label in _SPECIAL_FILES:
            p = bare / rel
            if p.is_symlink() or p.exists():
                findings.append(label)

        replace_dir = bare / "refs" / "replace"
        try:
            if replace_dir.is_dir() and any(replace_dir.iterdir()):
                findings.append("refs/replace entries present")
        except OSError:
            pass

        packed = bare / "packed-refs"
        if packed.exists():
            try:
                for line in packed.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines():
                    if "refs/replace/" in line:
                        findings.append("packed refs/replace entry present")
                        break
            except OSError:
                pass

        return findings


def _regular_file_finding(path: Path, *, what: str) -> str | None:
    """Return a finding if ``path`` exists but is NOT a size-capped regular
    non-symlink file; None if it is a clean regular file or is simply absent.
    ``lstat`` (not ``stat``) so a symlink is detected rather than followed."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return f"{what} is unreadable"
    if stat.S_ISLNK(st.st_mode):
        return f"{what} is a symlink (not a regular file)"
    if not stat.S_ISREG(st.st_mode):
        return f"{what} is not a regular file"
    if st.st_size > _MAX_CONFIG_BYTES:
        return f"{what} exceeds the size cap"
    return None


def _config_value(raw: str) -> str:
    """The effective value of a git-config line: strip an inline (unquoted)
    ``#``/``;`` comment, then surrounding double-quotes. Used only to compare
    ``remote.origin.url`` / ``fetch`` against their expected literals — every
    other key is denied by NAME, so its value is never inspected."""
    s = raw.strip()
    out: list[str] = []
    in_q = False
    prev = ""
    for c in s:
        if c == '"' and prev != "\\":
            in_q = not in_q
            out.append(c)
        elif c in ("#", ";") and not in_q:
            break
        else:
            out.append(c)
        prev = c
    v = "".join(out).strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return v


def _git_bool(has_eq: bool, value_raw: str) -> bool | None:
    """Interpret a git-config boolean VALUE, or None if it is not a boolean.

    A key with no ``=`` (``has_eq`` False, e.g. a lone ``bare`` line) is
    git-true. Otherwise the comment/quote-stripped value is matched against
    git's boolean spellings, case-insensitively. Used only to VALUE-check the
    handful of boolean core keys a fresh clone writes — the value itself is
    never echoed into a finding."""
    if not has_eq:
        return True
    v = _config_value(value_raw).lower()
    if v in _GIT_BOOL_TRUE:
        return True
    if v in _GIT_BOOL_FALSE:
        return False
    return None


def _parse_section_header(line: str) -> tuple[str, str | None] | None:
    """Parse a ``[...]`` header into ``(section, subsection)`` (both lowercased),
    or None if it is not a well-formed header. Handles modern
    ``[section "sub"]`` and legacy dotted ``[section.sub]`` (git treats them
    identically). An unparseable header returns None → the caller fails closed."""
    m = _CONFIG_SECTION_RE.match(line)
    if not m:
        return None
    raw_section = m.group(1)
    quoted_sub = m.group(2)
    if quoted_sub is not None:
        return raw_section.lower(), quoted_sub.lower()
    # Legacy: subsection is everything after the first dot (case-insensitive).
    head, dot, tail = raw_section.partition(".")
    return head.lower(), (tail.lower() if dot else None)


def _finding_for(section: str | None, sub: str | None) -> str:
    """A VALUE-LESS, FIXED-vocabulary descriptor for a denied config entry.
    It is a function of the SECTION alone — never of the
    caller-controlled key name, subsection, or value, any of which can carry a
    URL or token. The caller (``external_git_service``) logs/persists only these
    fixed strings, so a secret planted in a config key or subsection can never
    leak through a finding. The per-key / per-value distinctions that DO matter
    (a core value, an origin url/fetch mismatch, an objectFormat mismatch, a
    missing origin) are emitted inline by :func:`_inspect_config_text` as their
    own fixed strings."""
    if section == "remote":
        # Reached only for a NON-origin remote; origin findings are inline.
        return "unexpected remote section in config"
    if section == "http":
        if sub is not None:
            return "url-scoped http override in config"
        return "unexpected http key in config"
    if section == "core":
        return "unexpected core key in config"
    if section == "extensions":
        return "unexpected extensions key in config"
    if section in ("include", "includeif"):
        return "config include directive present"
    if section == "credential":
        return "credential section in config"
    if section == "protocol":
        return "protocol override in config"
    if section == "url":
        return "url insteadOf rewrite in config"
    if section == "branch":
        return "unexpected branch section in config"
    return "disallowed config entry"


def _inspect_config_text(
    text: str,
    *,
    expected_url: str | None,
    branch: str | None,
    expected_object_format: str = _EXPECTED_OBJECT_FORMAT,
) -> list[str]:
    """DEFAULT-DENY scan of raw ``.git/config``.

    ALLOWS ONLY the exact sections / keys / VALUES a fresh ``git clone --bare
    --single-branch --no-tags`` writes (verified empirically on git 2.37–2.51):

    * ``[core]`` — ``repositoryformatversion = 0`` and ``bare = <true>`` with
      their EXACT values, plus the platform booleans (filemode / ignorecase /
      precomposeunicode / symlinks) whose value must parse as a git boolean. Any
      other core key, or a wrong value on these (``bare = false`` on a bare repo,
      ``repositoryformatversion = 1``), is a finding. Both required core keys
      must be PRESENT: their absence (an emptied ``[core]``, or no config at all)
      is ``missing core.repositoryformatversion`` / ``missing core.bare`` — a
      missing required key can no longer pass as clean.
    * ``[extensions]`` — ``objectFormat`` only, and only equal to the expected
      format (SHA-1); a clean SHA-1 clone writes no extensions section, so
      ``sha256`` / ``worktreeConfig`` / anything else is a finding.
    * ``[remote "origin"]`` — ``url`` matching the canonical URL and REQUIRED
      exactly once (absence ⇒ ``missing remote.origin.url`` when a canonical URL
      is known; a second url ⇒ ``duplicate remote.origin.url``, since
      ``remote.<name>.url`` is multi-valued and a planted second entry could
      redirect the fetch), an OPTIONAL ``fetch`` matching the single bare
      single-branch refspec (zero or one; a duplicate is a finding), and
      ``tagOpt = --no-tags``. A renamed/extra remote or any other origin key is a
      finding.

    A blank or ABSENT config (read as ``""``) is a single ``config is empty or
    absent`` finding — a bare repo always has a populated config, so an
    empty/missing one is never clean.

    Everything else — a rewritten ``url.*.insteadOf``, an ``include``, a
    ``credential`` / ``http`` / ``proxy`` override, a command-bearing ``core``
    key, a ``[branch]`` section (a bare clone writes none), an unknown section —
    is a finding. ``include`` is NEVER expanded (this reads raw text, not
    ``git config``). Findings are de-duplicated and strictly VALUE-LESS: never
    the caller-controlled key, subsection, or value.
    """
    findings: list[str] = []
    seen: set[str] = set()

    def add(f: str) -> None:
        if f not in seen:
            seen.add(f)
            findings.append(f)

    # REQUIRED structure, part 1: a real bare-clone config is NEVER
    # empty — it always carries `[core]` with repositoryformatversion + bare. A
    # blank or absent config file (read here as ""/whitespace) has none of the
    # required keys, so it is a finding in its own right rather than clean. Emit
    # a single descriptor instead of piling on every per-key "missing" below.
    if not text.strip():
        return ["config is empty or absent"]

    # The ONLY fetch refspec a bare single-branch clone would persist is the bare
    # mapping narrowed to the one branch. The ``refs/remotes/origin/*`` forms
    # belong to NON-bare clones (verified empirically) and are intentionally NOT
    # accepted here (refspec narrowing). In practice our bare clone writes
    # no fetch key at all, so this only gates a planted/synthetic one.
    expected_fetch = {f"+refs/heads/{branch}:refs/heads/{branch}"} if branch else None

    # Required-key presence + occurrence tracking. A fresh bare
    # single-branch clone writes repositoryformatversion + bare exactly once and
    # origin.url exactly once; their ABSENCE (or a duplicated url/fetch) is a
    # finding, closing the "missing required key ⇒ default-allow" hole.
    saw_repositoryformatversion = False
    saw_bare = False
    origin_url_count = 0
    origin_fetch_count = 0
    section: str | None = None
    sub: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line[0] in (";", "#"):
            continue
        if line.startswith("["):
            parsed = _parse_section_header(line)
            if parsed is None:
                add("unparseable config section header")
                section, sub = "\x00invalid", None  # deny every key under it
                continue
            section, sub = parsed
            # A whole-section-suspicious header (non-origin remote, url rewrite,
            # include, credential, protocol, branch) is flagged at the header so
            # a KEYLESS suspicious section is still caught.
            if section == "remote" and sub != "origin":
                add("unexpected remote section in config")
            elif section in (
                "url", "include", "includeif", "credential", "protocol", "branch"
            ):
                add(_finding_for(section, sub))
            continue

        # key line — VALUE-LESS by default; only the handful of keys below have
        # their VALUE inspected (core exacts, objectFormat, origin url/fetch).
        name_raw, eq, value_raw = line.partition("=")
        has_eq = eq == "="
        name = name_raw.strip().lower()
        if not name:
            add("unparseable config line")
            continue

        if section == "core":
            if name == "repositoryformatversion":
                saw_repositoryformatversion = True
                if _config_value(value_raw) != "0":
                    add("core.repositoryformatversion is not 0 in config")
            elif name == "bare":
                saw_bare = True
                if _git_bool(has_eq, value_raw) is not True:
                    add("core.bare is not true in config")
            elif name in _CORE_BOOL_KEYS:
                if _git_bool(has_eq, value_raw) is None:
                    add("core key has a non-boolean value in config")
            else:
                add(_finding_for("core", sub))
        elif section == "extensions":
            if name == "objectformat":
                if _config_value(value_raw).lower() != expected_object_format:
                    add("extensions.objectFormat does not match the expected format")
            elif name == "worktreeconfig":
                add("extensions.worktreeConfig enabled in config")
            else:
                add(_finding_for("extensions", sub))
        elif section == "remote" and sub == "origin":
            if name == "url":
                # Counted even when mismatched: the mismatch finding covers the
                # bad value; the count drives the missing (0) / duplicate (>1)
                # checks after the sweep. A fresh clone writes EXACTLY one.
                origin_url_count += 1
                if expected_url is None or _config_value(value_raw) != expected_url:
                    add("remote.origin.url does not match the canonical URL")
            elif name == "fetch":
                origin_fetch_count += 1
                if (
                    expected_fetch is None
                    or _config_value(value_raw) not in expected_fetch
                ):
                    add(
                        "remote.origin fetch refspec does not match the "
                        "expected branch"
                    )
            elif name == "tagopt":
                # `git clone --no-tags` (our own clone argv) records
                # remote.origin.tagOpt = --no-tags. Allow exactly that; any other
                # value is a finding.
                if _config_value(value_raw) != "--no-tags":
                    add("unexpected remote.origin key in config")
            else:
                add("unexpected remote.origin key in config")
        elif section == "branch":
            # A bare clone writes NO `[branch]` section, so any branch.* key —
            # e.g. an arbitrary branch pointing remote/merge off-canonical
            # (Finding #2) — is a finding. The header flag covers the keyless
            # case; this covers keyed lines.
            add(_finding_for("branch", sub))
        elif section in (
            "remote", "http", "url", "include", "includeif", "credential", "protocol"
        ):
            # Denied sections: a VALUE-LESS per-key descriptor (the header flag
            # above already covers keyless cases).
            add(_finding_for(section, sub))
        else:
            # Unknown section (or a key before any header) — default-deny.
            add("disallowed config entry")

    # REQUIRED structure, part 2. A fresh bare single-branch clone
    # ALWAYS writes core.repositoryformatversion=0 and core.bare=true (verified
    # empirically, git 2.37–2.51). Their ABSENCE means the config is not the
    # shape a clean clone produces — a finding, so a missing key can no longer
    # slip through as clean. (An entirely absent `[core]` surfaces as BOTH of
    # these, which subsumes a "missing core section" descriptor.)
    if not saw_repositoryformatversion:
        add("missing core.repositoryformatversion in config")
    if not saw_bare:
        add("missing core.bare in config")
    # The canonical pin must be present EXACTLY once. Absence = no origin to pin
    # the fetch against (only meaningful when we have a canonical URL to compare,
    # unchanged from the prior contract); more than one = an ambiguous/overriding
    # second url (git treats remote.<name>.url as multi-valued, so a planted
    # second entry could redirect the fetch). A fresh clone writes exactly one.
    if origin_url_count == 0:
        if expected_url is not None:
            add("missing remote.origin.url in config")
    elif origin_url_count > 1:
        add("duplicate remote.origin.url in config")
    # A bare single-branch clone writes zero or one fetch refspec; more than one
    # is a planted/ambiguous refspec (an extra, wider mapping could survive).
    if origin_fetch_count > 1:
        add("duplicate remote.origin fetch refspec in config")
    return findings


def _safe_int(value: str) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return 0


def _parse_vault_log(out: str) -> list[dict]:
    """Parse ``git log --name-status --format=<RS-bracketed>`` output (NUL-free)
    into structured commits. Each commit's format block is bracketed by RS
    (0x1e), so splitting the stream on RS yields alternating
    ``[fields, name-status, fields, name-status, ...]`` (index 0 is the empty
    pre-first-RS remainder)."""
    chunks = out.split(_RS)
    body = chunks[1:]
    entries: list[dict] = []
    for i in range(0, len(body) - 1, 2):
        parts = body[i].split(_US)
        if len(parts) < 5:
            continue
        sha, author, ct, subject, body_text = parts[:5]
        if not _OID_RE.fullmatch(sha.strip()):
            continue
        files: list[dict] = []
        for line in body[i + 1].splitlines():
            line = line.strip()
            if not line:
                continue
            cols = line.split("\t")
            code = cols[0].strip()
            # `R100\told\tnew` / `C100\told\tnew` → destination path is last.
            target = cols[-1] if len(cols) > 1 else ""
            if not target or target.startswith("."):
                continue
            if code[:1] == "A":
                change = "added"
            elif code[:1] == "D":
                change = "deleted"
            else:
                change = "modified"
            files.append({"path": target, "change": change})
        entries.append(
            {
                "hash": sha.strip(),
                "author": author,
                "committed_epoch": _safe_int(ct),
                "subject": subject,
                "body": body_text,
                "files": files,
            }
        )
    return entries


def _sanitize(
    text: bytes | str | None,
    *,
    url: str | None,
    cred: str | None,
    token: str | None = None,
) -> str:
    """Strip the canonical URL, the raw auth token, the credential header, and
    any bare ``Basic <base64>`` from git stderr, then truncate.

    ``token`` is passed as well as ``cred`` because the design mandates removing
    the raw token itself: git never prints it in the clear (it lives inside the
    base64 header, and GIT_TRACE_REDACT=1 redacts even that), but a defence-in-
    depth sanitizer must not depend on that assumption. The generated
    ``Basic <base64>`` form is stripped too because a remote can reflect
    the Authorization header back in an error body."""
    if not text:
        return ""
    s = text.decode("utf-8", "replace") if isinstance(text, (bytes, bytearray)) else text
    if url:
        s = s.replace(url, "<remote>")
    if token:
        # Strip the raw token first, then its base64 rendering (both the
        # user:token pair and the bare token), so no fragment survives.
        s = s.replace(token, "<redacted>")
        b64_pair = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        s = s.replace(b64_pair, "<redacted>")
    if cred:
        s = s.replace(cred, "<redacted>")
        # cred is "Authorization: Basic <b64>"; also strip the bare b64.
        b64 = cred.split(" ", 2)[-1]
        if b64:
            s = s.replace(b64, "<redacted>")
    s = re.sub(r"([Bb]asic)\s+[A-Za-z0-9+/=]{8,}", r"\1 <redacted>", s)
    # Strip userinfo from ANY scheme://user[:pass]@host URL, not only the known
    # canonical one: a remote can echo back a URL
    # carrying embedded credentials that is not byte-identical to our canonical
    # form (e.g. a redirect target, or a different-cased/encoded host). The
    # authority run is matched GREEDILY up to the LAST `@` STILL INSIDE the
    # authority (``[^/\s?#]+`` excludes `/`, whitespace, and the ``?``/``#`` that
    # begin the query/fragment) so the match cannot cross into the path, query,
    # or a following URL. This keeps two properties at once: a multi-`@`
    # authority (`user@a@host`, crafted so a first-`@`-only strip would leave
    # `a@host`) is still redacted in FULL, while a benign query/fragment `@`
    # (`host?email=a@b`, `host#x@y`) no longer drags the real host into the
    # redaction (over-redaction of diagnostics, not a secret leak).
    s = re.sub(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s?#]+@", r"\1<redacted>@", s)
    s = s.replace("x-access-token", "<user>")
    return s.strip()[:500]
