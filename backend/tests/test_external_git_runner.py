"""Tests for the hermetic ``ExternalGitRunner`` execution boundary.

Three layers:

1. **Pure unit** — env construction, global-arg ordering, DNS-pin formatting,
   ls-remote parsing (exact ref + exact OID). No git, no network.
2. **Argv capture** — clone/fetch/ls-remote argv is built with the credential
   as a header (never in argv/URL) and the URL only after ``--``. ``_exec`` is
   stubbed so no process runs.
3. **Real git** — against the actual ``git`` binary: an ``ext::``/``file://``
   transport is blocked by the sealed env; a real clone/fetch over the
   in-process smart-HTTP fixture proves DNS-pin + credential-header +
   ambient-env isolation end-to-end; the structure default-deny flags every
   unexpected redirection artifact; and ``GIT_NO_REPLACE_OBJECTS`` neutralizes a
   planted replace object.
"""

from __future__ import annotations

import base64
import os
import selectors
import shutil
import subprocess
import time

import pytest

import app.services.external_git_runner as egr
from app.config import Settings
from app.services.external_git_runner import (
    ExternalGitCommandError,
    ExternalGitOutputCapError,
    ExternalGitRunner,
    _CRED_ENV,
    _OID_RE,
    _cred_header,
    _curlopt_resolve,
    _inspect_config_text,
    _sanitize,
)
from app.services.external_git_validation import (
    ExternalGitPolicyError,
    ExternalGitTransientError,
)
from tests.extgit_http import build_runner

GIT = shutil.which("git") or "git"

_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "T",
    "GIT_AUTHOR_EMAIL": "t@akb.local",
    "GIT_COMMITTER_NAME": "T",
    "GIT_COMMITTER_EMAIL": "t@akb.local",
}


def _init_bare(tmp_path, name: str = "m.git"):
    bare = tmp_path / name
    subprocess.run([GIT, "init", "--bare", str(bare)], check=True, capture_output=True)
    return bare


def _git_dir(bare, *args, want_input: str | None = None) -> str:
    env = dict(os.environ)
    env.update(_COMMIT_ENV)
    return subprocess.run(
        [GIT, f"--git-dir={bare}", *args],
        check=True,
        capture_output=True,
        text=True,
        input=want_input,
        env=env,
    ).stdout.strip()


def _make_commit(bare, path: str, content: str) -> str:
    """Create a commit (blob→tree→commit) directly in a bare repo. Returns the
    commit SHA. No worktree needed."""
    blob = _git_dir(bare, "hash-object", "-w", "--stdin", want_input=content)
    tree = _git_dir(bare, "mktree", want_input=f"100644 blob {blob}\t{path}\n")
    return _git_dir(bare, "commit-tree", tree, "-m", "c")


# ══ 1. Pure unit: hermetic env ═══════════════════════════════════════
def test_base_env_is_built_from_scratch_and_seals_ambient(monkeypatch):
    # Plant unwanted ambient vars that MUST NOT reach the child.
    ambient_vars = {
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "HTTP_PROXY": "http://127.0.0.1:1",
        "ALL_PROXY": "http://127.0.0.1:1",
        "NETRC": "/tmp/evil-netrc",
        "GIT_ASKPASS": "/tmp/evil-askpass",
        "SSH_ASKPASS": "/tmp/evil-askpass",
        "GIT_EXEC_PATH": "/tmp/evil-exec",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/tmp/evil-objs",
        "GIT_OBJECT_DIRECTORY": "/tmp/evil-objs",
        "GIT_COMMON_DIR": "/tmp/evil-common",
        "GIT_CURL_VERBOSE": "1",
        "GIT_TRACE": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "protocol.ext.allow",
        "GIT_CONFIG_VALUE_0": "always",
    }
    for k, v in ambient_vars.items():
        monkeypatch.setenv(k, v)

    env = ExternalGitRunner(settings=Settings(external_git_allow_http=False))._base_env()

    # Forbidden vars are simply absent (built from scratch, not copied).
    for k in (
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NETRC", "GIT_ASKPASS",
        "SSH_ASKPASS", "GIT_EXEC_PATH", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_CURL_VERBOSE",
        "GIT_TRACE", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
    ):
        assert k not in env, k

    # Our fixed values win over the ambient ones.
    assert env["GIT_CONFIG_COUNT"] == "0"  # not the ambient "1"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert env["GIT_LITERAL_PATHSPECS"] == "1"
    assert env["GIT_TRACE_REDACT"] == "1"
    # HOME / XDG point at an EMPTY sealed dir → no ambient .netrc / .gitconfig.
    assert env["HOME"] == env["XDG_CONFIG_HOME"]
    assert os.listdir(env["HOME"]) == []


def test_base_env_allows_http_only_when_opted_in():
    on = ExternalGitRunner(settings=Settings(external_git_allow_http=True))._base_env()
    off = ExternalGitRunner(settings=Settings(external_git_allow_http=False))._base_env()
    assert on["GIT_ALLOW_PROTOCOL"] == "http:https"
    assert off["GIT_ALLOW_PROTOCOL"] == "https"


# ══ 1. Pure unit: global-arg ordering + DNS pin ══════════════════════
def test_net_global_args_reset_precedes_credential_and_pins_dns():
    r = ExternalGitRunner(settings=Settings(external_git_allow_http=False))
    url = "https://h.example/x.git"
    args = r._net_global_args(
        url, "h.example", 443, ("1.2.3.4", "2606:4700::1111"), has_cred=True
    )
    reset = f"http.{url}.extraHeader="
    cfgenv = f"--config-env=http.{url}.extraHeader={_CRED_ENV}"
    # The empty-value reset MUST come before the --config-env set, else the set
    # header is wiped from the multi-valued list.
    assert reset in args and cfgenv in args
    assert args.index(reset) < args.index(cfgenv)
    # Default-deny protocol posture; http NOT opted in here.
    assert "protocol.allow=never" in args
    assert "protocol.https.allow=always" in args
    assert "protocol.http.allow=always" not in args
    # url-scoped proxy off / no redirects / TLS verify on / no submodules.
    assert f"http.{url}.proxy=" in args
    assert f"http.{url}.followRedirects=false" in args
    assert "http.sslVerify=true" in args
    assert "fetch.recurseSubmodules=no" in args
    # DNS pin: IPv6 bracketed in the address list.
    assert "http.curloptResolve=h.example:443:1.2.3.4,[2606:4700::1111]" in args


def test_net_global_args_http_opt_in_adds_http_allow():
    r = ExternalGitRunner(settings=Settings(external_git_allow_http=True))
    args = r._net_global_args(
        "http://h/x.git", "h", 80, ("1.2.3.4",), has_cred=False
    )
    assert "protocol.http.allow=always" in args


def test_curlopt_resolve_skips_ip_literal_hosts():
    # A DNS name is pinned; an IP-literal host needs no pin (already fixed).
    assert _curlopt_resolve("h.example", 443, ("1.2.3.4",)) == "h.example:443:1.2.3.4"
    assert _curlopt_resolve("93.184.216.34", 443, ("93.184.216.34",)) is None
    assert _curlopt_resolve("2606:4700::1111", 443, ("2606:4700::1111",)) is None


# ══ 1. Pure unit: ls-remote parsing ══════════════════════════════════
def test_ls_remote_uses_exact_ref_and_validates_oid(monkeypatch):
    r = build_runner(5000)
    good = "a" * 40
    # A decoy line whose ref ENDSWITH the target must be ignored (exact match),
    # and only a 40-hex OID is accepted.
    out = (
        f"b{'0' * 39}\tprefix/refs/heads/main\n"  # endswith decoy → ignored
        f"{good}\trefs/heads/main\n"
    )
    monkeypatch.setattr(r, "_exec", lambda *a, **k: out)
    assert r.ls_remote_sha("http://mirror.test:5000/x.git", "main", None) == good

    monkeypatch.setattr(r, "_exec", lambda *a, **k: "not-a-hex-oid\trefs/heads/main\n")
    assert r.ls_remote_sha("http://mirror.test:5000/x.git", "main", None) is None

    monkeypatch.setattr(r, "_exec", lambda *a, **k: "")
    assert r.ls_remote_sha("http://mirror.test:5000/x.git", "main", None) is None


# ══ 2. Argv capture: credential as header, URL after -- ══════════════
def _capture(monkeypatch, runner):
    calls: list[dict] = []

    def fake_exec(args, **kw):
        calls.append({"args": list(args), **kw})
        return ""

    monkeypatch.setattr(runner, "_exec", fake_exec)
    return calls


