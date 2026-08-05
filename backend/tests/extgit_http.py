"""In-process smart-HTTP git server + policy helpers for exercising
``ExternalGitRunner`` against a REAL git transport (stage 2 tests).

The validator is fail-closed: a mirror URL must resolve to a globally-routable
unicast address, so a plain ``http://127.0.0.1`` fixture would be rejected. We
instead serve bare repos over ``git http-backend`` on ``127.0.0.1:<ephemeral>``
and reach them through a FAKE hostname (``mirror.test``) that:

* an injected fake resolver maps to ``127.0.0.1`` (so ``validate`` accepts it via
  a matching ``(host, CIDR, port)`` allowlist rule), and
* the runner pins with ``http.curloptResolve`` — so the exercise covers the DNS
  pin end-to-end while the local server actually answers.

This is the "local git fixture served over the test policy" the design calls
for: it uses the actual ``git`` binary and the actual hermetic env, so protocol
blocks, credential-header injection, ambient-env isolation, and the structure
default-deny are all tested for real, not mocked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.config import Settings
from app.services.external_git_runner import ExternalGitRunner
from app.services.external_git_validation import ExternalGitTransientError

GIT = shutil.which("git") or "git"
FIXTURE_HOST = "mirror.test"


def _git(*args: str, cwd: str, env: dict | None = None) -> str:
    full = dict(os.environ)
    # Deterministic identity so commits are reproducible and never read an
    # ambient user.name/email (which may be unset in CI).
    full.update(
        {
            "GIT_AUTHOR_NAME": "AKB Test",
            "GIT_AUTHOR_EMAIL": "test@akb.local",
            "GIT_COMMITTER_NAME": "AKB Test",
            "GIT_COMMITTER_EMAIL": "test@akb.local",
        }
    )
    if env:
        full.update(env)
    return subprocess.run(
        [GIT, *args], cwd=cwd, env=full, check=True, capture_output=True, text=True
    ).stdout.strip()


class GitHttpFixture:
    """A threaded smart-HTTP git server rooted at a temp ``project_root``.

    ``add_repo`` publishes a bare repo (from an initial file set) and returns
    its ``(url, head_sha)``; ``publish_change`` commits + pushes a follow-up so
    fetch/incremental paths can be exercised.
    """

    def __init__(self) -> None:
        self.project_root = tempfile.mkdtemp(prefix="akb-extgit-httproot-")
        self._works: dict[str, str] = {}
        self.require_auth = False
        self.last_auth: str | None = None
        self.auth_seen = threading.Event()
        # Every served HTTP request bumps this — a read that performs ZERO
        # outbound git traffic leaves it unchanged (zero-network).
        self.request_count = 0
        self._backend = (
            subprocess.run(
                [GIT, "--exec-path"], check=True, capture_output=True, text=True
            ).stdout.strip()
            + "/git-http-backend"
        )
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_factory())
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    # ── repo management ──────────────────────────────────────────────
    def bare_path(self, name: str) -> str:
        return os.path.join(self.project_root, f"{name}.git")

    def url(self, name: str) -> str:
        return f"http://{FIXTURE_HOST}:{self.port}/{name}.git"

    def add_repo(self, name: str, files: dict[str, str], branch: str = "main") -> tuple[str, str]:
        work = os.path.join(self.project_root, f"_work_{name}")
        os.makedirs(work)
        _git("init", "-b", branch, cwd=work)
        for rel, content in files.items():
            p = os.path.join(work, rel)
            os.makedirs(os.path.dirname(p) or work, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        _git("add", "-A", cwd=work)
        _git("commit", "-m", "init", cwd=work)
        head = _git("rev-parse", f"refs/heads/{branch}", cwd=work)
        bare = self.bare_path(name)
        subprocess.run([GIT, "clone", "--bare", work, bare], check=True, capture_output=True)
        self._works[name] = work
        return self.url(name), head

    def publish_change(
        self, name: str, path: str, content: str, branch: str = "main"
    ) -> str:
        """Commit a change in the upstream working repo and push it to the served
        bare. Returns the new upstream head SHA."""
        work = self._works[name]
        p = os.path.join(work, path)
        os.makedirs(os.path.dirname(p) or work, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        _git("add", "-A", cwd=work)
        _git("commit", "-m", f"update {path}", cwd=work)
        head = _git("rev-parse", f"refs/heads/{branch}", cwd=work)
        subprocess.run(
            [GIT, "push", self.bare_path(name), f"{branch}:{branch}"],
            cwd=work,
            check=True,
            capture_output=True,
        )
        return head

    def remove_path(self, name: str, path: str, branch: str = "main") -> str:
        """Delete ``path`` upstream and push. Returns the new upstream head SHA.

        The counterpart of ``publish_change``: a reconcile against this head
        takes the ``local.keys() - remote_tree.keys()`` branch, which is the
        only way to reach ``_delete_external_path`` — and therefore the
        publication cascade — with a real git transport.
        """
        work = self._works[name]
        _git("rm", "-q", "--", path, cwd=work)
        _git("commit", "-m", f"remove {path}", cwd=work)
        head = _git("rev-parse", f"refs/heads/{branch}", cwd=work)
        subprocess.run(
            [GIT, "push", self.bare_path(name), f"{branch}:{branch}"],
            cwd=work,
            check=True,
            capture_output=True,
        )
        return head

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        shutil.rmtree(self.project_root, ignore_errors=True)

    # ── HTTP → git-http-backend (CGI) glue ──────────────────────────
    def _handler_factory(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def _run(self, method: str) -> None:
                fixture.request_count += 1
                auth = self.headers.get("Authorization")
                if auth:
                    fixture.last_auth = auth
                    fixture.auth_seen.set()
                if fixture.require_auth and not auth:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="akb"')
                    self.end_headers()
                    return
                path = self.path.split("?", 1)[0]
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                env = {
                    "GIT_PROJECT_ROOT": fixture.project_root,
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": method,
                    "PATH_INFO": path,
                    "QUERY_STRING": query,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "REMOTE_ADDR": "127.0.0.1",
                    "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                }
                body = b""
                if method == "POST":
                    body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                proc = subprocess.run(
                    [fixture._backend], env=env, input=body, capture_output=True
                )
                head, _, payload = proc.stdout.partition(b"\r\n\r\n")
                status = "200 OK"
                headers: list[tuple[str, str]] = []
                for line in head.decode("latin1").split("\r\n"):
                    if not line:
                        continue
                    if line.lower().startswith("status:"):
                        status = line.split(":", 1)[1].strip()
                    elif ":" in line:
                        k, v = line.split(":", 1)
                        headers.append((k.strip(), v.strip()))
                try:
                    code = int(status.split()[0])
                except ValueError:
                    code = 200
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self._run("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._run("POST")

            def log_message(self, *a) -> None:  # silence
                pass

        return Handler


class FakeResolver:
    """Deterministic resolver for fixture hosts. Unmapped hosts raise transient
    so a test never silently reaches real DNS."""

    def __init__(self, mapping: dict[str, list[str]]):
        self.mapping = mapping

    def resolve(self, host: str, *, timeout: float) -> list[str]:
        if host in self.mapping:
            return list(self.mapping[host])
        raise ExternalGitTransientError(f"no fixture mapping for {host}")


def fixture_settings(port: int, *, allow_http: bool = True, **overrides) -> Settings:
    """Settings that admit the fixture host on its ephemeral port via a full
    (host, CIDR, port) allowlist pin — everything else stays fail-closed."""
    kw = dict(
        external_git_allow_http=allow_http,
        external_git_host_allowlist=[
            {
                "host": FIXTURE_HOST,
                "cidrs": ["127.0.0.0/8", "::1/128"],
                "ports": [port],
            }
        ],
    )
    kw.update(overrides)
    return Settings(**kw)


def build_runner(port: int, *, allow_http: bool = True, **settings_overrides) -> ExternalGitRunner:
    return ExternalGitRunner(
        settings=fixture_settings(port, allow_http=allow_http, **settings_overrides),
        resolver=FakeResolver({FIXTURE_HOST: ["127.0.0.1"]}),
    )