def test_clone_argv_has_no_token_url_after_dashdash_cred_via_env(monkeypatch):
    r = build_runner(5000)  # allow_http, mirror.test:5000 allowlisted
    calls = _capture(monkeypatch, r)
    token = "TOKEN-SEKRIT-abc123"
    r.clone_bare("http://mirror.test:5000/x.git", "main", token, "/tmp/does-not-matter.git")

    clone = next(c for c in calls if "clone" in c["args"])
    args = clone["args"]
    joined = " ".join(args)
    assert token not in joined  # token NEVER in argv
    assert "--" in args
    assert args[args.index("--") + 1] == "http://mirror.test:5000/x.git"
    # global -c / --config-env all precede the subcommand.
    assert args.index("clone") > max(
        i for i, a in enumerate(args) if a == "-c" or a.startswith("--config-env=")
    )
    assert any(
        a == f"--config-env=http.http://mirror.test:5000/x.git.extraHeader={_CRED_ENV}"
        for a in args
    )
    # The token rides in as an Authorization header value via cred_header
    # (which _exec places in the child env, never in argv).
    expect = "Authorization: Basic " + base64.b64encode(
        f"x-access-token:{token}".encode()
    ).decode()
    assert clone["cred_header"] == expect
    assert token not in str(clone.get("redact_url"))


def test_fetch_argv_url_after_dashdash_and_refspec(monkeypatch):
    r = build_runner(5000)
    calls = _capture(monkeypatch, r)
    r.fetch_to_ref(
        "/tmp/bare.git", "http://mirror.test:5000/x.git", "main", None,
        "refs/akb/fetch-tmp/main",
    )
    fetch = next(c for c in calls if "fetch" in c["args"])
    args = fetch["args"]
    assert args[0] == "--git-dir=/tmp/bare.git"
    assert "--" in args
    assert args[args.index("--") + 1] == "http://mirror.test:5000/x.git"
    assert "+refs/heads/main:refs/akb/fetch-tmp/main" in args


def test_ls_remote_argv_url_after_dashdash(monkeypatch):
    r = build_runner(5000)
    calls = _capture(monkeypatch, r)
    r.ls_remote_sha("http://mirror.test:5000/x.git", "main", None)
    lsr = next(c for c in calls if "ls-remote" in c["args"])
    args = lsr["args"]
    assert "--" in args
    assert args[args.index("--") + 1] == "http://mirror.test:5000/x.git"
    assert args[args.index("--") + 2] == "refs/heads/main"


# ══ 3. Real git: disallowed transport schemes are blocked ═══════════
def test_non_https_transport_scheme_rejected(tmp_path):
    """The sealed GIT_ALLOW_PROTOCOL=https must refuse a non-https
    transport and run nothing."""
    r = ExternalGitRunner(settings=Settings(external_git_allow_http=False))
    marker = tmp_path / "PWNED"
    with pytest.raises(ExternalGitCommandError) as exc:
        r._exec(
            ["ls-remote", "--", f"ext::sh -c touch{chr(32)}{marker}", "main"],
            cwd=str(tmp_path),
            timeout=30,
        )
    assert "not allowed" in str(exc.value)
    assert not marker.exists()  # the command never executed


def test_file_scheme_rejected(tmp_path):
    r = ExternalGitRunner(settings=Settings(external_git_allow_http=False))
    with pytest.raises(ExternalGitCommandError) as exc:
        r._exec(["ls-remote", "--", "file:///etc", "main"], cwd=str(tmp_path), timeout=30)
    assert "not allowed" in str(exc.value)


def test_network_methods_reject_disallowed_scheme_before_exec():
    # Layer-2 re-validation refuses non-https at the runner boundary too.
    r = build_runner(5000)
    for bad in ("ext::sh -c whoami", "file:///etc/passwd", "git://mirror.test:5000/x"):
        with pytest.raises(ExternalGitPolicyError):
            r.ls_remote_sha(bad, "main", None)


# ══ 3. Real git: clone / fetch / ls-remote over the fixture ══════════
def test_clone_over_pinned_fake_host_returns_materialized_sha(git_http, tmp_path):
    """A FAKE hostname (mirror.test) that exists only via curloptResolve is
    reachable — proving the DNS pin works end-to-end — and the returned SHA is
    the LOCAL materialized ref."""
    r = build_runner(git_http.port)
    url, head = git_http.add_repo("clone_ok", {"doc.md": "# Hi\n"})
    dest = tmp_path / "c.git"
    sha = r.clone_bare(url, "main", None, dest)
    assert sha == head
    assert r.ls_tree(dest, sha) == {"doc.md": r.ls_tree(dest, sha)["doc.md"]}
    assert "doc.md" in r.ls_tree(dest, sha)


def test_ls_remote_exact_and_missing_branch(git_http):
    r = build_runner(git_http.port)
    url, head = git_http.add_repo("lsr", {"a.md": "x"})
    assert r.ls_remote_sha(url, "main", None) == head
    assert r.ls_remote_sha(url, "does-not-exist", None) is None


def test_credential_header_reaches_server_and_never_touches_disk(git_http, tmp_path):
    r = build_runner(git_http.port)
    url, head = git_http.add_repo("cred", {"a.md": "x"})
    git_http.require_auth = True  # server 401s if the header is absent
    token = "gh-SECRET-token-9f8e7d"
    dest = tmp_path / "c.git"

    sha = r.clone_bare(url, "main", token, dest)  # succeeds ONLY if header sent
    assert sha == head

    expect = "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    assert git_http.last_auth == expect
    # Token / header absent from the persisted bare config.
    cfg = (dest / "config").read_text()
    assert token not in cfg
    assert expect.split(" ", 1)[1] not in cfg  # base64 form absent too


def test_ambient_proxy_and_config_are_ignored_by_child(git_http, tmp_path, monkeypatch):
    """An ambient proxy + a GIT_CONFIG_* override must not affect the
    child: the clone still succeeds via the pinned direct connection."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.proxy")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "http://127.0.0.1:1")

    r = build_runner(git_http.port)
    url, head = git_http.add_repo("amb", {"a.md": "x"})
    dest = tmp_path / "c.git"
    sha = r.clone_bare(url, "main", None, dest)  # would hit the dead proxy if leaked
    assert sha == head


def test_fetch_is_incremental_and_reads_local_ref(git_http, tmp_path):
    r = build_runner(git_http.port)
    url, head1 = git_http.add_repo("fetchme", {"a.md": "1"})
    dest = tmp_path / "c.git"
    assert r.clone_bare(url, "main", None, dest) == head1

    head2 = git_http.publish_change("fetchme", "b.md", "2")
    r.fetch_to_ref(dest, url, "main", None, "refs/akb/fetch-tmp/main")
    r.update_ref(dest, "refs/heads/main", "refs/akb/fetch-tmp/main")
    r.delete_ref_quiet(dest, "refs/akb/fetch-tmp/main")
    assert r.rev_parse(dest, "refs/heads/main") == head2
    assert set(r.ls_tree(dest, head2)) == {"a.md", "b.md"}


# ══ 3. Real git: structure default-deny (redirection artifacts) ═══
def _append_config(bare, text: str) -> None:
    cfg = bare / "config"
    cfg.write_text(cfg.read_text() + text)


def test_clean_clone_has_no_structure_findings(git_http, tmp_path):
    """No false positives: a freshly-cloned mirror is structurally clean, and
    remote.origin.url matches the canonical URL."""
    r = build_runner(git_http.port)
    url, head = git_http.add_repo("clean", {"a.md": "x"})
    dest = tmp_path / "c.git"
    r.clone_bare(url, "main", None, dest)
    assert r.inspect_structure(dest, url) == []


@pytest.mark.parametrize(
    "snippet,needle",
    [
        ('\n[remote "https://good.example/x.git"]\n\turl = ext::sh -c evil\n', "remote"),
        ('\n[url "https://evil.example/"]\n\tinsteadOf = https://good.example/\n', "insteadOf"),
        ('\n[includeIf "gitdir:/"]\n\tpath = /tmp/evil\n', "include"),
        ('\n[include]\n\tpath = /tmp/evil\n', "include"),
        ('\n[credential]\n\thelper = /tmp/evil.sh\n', "credential"),
        # value-less fixed enum: a rejected core/http key is described by section,
        # never by the (caller-controlled) key name.
        ('\n[core]\n\tsshCommand = /tmp/evil.sh\n', "core key"),
        ('\n[core]\n\thooksPath = /tmp/evilhooks\n', "core key"),
        ('\n[http]\n\tproxy = http://evil:8080\n', "http key"),
        ('\n[http "https://good.example/x.git"]\n\textraHeader = X: 1\n', "url-scoped http"),
        ('\n[http]\n\tsslVerify = false\n', "http key"),
        ('\n[protocol "ext"]\n\tallow = always\n', "protocol"),
        ('\n[extensions]\n\tworktreeConfig = true\n', "worktreeconfig"),
    ],
)
def test_structure_flags_config_redirection(tmp_path, snippet, needle):
    bare = _init_bare(tmp_path)
    _append_config(bare, snippet)
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git")
    assert any(needle.lower() in f.lower() for f in findings), findings


def test_structure_flags_origin_url_mismatch(tmp_path):
    bare = _init_bare(tmp_path)
    _append_config(bare, '\n[remote "origin"]\n\turl = https://attacker.example/x.git\n')
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git")
    assert any("origin" in f.lower() for f in findings), findings


def test_structure_origin_url_match_is_clean(tmp_path):
    bare = _init_bare(tmp_path)
    _append_config(bare, '\n[remote "origin"]\n\turl = https://good.example/x.git\n')
    r = ExternalGitRunner(settings=Settings())
    assert r.inspect_structure(bare, "https://good.example/x.git") == []


@pytest.mark.parametrize(
    "rel,body,needle",
    [
        ("config.worktree", "[core]\n", "worktree"),
        ("commondir", "../somewhere\n", "commondir"),
        ("shallow", "abc\n", "shallow"),
        ("info/grafts", "abc def\n", "grafts"),
        ("objects/info/alternates", "/some/other/objects\n", "alternates"),
    ],
)
def test_structure_flags_ondisk_redirection_files(tmp_path, rel, body, needle):
    bare = _init_bare(tmp_path)
    p = bare / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare)
    assert any(needle in f.lower() for f in findings), findings


def test_structure_flags_replace_ref(tmp_path):
    bare = _init_bare(tmp_path)
    c1 = _make_commit(bare, "a.md", "ORIGINAL\n")
    c2 = _make_commit(bare, "a.md", "EVIL\n")
    subprocess.run(
        [GIT, f"--git-dir={bare}", "replace", "-f", c1, c2],
        check=True, capture_output=True, env={**os.environ, **_COMMIT_ENV},
    )
    r = ExternalGitRunner(settings=Settings())
    assert any("replace" in f.lower() for f in r.inspect_structure(bare))


# ══ 3. Real git: GIT_NO_REPLACE_OBJECTS runtime backstop ═════════════
def test_git_no_replace_objects_neutralizes_object_substitution(tmp_path):
    """A replace ref that swaps a blob's content must have NO effect on the
    runner's reads (GIT_NO_REPLACE_OBJECTS=1), even though vanilla git honors
    it — this backstops any replace ref the structure check somehow misses."""
    bare = _init_bare(tmp_path)
    blob_a = _git_dir(bare, "hash-object", "-w", "--stdin", want_input="ORIGINAL\n")
    blob_b = _git_dir(bare, "hash-object", "-w", "--stdin", want_input="EVIL\n")
    subprocess.run(
        [GIT, f"--git-dir={bare}", "replace", "-f", blob_a, blob_b],
        check=True, capture_output=True, env={**os.environ, **_COMMIT_ENV},
    )
    # Vanilla git (no sealed env) honors the replacement → returns EVIL.
    vanilla = subprocess.run(
        [GIT, f"--git-dir={bare}", "cat-file", "blob", blob_a],
        check=True, capture_output=True, text=True,
    ).stdout
    assert vanilla == "EVIL\n"
    # The runner reads the ORIGINAL object — replacement is neutralized.
    r = ExternalGitRunner(settings=Settings())
    assert r.cat_blob(bare, blob_a) == b"ORIGINAL\n"


def test_blob_size_and_path_size_are_bounded_reads(tmp_path):
    """`blob_size` / `path_size` return the object's byte size via `cat-file -s`
    WITHOUT materializing content — the bounded primitive the oversized gate
    calls before a read. `path_size` is None ONLY for a path
    genuinely absent at a resolvable rev; an UNRESOLVABLE rev fails closed,
    never masked as absent."""
    bare = _init_bare(tmp_path)
    content = "0123456789\n" * 7  # 77 bytes
    nbytes = len(content.encode())
    commit = _make_commit(bare, "a.md", content)
    blob = _git_dir(bare, "rev-parse", f"{commit}:a.md")
    r = ExternalGitRunner(settings=Settings())

    assert r.blob_size(bare, blob) == nbytes
    assert r.path_size(bare, commit, "a.md") == nbytes  # <rev>:<path> sizing
    assert r.path_size(bare, commit, "missing.md") is None  # absent path → None
    # An unknown (valid-hex) rev no longer resolves to a commit → PROPAGATE, not
    # masked as absent (a corrupt object store is indistinguishable from this at
    # the rev level, so it must fail closed — MAJOR, fix-4).
    with pytest.raises(ExternalGitCommandError):
        r.path_size(bare, "0" * 40, "a.md")
    # A non-OID, non-HEAD rev is rejected (same guard as cat_path).
    with pytest.raises(ExternalGitPolicyError):
        r.path_size(bare, "notahex", "a.md")


# ══ 3. Real git: hooks do not execute ════════════════════════════════
def test_repo_local_hook_does_not_run(tmp_path):
    """A planted reference-transaction hook must NOT fire on the runner's
    update-ref: the belt `-c core.hooksPath=<sealed empty>` overrides any
    repo-local hooksPath."""
    bare = _init_bare(tmp_path)
    commit = _make_commit(bare, "a.md", "hi\n")
    hooks = bare / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = tmp_path / "HOOK_RAN"
    hook = hooks / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)

    # Sanity: a vanilla update-ref (hooksPath = repo default) DOES fire the hook.
    vmarker = tmp_path / "VANILLA_HOOK_RAN"
    hook.write_text(f"#!/bin/sh\ntouch '{vmarker}'\n")
    subprocess.run(
        [GIT, f"--git-dir={bare}", "update-ref", "refs/heads/vanilla", commit],
        check=True, capture_output=True, env={**os.environ, **_COMMIT_ENV},
    )
    assert vmarker.exists()  # confirms the hook is wired and would run

    # The runner's update-ref must NOT run it.
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    r = ExternalGitRunner(settings=Settings())
    r.update_ref(bare, "refs/heads/viarunner", commit)
    assert not marker.exists()


def test_update_ref_rejects_unsafe_ref_argument(tmp_path):
    """update_ref / delete_ref_quiet refuse a ref that could be parsed as an
    option or carry ref-magic — that confusion is structurally impossible, not
    merely reliant on --end-of-options."""
    bare = _init_bare(tmp_path)
    commit = _make_commit(bare, "a.md", "x\n")
    r = ExternalGitRunner(settings=Settings())
    for bad in ("--upload-pack=evil", "refs/heads/x~1", "refs/heads/x y", "-d"):
        with pytest.raises(ExternalGitPolicyError):
            r.update_ref(bare, bad, commit)
    with pytest.raises(ExternalGitPolicyError):
        r.delete_ref_quiet(bare, "--all")
    # A normal ref round-trips.
    r.update_ref(bare, "refs/heads/ok", commit)
    assert r.rev_parse(bare, "refs/heads/ok") == commit


# ══ Secondary-HTTP narrowing at the git-config layer ══
def test_net_args_reset_curlopt_and_bundleuri_before_pin():
    r = ExternalGitRunner(settings=Settings(external_git_allow_http=False))
    url = "https://h.example/x.git"
    args = r._net_global_args(url, "h.example", 443, ("1.2.3.4",), has_cred=False)
    # curloptResolve is multi-valued: the empty-value RESET must precede our pin
    # so an inherited/planted resolve entry cannot survive alongside ours.
    empty_idx = args.index("http.curloptResolve=")
    pin_idx = args.index("http.curloptResolve=h.example:443:1.2.3.4")
    assert empty_idx < pin_idx
    # bundle-URI reset + packfile-URI deny + redirect closures.
    assert "fetch.bundleURI=" in args
    assert "transfer.bundleURI=false" in args
    assert "fetch.uriProtocols=" in args
    # GLOBAL redirect-off (covers a redirect to a SECONDARY host), plus url-scoped.
    assert "http.followRedirects=false" in args
    assert f"http.{url}.followRedirects=false" in args


# ══ --no-tags on clone + fetch argv ═════════════════════════
def test_clone_and_fetch_argv_carry_no_tags(monkeypatch):
    r = build_runner(5000)
    calls = _capture(monkeypatch, r)
    r.clone_bare("http://mirror.test:5000/x.git", "main", None, "/tmp/x.git")
    r.fetch_to_ref("/tmp/x.git", "http://mirror.test:5000/x.git", "main", None, "refs/akb/fetch-tmp/main")
    clone = next(c for c in calls if "clone" in c["args"])
    fetch = next(c for c in calls if "fetch" in c["args"])
    assert "--no-tags" in clone["args"]
    assert "--no-tags" in fetch["args"] and "--no-recurse-submodules" in fetch["args"]


# ══ Structure inspector = real default-deny allowlist ═══════
@pytest.mark.parametrize(
    "snippet,needle",
    [
        # secondary-fetch keys (reject set)
        ('\n[fetch]\n\tbundleURI = https://evil.example/x.bundle\n', "disallowed"),
        ('\n[transfer]\n\tbundleURI = true\n', "disallowed"),
        # promisor / partial-clone / transport-redirect keys on origin
        ('\n[remote "origin"]\n\tpromisor = true\n', "remote.origin"),
        ('\n[remote "origin"]\n\tpartialclonefilter = blob:none\n', "remote.origin"),
        ('\n[remote "origin"]\n\tvcs = evil\n', "remote.origin"),
        ('\n[remote "origin"]\n\tproxy = http://evil:8080\n', "remote.origin"),
        ('\n[remote "origin"]\n\tuploadpack = /tmp/evil.sh\n', "remote.origin"),
        # http.* secondary keys — value-less fixed enum (no key echoed)
        ('\n[http]\n\tcurloptResolve = evil:443:6.6.6.6\n', "http key"),
        ('\n[http]\n\tsslCert = /tmp/e.pem\n', "http key"),
        ('\n[http]\n\tcookieFile = /tmp/c\n', "http key"),
        # command-bearing core keys not in the allowlist
        ('\n[core]\n\tfsmonitor = /tmp/evil\n', "core key"),
        ('\n[core]\n\tpager = /tmp/evil\n', "core key"),
        # LEGACY dotted-subsection syntax must be parsed like [remote "origin"]
        ('\n[remote.origin]\n\turl = https://evil.example/x.git\n', "origin"),
        # unknown section → default-deny
        ('\n[weirdsection]\n\tkey = val\n', "disallowed"),
    ],
)
def test_structure_default_deny_flags(tmp_path, snippet, needle):
    bare = _init_bare(tmp_path)
    _append_config(bare, snippet)
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert any(needle in f.lower() for f in findings), findings


def test_structure_findings_are_value_less(tmp_path):
    """A finding must NEVER echo a caller-controlled subsection value (a
    renamed remote whose name is a URL) or a config value."""
    bare = _init_bare(tmp_path)
    _append_config(
        bare,
        '\n[remote "https://x-access-token:SEKRIT@evil.example/x.git"]\n'  # pragma: allowlist secret
        "\turl = https://evil.example/x.git\n"
        "\tproxy = http://SECRETPROXY:9\n",
    )
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert findings  # flagged
    blob = " ".join(findings)
    assert "SEKRIT" not in blob and "SECRETPROXY" not in blob and "evil.example" not in blob


def test_structure_origin_fetch_refspec_mismatch_flagged(tmp_path):
    bare = _init_bare(tmp_path)
    _append_config(
        bare,
        '\n[remote "origin"]\n\turl = https://good.example/x.git\n'
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
    )
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert any("fetch refspec" in f.lower() for f in findings), findings


def test_structure_origin_fetch_refspec_exact_match_is_clean(tmp_path):
    bare = _init_bare(tmp_path)
    # The single form a bare single-branch clone would persist
    # (narrowing): the `refs/remotes/origin/*` destination belongs to NON-bare
    # clones and is now rejected — see test_structure_origin_fetch_refspec_
    # mismatch_flagged.
    _append_config(
        bare,
        '\n[remote "origin"]\n\turl = https://good.example/x.git\n'
        "\tfetch = +refs/heads/main:refs/heads/main\n",
    )
    r = ExternalGitRunner(settings=Settings())
    assert r.inspect_structure(bare, "https://good.example/x.git", "main") == []


# ── VALUE + REQUIRED-STRUCTURE default-deny ──────
# The name-only allowlist let a bare repo claim `bare = false`, ship
# `objectFormat = sha256`, carry an arbitrary `[branch]` pointing off-canonical,
# or omit remote.origin entirely — all with NO finding. These lock the value and
# required-structure checks (Finding #2).
@pytest.mark.parametrize(
    "snippet,needle",
    [
        # a bare repo that CLAIMS non-bare — VALUE default-deny, not just key.
        ("\n[core]\n\tbare = false\n", "bare is not true"),
        # repositoryformatversion must be exactly 0 (1 ⇒ extensions / non-SHA-1).
        (
            "\n[core]\n\trepositoryformatversion = 1\n",
            "repositoryformatversion is not 0",
        ),
        # an object format that is not the SHA-1 world this reader assumes.
        ("\n[extensions]\n\tobjectFormat = sha256\n", "objectformat"),
        # a platform-boolean core key carrying a non-boolean (path/command).
        ("\n[core]\n\tfilemode = /tmp/evil\n", "non-boolean"),
        # a bare clone writes NO [branch]; an arbitrary branch pointing off the
        # canonical remote/merge must be a finding.
        (
            '\n[branch "anything"]\n\tremote = https://evil.example/x\n'
            "\tmerge = refs/heads/evil\n",
            "branch",
        ),
    ],
)
def test_structure_value_and_required_structure_default_deny(tmp_path, snippet, needle):
    bare = _init_bare(tmp_path)
    _append_config(bare, snippet)
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert any(needle in f.lower() for f in findings), findings


def test_structure_flags_missing_origin(tmp_path):
    """A bare repo with NO remote.origin.url has no canonical pin at all — a
    finding in its own right (Finding #2). `git init --bare` writes a
    [core] section but no remote, which is exactly that shape."""
    bare = _init_bare(tmp_path)
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert any("missing remote.origin" in f.lower() for f in findings), findings


def test_structure_clean_config_with_platform_booleans_is_clean(tmp_path):
    """No false-reject: a config carrying every platform-boolean variant a fresh
    bare clone may emit (incl. `false` values and a valueless `bare`) plus the
    canonical origin + tagOpt is structurally clean."""
    bare = _init_bare(tmp_path)
    cfg = bare / "config"
    cfg.write_text(
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = true\n"
        "\tbare\n"  # valueless boolean key ⇒ git-true
        "\tignorecase = true\n"
        "\tprecomposeunicode = false\n"
        "\tsymlinks = false\n"
        '[remote "origin"]\n'
        "\turl = https://good.example/x.git\n"
        "\ttagOpt = --no-tags\n"
    )
    r = ExternalGitRunner(settings=Settings())
    assert r.inspect_structure(bare, "https://good.example/x.git", "main") == []


def test_structure_finding_never_echoes_secret_in_config_key(tmp_path):
    """Finding #4: a secret placed into a config KEY name (not only a
    subsection) must be absent from the value-less findings — the finding is a
    fixed section-level enum, so no caller-controlled text is interpolated."""
    bare = _init_bare(tmp_path)
    _append_config(
        bare,
        "\n[http]\n\thttps://user:SECRETMARKER123@evil.example/x = 1\n"  # pragma: allowlist secret
        "\n[core]\n\tSECRETMARKER456 = /tmp/evil\n",
    )
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert findings  # flagged
    blob = " ".join(findings).lower()
    assert "secretmarker123" not in blob
    assert "secretmarker456" not in blob
    assert "evil.example" not in blob


def test_structure_flags_symlink_config(tmp_path):
    bare = _init_bare(tmp_path)
    cfg = bare / "config"
    real = cfg.read_text()
    cfg.unlink()
    target = tmp_path / "elsewhere_config"
    target.write_text(real)
    cfg.symlink_to(target)
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert any("symlink" in f.lower() for f in findings), findings


def test_structure_flags_oversized_config(tmp_path, monkeypatch):
    bare = _init_bare(tmp_path)
    monkeypatch.setattr(egr, "_MAX_CONFIG_BYTES", 64)
    _append_config(bare, "\n; " + "x" * 400 + "\n")
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare)
    assert any("size cap" in f.lower() for f in findings), findings


def test_structure_flags_dumb_http_alternate(tmp_path):
    """A dumb-HTTP `objects/info/http-alternates` (off-origin fetch pointer that
    git exposes no config knob to disable) must be flagged so the caller
    re-clones — the app-level closure for that egress class."""
    bare = _init_bare(tmp_path)
    p = bare / "objects" / "info" / "http-alternates"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("http://169.254.169.254/latest/\n")
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare)
    assert any("http-alternates" in f.lower() for f in findings), findings


# ══ REQUIRED-KEY PRESENCE + duplicate default-deny ══
# The value-checks above only fire when a key is PRESENT; a config that OMITS a
# required key (repositoryformatversion / bare / origin.url), is blank/absent, or
# DUPLICATES origin.url/fetch previously scanned clean. These lock the presence
# and occurrence side of the default-deny.

# The EXACT `.git/config` a fresh bare single-branch clone writes, captured
# empirically (git 2.51.0):
#     git clone --bare --single-branch --no-tags --branch main -- <url> <dest>
# yields [core] (repositoryformatversion=0, filemode, bare=true, ignorecase,
# precomposeunicode) + [remote "origin"] (url, tagOpt=--no-tags) and NO fetch
# key. This shape MUST stay clean (false-reject 0) after the presence checks.
_EMPIRICAL_CLEAN_CLONE_CONFIG = (
    "[core]\n"
    "\trepositoryformatversion = 0\n"
    "\tfilemode = true\n"
    "\tbare = true\n"
    "\tignorecase = true\n"
    "\tprecomposeunicode = true\n"
    '[remote "origin"]\n'
    "\turl = {url}\n"
    "\ttagOpt = --no-tags\n"
)


@pytest.mark.parametrize(
    "text,needle",
    [
        # blank / absent config (read as "") — a bare repo always has one.
        ("", "empty or absent"),
        ("   \n\n\t\n", "empty or absent"),
        # required core keys absent — value-checks alone would pass them clean.
        (
            '[core]\n\tbare = true\n[remote "origin"]\n\turl = {url}\n',
            "missing core.repositoryformatversion",
        ),
        (
            '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = {url}\n',
            "missing core.bare",
        ),
        # no [core] section at all ⇒ BOTH required-core findings (subsumes a
        # "missing core section" descriptor).
        ('[remote "origin"]\n\turl = {url}\n', "missing core.repositoryformatversion"),
        ('[remote "origin"]\n\turl = {url}\n', "missing core.bare"),
        # duplicated origin.url (git treats it as multi-valued → a planted 2nd
        # entry could redirect the fetch) even when BOTH copies match canonical.
        (
            "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
            '[remote "origin"]\n\turl = {url}\n\turl = {url}\n',
            "duplicate remote.origin.url",
        ),
        # duplicated fetch refspec (an extra, possibly wider, mapping).
        (
            "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
            '[remote "origin"]\n\turl = {url}\n'
            "\tfetch = +refs/heads/main:refs/heads/main\n"
            "\tfetch = +refs/heads/main:refs/heads/main\n",
            "duplicate remote.origin fetch refspec",
        ),
    ],
)
def test_inspect_config_text_flags_missing_or_duplicate_required(text, needle):
    findings = _inspect_config_text(
        text.format(url="https://good.example/x.git"),
        expected_url="https://good.example/x.git",
        branch="main",
    )
    assert any(needle in f for f in findings), findings


def test_inspect_config_text_empirical_clone_is_clean_false_reject_zero():
    """The exact config a fresh bare single-branch clone writes (captured from
    git 2.51.0, see _EMPIRICAL_CLEAN_CLONE_CONFIG) is still clean after the
    required-key presence checks — the false-reject-0 guarantee."""
    text = _EMPIRICAL_CLEAN_CLONE_CONFIG.format(url="https://good.example/x.git")
    assert _inspect_config_text(
        text, expected_url="https://good.example/x.git", branch="main"
    ) == []
    # A single (non-duplicated) fetch refspec of the expected shape also passes —
    # a bare clone writes none, but a legitimately-narrowed one is allowed.
    with_fetch = text.replace(
        "\ttagOpt = --no-tags\n",
        "\ttagOpt = --no-tags\n\tfetch = +refs/heads/main:refs/heads/main\n",
    )
    assert _inspect_config_text(
        with_fetch, expected_url="https://good.example/x.git", branch="main"
    ) == []


def test_structure_flags_absent_config_file(tmp_path):
    """A bare dir that EXISTS but whose config file is gone is not clean: a bare
    repo always has a config, so its absence is a finding (was a default-allow
    hole — inspect_structure returned [] on a missing config file)."""
    bare = _init_bare(tmp_path)
    (bare / "config").unlink()
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert any("empty or absent" in f for f in findings), findings


def test_structure_flags_blank_config_file(tmp_path):
    bare = _init_bare(tmp_path)
    (bare / "config").write_text("")
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert any("empty or absent" in f for f in findings), findings


def test_structure_absent_bare_dir_stays_clone_signal(tmp_path):
    """A wholly-absent bare dir is the caller's "clone, don't fetch" signal and
    must remain [] — the missing-config finding is scoped to a bare that exists
    but has no/blank config, not to a repo that was never cloned."""
    r = ExternalGitRunner(settings=Settings())
    assert r.inspect_structure(tmp_path / "never.git", "https://good.example/x.git") == []


@pytest.mark.parametrize(
    "cfg_body,needle",
    [
        # a bare repo config with [core] but the required key omitted.
        (
            '[core]\n\tbare = true\n[remote "origin"]\n\turl = {url}\n',
            "missing core.repositoryformatversion",
        ),
        (
            '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = {url}\n',
            "missing core.bare",
        ),
        # duplicated origin url / fetch written on disk.
        (
            "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
            '[remote "origin"]\n\turl = {url}\n\turl = {url}\n',
            "duplicate remote.origin.url",
        ),
        (
            "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
            '[remote "origin"]\n\turl = {url}\n'
            "\tfetch = +refs/heads/main:refs/heads/main\n"
            "\tfetch = +refs/heads/main:refs/heads/main\n",
            "duplicate remote.origin fetch refspec",
        ),
    ],
)
def test_structure_flags_missing_or_duplicate_required_on_disk(tmp_path, cfg_body, needle):
    bare = _init_bare(tmp_path)
    (bare / "config").write_text(cfg_body.format(url="https://good.example/x.git"))
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert any(needle in f for f in findings), findings


def test_structure_empirical_clone_config_on_disk_is_clean(tmp_path):
    """False-reject 0 at the FS layer: the empirically-captured clean clone
    config written verbatim is structurally clean."""
    bare = _init_bare(tmp_path)
    (bare / "config").write_text(
        _EMPIRICAL_CLEAN_CLONE_CONFIG.format(url="https://good.example/x.git")
    )
    r = ExternalGitRunner(settings=Settings())
    assert r.inspect_structure(bare, "https://good.example/x.git", "main") == []


def test_missing_and_duplicate_findings_are_value_less(tmp_path):
    """The new presence/duplicate findings are FIXED strings — a secret placed
    into the duplicated url must never leak into a finding."""
    bare = _init_bare(tmp_path)
    (bare / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
        '[remote "origin"]\n'
        "\turl = https://good.example/x.git\n"
        "\turl = https://x-access-token:SEKRIT999@evil.example/x.git\n"  # pragma: allowlist secret
    )
    r = ExternalGitRunner(settings=Settings())
    findings = r.inspect_structure(bare, "https://good.example/x.git", "main")
    assert any("duplicate remote.origin.url" in f for f in findings), findings
    blob = " ".join(findings)
    assert "SEKRIT999" not in blob and "evil.example" not in blob


# ══ Type-enforced safe-failure — sanitizer coverage ═════════
def test_sanitize_strips_token_base64_and_any_userinfo_url():
    token = "SEKRIT-TOKEN-abc123"
    cred = _cred_header(token)
    b64 = cred.split(" ", 2)[-1]
    raw = (
        f"fatal: unable to access "
        f"'https://x-access-token:{token}@evil.example/x.git': HTTP 401\n"  # pragma: allowlist secret
        f"sent header {cred}\n"
        f"redirected to https://otheruser:otherpass@other.example/y.git\n"  # pragma: allowlist secret
    )
    out = _sanitize(raw, url="https://evil.example/x.git", cred=cred, token=token)
    assert token not in out
    assert b64 not in out
    assert "x-access-token" not in out
    # ANY userinfo-bearing URL is stripped, not only the exact canonical one.
    assert "otherpass" not in out and "otheruser" not in out


def test_sanitize_strips_multi_at_userinfo():
    """Finding #4: a multi-`@` authority (crafted so a first-`@`-only strip
    would leave trailing userinfo like `b@c@host`) is redacted up to the LAST
    `@`; the real host after it is preserved."""
    raw = "fatal: unable to access 'https://a@b@c@host.example/x.git': 500"
    out = _sanitize(raw, url=None, cred=None, token=None)
    assert "a@b@c" not in out  # no userinfo remnant survives
    assert "<redacted>@host.example" in out  # authority userinfo gone, host kept


# ══ MINOR (round 3): userinfo redaction stops at the authority boundary ══
# The authority run is now `[^/\s?#]+@` (was `[^/\s]+@`): the `@` in a query or
# fragment no longer drags the real host into the redaction, while a genuine
# multi-`@` userinfo is still redacted in full. This is a diagnostic-quality fix
# (over-redaction), not a secret leak.
@pytest.mark.parametrize(
    "raw,expect_present,expect_absent",
    [
        # a `@` in the QUERY must not be mistaken for userinfo — host preserved.
        (
            "fatal: cannot access 'https://host.example/x.git?email=a@b.example'",
            "https://host.example/x.git?email=a@b.example",
            "<redacted>",
        ),
        # a `@` in the FRAGMENT — host preserved.
        (
            "note https://host.example/x#anchor@v2 failed",
            "https://host.example/x#anchor@v2",
            "<redacted>",
        ),
        # a `@` in the PATH — host preserved (was already fine; kept as a guard).
        (
            "ref https://host.example/path@name here",
            "https://host.example/path@name",
            "<redacted>",
        ),
        # a URL with NO `@` at all — untouched.
        (
            "GET https://host.example/plain done",
            "https://host.example/plain",
            "<redacted>",
        ),
    ],
)
def test_sanitize_does_not_over_redact_query_fragment_path_at(raw, expect_present, expect_absent):
    out = _sanitize(raw, url=None, cred=None, token=None)
    assert expect_present in out, out
    assert expect_absent not in out, out


def test_sanitize_strips_real_userinfo_but_keeps_query_at():
    """A URL with BOTH genuine userinfo AND a query `@`: the userinfo is
    redacted, the host and the query `@` survive."""
    raw = "fatal: access 'https://user:pass@host.example/x?to=a@b.example' denied"  # pragma: allowlist secret
    out = _sanitize(raw, url=None, cred=None, token=None)
    assert "user:pass" not in out and "pass@" not in out
    assert "<redacted>@host.example/x?to=a@b.example" in out, out


# ══ Regression: env gate beats a config-level protocol.*.allow override ══
def test_git_allow_protocol_is_authoritative_over_config(tmp_path):
    """A `-c protocol.ext.allow=always` (or a GIT_CONFIG_* override) must NOT
    re-enable ext:: — the sealed env GIT_ALLOW_PROTOCOL=https wins over config."""
    r = ExternalGitRunner(settings=Settings(external_git_allow_http=False))
    marker = tmp_path / "PWNED"
    with pytest.raises(ExternalGitCommandError) as exc:
        r._exec(
            [
                "-c",
                "protocol.ext.allow=always",
                "ls-remote",
                "--",
                f"ext::sh -c touch{chr(32)}{marker}",
                "main",
            ],
            cwd=str(tmp_path),
            timeout=30,
        )
    assert "not allowed" in str(exc.value)
    assert not marker.exists()


# ══ Hermetic mirror READ paths (over the fixture) ════════
def test_hermetic_readers_over_fixture(git_http, tmp_path):
    r = build_runner(git_http.port)
    url, head = git_http.add_repo(
        "readers", {"doc.md": "# Title\n\nbody\n", "sub/a.md": "aa\n"}
    )
    dest = tmp_path / "c.git"
    r.clone_bare(url, "main", None, dest)

    assert r.cat_path(dest, "HEAD", "doc.md") == b"# Title\n\nbody\n"
    assert r.cat_path(dest, head, "doc.md") == b"# Title\n\nbody\n"
    assert r.cat_path(dest, "HEAD", "nope.md") is None

    log = r.log_for_path(dest, "doc.md", 10)
    assert log and log[0]["hash"] == head and log[0]["message"].strip() == "init"

    vl = r.vault_log_entries(dest, max_count=10)
    assert vl and vl[0]["hash"] == head
    assert any(f["path"] == "doc.md" for f in vl[0]["files"])

    d = r.file_diff_entry(dest, head, "doc.md")
    assert d["type"] == "added" and "+# Title" in d["diff"]
    # An abbreviated hex commit resolves via hermetic rev-parse.
    assert r.file_diff_entry(dest, head[:12], "doc.md")["type"] == "added"
    # A non-hex / unknown commit → unknown, never an exception.
    assert r.file_diff_entry(dest, "not-a-sha", "doc.md")["type"] == "unknown"


def test_file_diff_entry_modified_over_fixture(git_http, tmp_path):
    r = build_runner(git_http.port)
    url, head1 = git_http.add_repo("diffmod", {"doc.md": "one\n"})
    dest = tmp_path / "c.git"
    r.clone_bare(url, "main", None, dest)
    git_http.publish_change("diffmod", "doc.md", "one\ntwo\n")
    r.fetch_to_ref(dest, url, "main", None, "refs/akb/fetch-tmp/main")
    r.update_ref(dest, "refs/heads/main", "refs/akb/fetch-tmp/main")
    head2 = r.rev_parse(dest, "refs/heads/main")
    d = r.file_diff_entry(dest, head2, "doc.md")
    assert d["type"] == "modified" and "+two" in d["diff"]


# ══ Streaming output cap — no oversized materialization ══
def test_capped_content_read_aborts_before_buffering_whole_blob(git_http, tmp_path):
    """A content read with ``max_output_bytes`` STREAMS stdout and aborts the
    instant it passes the cap (process group killed), instead of buffering the
    whole (oversized) blob into memory — the general memory-growth backstop behind
    the per-blob size pre-check. A cap comfortably above
    the size returns the full content (no false trip)."""
    r = build_runner(git_http.port)
    big = "X" * 300_000
    url, _head = git_http.add_repo("cap", {"big.md": big})
    dest = tmp_path / "c.git"
    r.clone_bare(url, "main", None, dest)
    oid = r.resolve_blob_oid(dest, "HEAD", "big.md")
    assert oid is not None and _OID_RE.fullmatch(oid)

    # A cap far below the blob size → the streamed read aborts with a 413-coded
    # cap error, NOT a silent None (the cap error is deliberately not an
    # ExternalGitCommandError, so cat_path's except handler cannot swallow it).
    with pytest.raises(ExternalGitOutputCapError) as exc:
        r.cat_blob(dest, oid, max_output_bytes=4096)
    assert exc.value.status_code == 413
    with pytest.raises(ExternalGitOutputCapError):
        r.cat_path(dest, "HEAD", "big.md", max_output_bytes=4096)

    # Above the size → full content, byte-exact (no false trip, no truncation).
    assert r.cat_blob(dest, oid, max_output_bytes=len(big.encode()) + 4096) == big.encode()
    assert r.cat_path(
        dest, "HEAD", "big.md", max_output_bytes=len(big.encode()) + 4096
    ) == big.encode()


def test_file_diff_entry_streaming_cap_bounds_the_patch(git_http, tmp_path):
    """The diff renderer honours ``max_output_bytes`` too — a root-commit
    addition (full content materialized) is aborted when the rendered patch would
    exceed the cap (diff backstop)."""
    r = build_runner(git_http.port)
    big = "line\n" * 60_000  # ~300 KB, rendered as +lines
    url, head = git_http.add_repo("capdiff", {"big.md": big})
    dest = tmp_path / "c.git"
    r.clone_bare(url, "main", None, dest)
    with pytest.raises(ExternalGitOutputCapError):
        r.file_diff_entry(dest, head, "big.md", max_output_bytes=4096)
    # Uncapped still renders the full addition (no regression).
    assert r.file_diff_entry(dest, head, "big.md")["type"] == "added"


def test_resolve_blob_oid_binds_size_and_read_across_head_move(git_http, tmp_path):
    """The exact-OID primitive: ``resolve_blob_oid`` pins ``<rev>:<path>`` to one
    immutable blob OID, so a concurrent fetch that promotes HEAD between the size
    check and the read cannot swap a small blob for a large one — size AND read
    stay bound to the same object."""
    r = build_runner(git_http.port)
    url, head1 = git_http.add_repo("oidbind", {"doc.md": "ORIGINAL\n"})
    dest = tmp_path / "c.git"
    r.clone_bare(url, "main", None, dest)
    oid = r.resolve_blob_oid(dest, "HEAD", "doc.md")
    assert oid is not None and _OID_RE.fullmatch(oid)
    assert r.blob_size(dest, oid) == len(b"ORIGINAL\n")

    # Promote HEAD to DIFFERENT (longer) content for the same path.
    git_http.publish_change("oidbind", "doc.md", "CHANGED-AND-MUCH-LONGER\n")
    r.fetch_to_ref(dest, url, "main", None, "refs/akb/fetch-tmp/main")
    r.update_ref(dest, "refs/heads/main", "refs/akb/fetch-tmp/main")
    assert r.rev_parse(dest, "HEAD") != head1  # HEAD moved

    # The oid resolved BEFORE the move still sizes + reads the ORIGINAL blob —
    # immune to the HEAD promotion (the whole point of exact-OID binding).
    assert r.blob_size(dest, oid) == len(b"ORIGINAL\n")
    assert r.cat_blob(dest, oid) == b"ORIGINAL\n"
    # Resolving HEAD:doc.md NOW yields the new blob (a different OID).
    oid2 = r.resolve_blob_oid(dest, "HEAD", "doc.md")
    assert oid2 != oid and r.cat_blob(dest, oid2) == b"CHANGED-AND-MUCH-LONGER\n"


def test_resolve_blob_oid_missing_and_bad_rev(git_http, tmp_path):
    r = build_runner(git_http.port)
    url, _head = git_http.add_repo("oidmiss", {"doc.md": "x\n"})
    dest = tmp_path / "c.git"
    r.clone_bare(url, "main", None, dest)
    assert r.resolve_blob_oid(dest, "HEAD", "nope.md") is None  # missing path → None
    # An unknown (valid-hex) rev does not resolve to a commit → PROPAGATE, never
    # masked as absent (fail-closed; a corrupt commit is indistinguishable here).
    with pytest.raises(ExternalGitCommandError):
        r.resolve_blob_oid(dest, "0" * 40, "doc.md")
    with pytest.raises(ExternalGitPolicyError):
        r.resolve_blob_oid(dest, "notahex", "doc.md")  # bad rev shape → policy error


def test_first_parent_oid_root_and_child(git_http, tmp_path):
    r = build_runner(git_http.port)
    url, head1 = git_http.add_repo("fp", {"a.md": "1\n"})
    dest = tmp_path / "c.git"
    r.clone_bare(url, "main", None, dest)
    assert r.first_parent_oid(dest, head1) is None  # root commit → no parent
    head2 = git_http.publish_change("fp", "a.md", "2\n")
    r.fetch_to_ref(dest, url, "main", None, "refs/akb/fetch-tmp/main")
    r.update_ref(dest, "refs/heads/main", "refs/akb/fetch-tmp/main")
    assert r.first_parent_oid(dest, head2) == head1  # child → first parent


def test_path_size_propagates_non_missing_error(tmp_path, monkeypatch):
    """``path_size`` collapses ONLY a genuinely missing rev/path to None; a real
    failure sizing an oid that DID resolve PROPAGATES rather than
    being masked as 'not present' — which a diff caller would misread as 'not
    oversized, proceed to materialize'."""
    bare = _init_bare(tmp_path)
    commit = _make_commit(bare, "a.md", "hi\n")
    r = ExternalGitRunner(settings=Settings())
    assert r.path_size(bare, commit, "nope.md") is None  # missing path → None

    def _boom(*a, **k):
        raise ExternalGitCommandError("boom sizing a resolved oid")

    monkeypatch.setattr(r, "blob_size", _boom)
    with pytest.raises(ExternalGitCommandError):
        r.path_size(bare, commit, "a.md")  # a.md resolves → blob_size raises → propagates


# ══ Timeout kills the whole process GROUP and reaps ═════════
def test_timeout_kills_process_group_and_reaps(tmp_path):
    """A wedged child + its BACKGROUND descendant are both killed by killpg on
    timeout (start_new_session groups them); the descendant never runs to its
    delayed side effect, and _exec returns promptly (no 30s hang)."""
    r = ExternalGitRunner(settings=Settings(), git_binary="/bin/sh")
    desc_marker = tmp_path / "DESCENDANT_RAN"
    # Backgrounded descendant would touch the marker at +2s; parent waits 30s.
    script = f"(sleep 2; touch '{desc_marker}') & sleep 30"
    start = time.monotonic()
    with pytest.raises(ExternalGitTransientError):
        r._exec(["-c", script], cwd=str(tmp_path), timeout=0.5)
    assert time.monotonic() - start < 15  # killed promptly, not after 30s
    # Wait past the descendant's +2s touch; killpg must have reaped it.
    time.sleep(2.5)
    assert not desc_marker.exists()


# ══ MAJOR (fix-4): resolve_blob_oid is a fail-closed typed ls-tree lookup ══
def test_resolve_blob_oid_typed_ls_tree_classification(tmp_path, monkeypatch):
    """``resolve_blob_oid`` uses a hermetic typed ls-tree lookup (MAJOR, fix-4):
    rc0 + an exact blob row → its OID; rc0 + EMPTY output → the path is genuinely
    absent → None; a malformed row, a non-40-hex object field, or a nonzero exit
    (surfaced as ``_local`` raising) → PROPAGATE, never masked as 'missing' —
    which ``path_size`` and its diff caller would misread as 'not oversized,
    proceed to materialize'."""
    r = ExternalGitRunner(settings=Settings())
    bare = _init_bare(tmp_path)
    commit = "c" * 40
    blob = "a" * 40
    # The commit always resolves to a full OID in this unit; real commit
    # corruption is exercised end-to-end below.
    monkeypatch.setattr(r, "rev_parse", lambda b, rev: commit)

    # rc0 + a single well-formed blob row at the exact path → its OID.
    monkeypatch.setattr(r, "_local", lambda b, a, **k: f"100644 blob {blob}\tx\0")
    assert r.resolve_blob_oid(bare, "HEAD", "x") == blob

    # rc0 + EMPTY output → git's clean 'no such path in tree' → None.
    monkeypatch.setattr(r, "_local", lambda b, a, **k: "")
    assert r.resolve_blob_oid(bare, "HEAD", "x") is None

    # rc0 + a row whose object field is NOT a 40-hex OID → PROPAGATE (not None).
    monkeypatch.setattr(r, "_local", lambda b, a, **k: "100644 blob zzz\tx\0")
    with pytest.raises(ExternalGitCommandError):
        r.resolve_blob_oid(bare, "HEAD", "x")

    # rc0 + a malformed row (no TAB / short metadata) → PROPAGATE.
    monkeypatch.setattr(r, "_local", lambda b, a, **k: "garbage\0")
    with pytest.raises(ExternalGitCommandError):
        r.resolve_blob_oid(bare, "HEAD", "x")

    # A nonzero ls-tree exit surfaces as ``_local`` raising (check=True) → PROPAGATE.
    def _boom(b, a, **k):
        raise ExternalGitCommandError("git command failed (exit 128): not a tree object")

    monkeypatch.setattr(r, "_local", _boom)
    with pytest.raises(ExternalGitCommandError):
        r.resolve_blob_oid(bare, "HEAD", "x")


def test_resolve_blob_oid_propagates_commit_resolution_failure(tmp_path, monkeypatch):
    """Step 1 (``rev-parse <rev>^{commit}``) is fail-closed: a missing / corrupt /
    unknown commit fails HERE and PROPAGATES rather than being mistaken for an
    absent path (MAJOR, fix-4)."""
    r = ExternalGitRunner(settings=Settings())
    bare = _init_bare(tmp_path)

    def _boom(b, rev):
        raise ExternalGitCommandError(
            "git command failed (exit 128): Needed a single revision"
        )

    monkeypatch.setattr(r, "rev_parse", _boom)
    with pytest.raises(ExternalGitCommandError):
        r.resolve_blob_oid(bare, "HEAD", "x")


def test_resolve_blob_oid_real_miss_typed_lookup(git_http, tmp_path):
    """End-to-end over a real clone: a genuinely absent path resolves to None via
    the typed ls-tree lookup (rc0 + empty), a present path resolves to its blob
    oid, and an unknown rev PROPAGATES (fail-closed) — no regression on the common
    present / absent-path cases (MAJOR, fix-4)."""
    r = build_runner(git_http.port)
    url, _head = git_http.add_repo("qmiss", {"doc.md": "x\n"})
    dest = tmp_path / "c.git"
    r.clone_bare(url, "main", None, dest)
    assert r.resolve_blob_oid(dest, "HEAD", "nope.md") is None  # absent path → None
    oid = r.resolve_blob_oid(dest, "HEAD", "doc.md")
    assert oid is not None and _OID_RE.fullmatch(oid)  # present path → its oid
    with pytest.raises(ExternalGitCommandError):
        r.resolve_blob_oid(dest, "0" * 40, "doc.md")  # unknown rev → fail-closed


def test_path_size_propagates_resolve_step_failure(tmp_path, monkeypatch):
    """``path_size`` propagates a real failure at the RESOLVE step too (not only
    the size step that ``test_path_size_propagates_non_missing_error`` covers): a
    commit that fails to resolve is not masked as 'absent' (MAJOR, fix-4)."""
    r = ExternalGitRunner(settings=Settings())
    bare = _init_bare(tmp_path)

    def _boom(b, rev):
        raise ExternalGitCommandError(
            "git command failed (exit 128): Needed a single revision"
        )

    monkeypatch.setattr(r, "rev_parse", _boom)
    with pytest.raises(ExternalGitCommandError):
        r.path_size(bare, "0" * 40, "a.md")


# End-to-end fail-closed proof on REAL (tmp_path-isolated) corrupt object stores.
def _empty_object_store(bare):
    """Remove every loose object + pack under a tmp_path bare so its commit object
    is GONE (a 'missing object store' corruption). tmp_path only — never a real
    repo/.git."""
    objdir = bare / "objects"
    for child in objdir.iterdir():
        if child.name == "info":
            continue  # keep objects/info so git still recognizes the object dir
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _commit_with_missing_root_tree(bare):
    """Store (loose) a commit object whose ``tree`` points at a NON-EXISTENT oid,
    then return its OID: the commit is present but its root tree is absent, WITHOUT
    disturbing any packed object. tmp_path only."""
    ghost_tree = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
    body = (
        f"tree {ghost_tree}\n"
        "author T <t@akb.local> 0 +0000\n"
        "committer T <t@akb.local> 0 +0000\n\nghost\n"
    )
    return _git_dir(bare, "hash-object", "-t", "commit", "-w", "--stdin", want_input=body)


def test_resolve_blob_oid_fails_closed_on_corrupt_object_store(tmp_path):
    """End-to-end on real tmp_path-isolated bares: a corrupt /
    missing object store must PROPAGATE, never be masked as a genuine 'absent
    path' (None) — otherwise the oversized diff gate reads it as 'image absent'
    and could bypass the per-image blob cap on I/O recovery. A genuinely-absent
    path at a healthy commit still returns None. Never touches a real repo/.git."""
    r = ExternalGitRunner(settings=Settings())

    # Baseline: a healthy loose-object bare — present path → oid, absent → None.
    healthy = _init_bare(tmp_path, "healthy.git")
    hc = _make_commit(healthy, "doc.md", "hello\n")
    assert r.resolve_blob_oid(healthy, hc, "doc.md") is not None
    assert r.resolve_blob_oid(healthy, hc, "nope.md") is None  # genuine absent → None
    assert r.path_size(healthy, hc, "nope.md") is None

    # Corruption 1 — emptied object store (the COMMIT object is gone): step-1
    # rev-parse ^{commit} fails → PROPAGATE (was masked as None before the fix).
    empty = _init_bare(tmp_path, "empty.git")
    ec = _make_commit(empty, "doc.md", "hello\n")
    _empty_object_store(empty)
    with pytest.raises(ExternalGitCommandError):
        r.resolve_blob_oid(empty, ec, "doc.md")
    with pytest.raises(ExternalGitCommandError):
        r.path_size(empty, ec, "doc.md")

    # Corruption 2 — commit present but ROOT TREE object gone: step-2 ls-tree
    # exits nonzero → PROPAGATE.
    notree = _init_bare(tmp_path, "notree.git")
    gc = _commit_with_missing_root_tree(notree)
    with pytest.raises(ExternalGitCommandError):
        r.resolve_blob_oid(notree, gc, "doc.md")
    with pytest.raises(ExternalGitCommandError):
        r.path_size(notree, gc, "doc.md")

    # Corruption 3 — tree present but the BLOB object gone: ls-tree still lists the
    # row (it never reads the blob), so resolve returns the oid; but the SIZE step
    # (cat-file -s) fails closed, so ``path_size`` — what the gate calls —
    # PROPAGATES (the gate sees an error, not 'absent').
    noblob = _init_bare(tmp_path, "noblob.git")
    bc = _make_commit(noblob, "doc.md", "hello\n")
    blob = _git_dir(noblob, "rev-parse", f"{bc}:doc.md")
    (noblob / "objects" / blob[:2] / blob[2:]).unlink()
    assert r.resolve_blob_oid(noblob, bc, "doc.md") == blob  # tree row still listed
    with pytest.raises(ExternalGitCommandError):
        r.path_size(noblob, bc, "doc.md")  # size step fails closed


# ══ Capped reader — single deadline + group kill/reap ══
def test_capped_reader_single_deadline_after_eof(tmp_path):
    """After stdout+stderr reach EOF, a child that lingers before exiting cannot
    stretch the wall clock past the ORIGINAL timeout: the capped reader waits for
    exit under ONE deadline, not the old fixed +5s post-EOF wait."""
    r = ExternalGitRunner(settings=Settings(), git_binary="/bin/sh")
    # Emit a little stdout, CLOSE both std streams, then sleep well past the
    # deadline. The reader must abort at ~0.5s, not the old ~5s post-EOF overshoot.
    script = "printf hi; exec 1>&- 2>&-; sleep 30"
    start = time.monotonic()
    with pytest.raises(ExternalGitTransientError):
        r._exec(
            ["-c", script], cwd=str(tmp_path), timeout=0.5, max_output_bytes=1_000_000
        )
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, elapsed  # ~0.5s — decisively under the old +5s overshoot


def test_capped_reader_cap_abort_kills_process_group(tmp_path):
    """Exceeding ``max_output_bytes`` SIGKILLs the whole process GROUP (not just
    the direct child) and reaps it: a backgrounded descendant never runs to its
    delayed side effect, so no orphan/zombie survives the cap abort."""
    r = ExternalGitRunner(settings=Settings(), git_binary="/bin/sh")
    marker = tmp_path / "DESC_RAN"
    # Descendant touches the marker at +2s; the parent floods stdout so the 4 KiB
    # cap trips at once and must killpg the group before the descendant's touch.
    script = f"(sleep 2; touch '{marker}') & yes AAAAAAAA"
    with pytest.raises(ExternalGitOutputCapError):
        r._exec(["-c", script], cwd=str(tmp_path), timeout=10, max_output_bytes=4096)
    time.sleep(2.5)
    assert not marker.exists()


def test_capped_reader_selector_failure_kills_reaps_and_closes(tmp_path, monkeypatch):
    """If the selector itself raises (a bad / out-of-range FD, an
    OSError from ``select``), the capped reader's whole-body ``try/finally`` still
    SIGKILLs the process group, reaps the child, and CLOSES both pipe FDs — no
    orphan / zombie / leaked descriptor survives a selector fault."""
    r = ExternalGitRunner(settings=Settings())
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    real_default_selector = selectors.DefaultSelector

    class _BoomSelector:
        """Register / unregister on a REAL selector so the drain loop is entered,
        but raise the instant ``select`` is called (a forced FD / OSError fault)."""

        def __init__(self):
            self._real = real_default_selector()

        def register(self, *a, **k):
            return self._real.register(*a, **k)

        def unregister(self, *a, **k):
            return self._real.unregister(*a, **k)

        def get_map(self):
            return self._real.get_map()

        def select(self, timeout=None):
            raise OSError("forced selector failure (out-of-range FD)")

        def close(self):
            return self._real.close()

    monkeypatch.setattr(egr.selectors, "DefaultSelector", _BoomSelector)
    with pytest.raises(OSError):
        r._communicate_capped(proc, timeout=10, max_output_bytes=4096)
    # The finally-block cleanup must have run on the abnormal (selector) exit:
    assert proc.poll() is not None  # killpg + wait → child reaped (no zombie)
    assert proc.stdout.closed  # stdout pipe FD closed (no leak)
    assert proc.stderr.closed  # stderr pipe FD closed (no leak)
