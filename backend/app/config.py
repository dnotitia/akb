"""AKB runtime configuration.

Single source: two YAML files merged at import time.

Lookup order (first hit wins):
  1. ./config/app.yaml + ./config/secret.yaml   (CWD-relative; local dev)
  2. /etc/akb/app.yaml + /etc/akb/secret.yaml   (containerised deploys)

The split exists so that `app.yaml` is safe to commit/share (no
secrets) and `secret.yaml` stays out of source control. Both files
are flat YAML mappings using the same keys as the Settings model
below — no environment variables are read, with one deliberate
exception: the AKB_PG_POOL_MIN_SIZE / AKB_PG_POOL_MAX_SIZE pool-sizing
overrides read in app/db/postgres.py, a deployment-layer knob so
k8s operators and the akb-platform control plane can tune the pool
per deployment without re-rendering config files.
"""

import ipaddress
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Code-owned hard floor for the external-git runner's git version.
# `http.curloptResolve` — the DNS-pin the hermetic runner depends on — is
# documented from git 2.37, so a git below this cannot enforce the pin. Operators
# may raise `external_git_min_git_version` to demand newer, but never lower it
# past this tuple; both config validation and the startup capability check floor
# against it. Kept here (not in the capability module) so `config` stays the
# single source of truth and there is no import cycle.
EXTERNAL_GIT_MIN_GIT_VERSION_FLOOR: tuple[int, int] = (2, 37)


def parse_git_version(text: str) -> tuple[int, int, int] | None:
    """Extract ``(major, minor, patch)`` from a dotted version string, or None if
    no leading ``N.N[.N]`` can be found. Tolerates a ``git version `` prefix and a
    trailing platform suffix (e.g. ``2.39.3 (Apple Git-145)``). Missing patch is 0.
    """
    import re

    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME = "akb_revision_m1_measurement"


class AuditSettings(BaseModel):
    """Compliance-grade audit log — **producer-only**. AKB emits an
    append-only, hash-chained JSON-lines audit stream and (optionally)
    hands the daily rolled file off to a WORM bucket; the operator's SIEM
    owns storage / query / retention under its own regime. Full rationale
    and the rejected alternatives are in `backend/CHANGELOG.md` 0.8.1.

    Its own nested section (`audit:` in app.yaml) so the surface can grow —
    redaction rules, per-action levels, signing keys, syslog/webhook sinks —
    without scattering `audit_*` keys across the flat top level.
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Local append target. In k8s mount a PVC here — a pod-local emptyDir
    # loses un-uploaded lines on restart.
    log_dir: str = "/data/audit"
    # Log read/query tool calls too (K8s "Metadata" level — no bodies).
    # State-changing calls are ALWAYS logged regardless of this flag. Set
    # false to cut volume on read-heavy deployments.
    log_reads: bool = True
    # S3 bucket for the daily handoff. Blank → file-only (the SIEM tails
    # the file; nothing is uploaded or pruned). Provision the bucket with
    # Object Lock for true WORM — AKB never creates it (lock mode can only
    # be set at bucket creation).
    bucket: str = ""
    # Dedicated audit-storage credentials. Blank fields fall back to the
    # system S3 connection (`s3_endpoint_url` / `s3_access_key` / …) —
    # convenient for small deploys, but for real segregation of duties
    # point these at a SEPARATE audit account. Give the app a *write-only*
    # credential (PutObject, no Delete) on an Object-Lock bucket: AKB never
    # deletes bucket objects (only the local handoff buffer is pruned), so
    # a compromise of the app's primary S3 key cannot rewrite or erase the
    # audit trail.
    endpoint_url: str = ""
    access_key: str = ""
    secret_key: str = ""
    region: str = ""
    # Uploader tick cadence (seconds). A completed file (older than today)
    # uploads on the next tick; the local copy is pruned
    # `local_retention_days` after its date, but only once a bucket upload
    # is confirmed — a bucket outage accumulates files locally instead of
    # losing audit.
    upload_interval_secs: int = 3600
    local_retention_days: int = 2


class ToolUsageSettings(BaseModel):
    """MCP tool-usage tracking — **analytics**, deliberately not the audit
    stream and not the `events` outbox.

    The three sinks sit at different altitudes and must not be unified:
    `events` carries domain verbs on successful writes for Redis fanout and
    is *deleted once delivered*; `audit` is a tamper-evident append-only
    ledger for a SIEM and cannot be grouped; this one answers "which tool is
    actually used, by whom, in what order, and how often does it fail".

    Coupling it to `audit` would be a repeat of the `akb_grep(replace=)`
    defect fixed in 0.9.x: `audit.log_reads=false` silently dropped a
    read-classified tool, and read calls are most of the usage signal.

    Full rationale, schema and rejected alternatives:
    `docs/design/proposal/2026-07-28-mcp-tool-usage-tracking/README.md`.
    """
    model_config = ConfigDict(extra="forbid")

    # Every numeric below is bounded: an unvalidated 0 or negative turns into a
    # hot maintenance loop, a flusher that never drains, or a purge cutoff in
    # the future that deletes live data.
    enabled: bool = False
    # Raw per-call rows are pruned this many days after they are folded into
    # `tool_usage_daily` (which is kept indefinitely — ~86 rows/day). Purge
    # additionally requires the row to carry its fold stamp
    # (`rolled_at IS NOT NULL`), so an un-aggregated row survives however old
    # it is.
    raw_retention_days: int = Field(default=30, ge=1, le=3650)
    # Bounded in-memory hand-off. `record()` runs on the single event loop,
    # so it may only append; a flusher task does the batched INSERT. On
    # overflow the OLDEST entries are evicted and counted — never silently.
    queue_max: int = Field(default=10_000, ge=1, le=1_000_000)
    flush_interval_secs: int = Field(default=5, ge=1, le=3600)
    flush_batch: int = Field(default=500, ge=1, le=10_000)
    # Rollup/purge cadence. Runs independently of `enabled` so that turning
    # tracking off still drains and prunes what was already collected (the
    # `events` outbox couples publish and purge and grows forever when its
    # transport is unconfigured — do not repeat that).
    rollup_interval_secs: int = Field(default=3600, ge=60, le=86_400)
    # Rows claimed/deleted per maintenance statement. Bounded so catching up
    # after an outage happens in steady chunks instead of one transaction big
    # enough to hit the statement timeout and spike WAL/bloat; a non-zero
    # result keeps the runner looping, so a backlog still drains promptly.
    maintenance_batch: int = Field(default=5_000, ge=1, le=100_000)
    # Hard ceiling on the shutdown drain + worker stop, so this fits inside the
    # container's termination grace (30s on k8s, 15s under the all-in-one
    # supervisor) instead of the 120s a `BackfillRunner` stop may otherwise
    # wait. Whatever is still queued when it expires is reported, not silent.
    shutdown_deadline_secs: float = Field(default=8.0, ge=0.5, le=60.0)


class ExternalGitHostRule(BaseModel):
    """One internal-exception entry for the external-git host allowlist.

    The default host policy for a mirror remote is fail-closed: every resolved
    A/AAAA address must be globally-routable unicast. A rule here is the ONLY
    way to reach a non-global address, and it is a *full pin*, never a host-only
    bypass:

    - ``host``  — the canonical (IDNA/ASCII, lowercased) hostname this rule
      applies to, matched exactly against the parsed URL host. Use ASCII hosts
      (internal names always are).
    - ``cidrs`` — every resolved IP for ``host`` MUST fall inside one of these
      networks. A host-only match never exempts the global-unicast check; the
      CIDR set *is* the constraint. Required (an empty set would allow nothing
      and is a foot-gun).
    - ``ports`` — EVERY port this host may be reached on, enumerated explicitly.
      Unlike a public host (which is implicitly reachable on the scheme's
      standard port), an internal-exception host has NO implicit port — the
      scheme's standard port is NOT auto-allowed. List ``443`` (or
      ``80``) explicitly if the internal host is reached there. At least one port
      is required (an empty set would allow no connection and is a foot-gun).
    """

    model_config = ConfigDict(extra="forbid")

    host: str
    cidrs: list[str] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_rule(self) -> "ExternalGitHostRule":
        host = self.host.strip().lower()
        if not host:
            raise ValueError("external_git host allowlist rule: host must not be empty")
        self.host = host

        if not self.cidrs:
            raise ValueError(
                f"external_git host allowlist rule '{host}': at least one CIDR is "
                "required (a host-only allow is never permitted)"
            )
        for cidr in self.cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as e:
                raise ValueError(
                    f"external_git host allowlist rule '{host}': invalid CIDR '{cidr}': {e}"
                ) from e
        if not self.ports:
            raise ValueError(
                f"external_git host allowlist rule '{host}': at least one port is "
                "required (the scheme's standard port is not auto-allowed for an "
                "internal-exception host)"
            )
        for port in self.ports:
            if isinstance(port, bool) or not (1 <= port <= 65535):
                raise ValueError(
                    f"external_git host allowlist rule '{host}': "
                    f"port {port!r} is out of range 1..65535"
                )
        return self


class Settings(BaseModel):
    # Forbid unknown keys so a typo in app.yaml / secret.yaml fails loudly
    # instead of being silently dropped (pydantic default is 'ignore').
    model_config = ConfigDict(extra="forbid")

    # Database
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "akb"
    db_user: str = "akb"
    db_password: str = ""

    # Main asyncpg pool sizing (app/db/postgres.py). max_size was hardcoded
    # at 20 until 0.9.x; 30 keeps a conservative default — a vanilla PG's
    # max_connections is 100 and the pgvector driver holds its own pool of
    # up to 8 — while giving concurrent write bursts more headroom. The
    # AKB_PG_POOL_MIN_SIZE / AKB_PG_POOL_MAX_SIZE env vars take precedence
    # over these keys (the one deliberate env exception; module docstring).
    pg_pool_min_size: int = 2
    pg_pool_max_size: int = 30

    # `akb_sql` / REST SQL: rows are coerced to JSON dicts on the single event
    # loop. A huge SELECT (`SELECT * FROM generate_series(1, 2e6)`) coerced in
    # one pass blocks the loop for seconds → /livez probe timeout → 503. We
    # coerce in batches of this size, yielding to the loop between batches, so
    # the result is NOT truncated (akb_sql is arbitrary SQL — callers bound
    # their own results with LIMIT) but the loop stays responsive.
    akb_sql_coerce_batch: int = 2000

    # Git storage root (bare repos live here)
    git_storage_path: str = "/data/vaults"

    # Native ledger selection is a dedicated M1 measurement path. Normal
    # deployments retain the legacy bare-Git revision behavior by default.
    document_revision_backend: Literal["bare_git_current", "native_ledger_m1"] = "bare_git_current"
    native_revision_m1_measurement_only: bool = False

    # External-git mirror — network timeouts (seconds) for the poller's
    # three remote-aware git ops. A hanging TCP session otherwise stalls
    # the entire poller task forever since asyncio.to_thread can't cancel
    # running threads.
    external_git_lsremote_timeout: int = 30
    external_git_fetch_timeout: int = 300
    external_git_clone_timeout: int = 900
    # How long a claimed vault stays "in flight" before peer workers can
    # re-claim. Has to exceed the longest realistic initial bootstrap.
    external_git_claim_lookahead_secs: int = 3600

    # ── External-git transport hardening ──────────────────────────────
    # Master kill-switch for the mirror feature. When False the poller must not
    # start, no mirror clone/fetch/ls-remote runs, and create-time validation
    # rejects an external_git block. OSS defaults on; a managed deployment that
    # doesn't expose mirroring sets this False.
    external_git_enabled: bool = True
    # Scheme policy — the SINGLE source of truth. https is always allowed; http
    # is permitted only when this is True (plaintext transport is a downgrade,
    # so it is opt-in). There is deliberately no second "allowed
    # schemes" list that could drift out of sync with this flag.
    external_git_allow_http: bool = False
    # Internal-exception allowlist. Empty (default) = fail-closed: every
    # resolved mirror IP must be globally-routable unicast. Each entry is a full
    # (host, CIDR set, ports) pin — see ExternalGitHostRule above.
    external_git_host_allowlist: list[ExternalGitHostRule] = Field(default_factory=list)
    # Names that are always cluster-internal and denied regardless of the IP
    # they resolve to (a public-looking CIDR behind one of these suffixes is
    # still refused). `localhost` is always denied in addition to these.
    external_git_cluster_dns_suffixes: list[str] = Field(
        default_factory=lambda: [".svc", ".svc.cluster.local", ".cluster.local"]
    )
    # Operator-supplied pod/service CIDRs (e.g. the cluster pod network and the
    # service ClusterIP range). Any resolved address inside one of these is
    # refused BEFORE the host allowlist is consulted, so a pod/service network
    # can never be admitted by an allowlist rule by accident. Empty by
    # default; a managed deployment sets its cluster's ranges here.
    external_git_deny_cidrs: list[str] = Field(default_factory=list)
    # Hard lifetime (seconds) for a single host resolution. getaddrinfo has no
    # per-call timeout and is not cancellable, so the resolver runs in a
    # throwaway worker that is ABANDONED on timeout — the caller never blocks
    # past this deadline.
    external_git_resolver_timeout: float = 5.0
    # Upper bound on concurrent host resolutions (a disposable-resolver pool /
    # back-pressure knob consumed by the poller-side caller in a later stage).
    external_git_max_concurrent_resolutions: int = 4
    # Per-file byte ceiling for a mirrored blob. `git cat-file -s` is checked
    # before materializing; oversize files are skipped deterministically.
    external_git_blob_max_bytes: int = 10 * 1024 * 1024
    # Poll-interval bounds (seconds). The minimum is a code-owned security floor
    # — an operator may RAISE it but validation never allows a value below 60 (a
    # tight loop against an upstream is abuse). The maximum bounds absurd values.
    external_git_poll_interval_min: int = 60
    external_git_poll_interval_max: int = 86400
    # Minimum git version the hermetic mirror runner requires. This is a
    # CODE-OWNED security floor, not a normal tunable: the DNS-pin relies on
    # git's `http.curloptResolve` (documented since git 2.37), so a
    # deployment whose git predates that would silently lose the pin. An operator
    # may RAISE this to demand a newer git, but validation refuses any value BELOW
    # the hard floor 2.37 — the value is floored again at the check site, so it can
    # never be lowered past 2.37. The `external_git_capability` startup check
    # parses the real `git --version` and fast-fails the boot below this.
    external_git_min_git_version: str = "2.37"

    # Embedding — optional since 0.6.2. Unset (empty string) disables the
    # dense leg: `embed_worker` skips the upstream call, every chunk lands
    # in vector_index with `dense IS NULL`, and `hybrid_search` serves
    # results from the BM25 leg alone. Set to an OpenAI-compatible
    # `/v1/embeddings` endpoint to enable hybrid retrieval.
    embed_base_url: str = "http://localhost:8080/v1"
    embed_model: str = "text-embedding-3-small"
    embed_api_key: str = ""
    # Default matches OpenAI text-embedding-3-small. Production deployments
    # using larger models (Qwen3-embed-8b = 4096) override in app.yaml.
    embed_dimensions: int = 1536

    # Model API governance. Standalone deployments retain direct-provider
    # behavior by default. Managed platform deployments opt into the hard
    # profile and declare the exact gateway base URL that every active model
    # route must use; startup rejects a direct-provider escape.
    model_api_governance_mode: Literal[
        "external_metering", "platform_hard"
    ] = "external_metering"
    platform_gateway_base_url: str = ""

    # LLM — optional. Only consumed by metadata_worker (auto-tagging
    # external_git imports). When unset, metadata_worker stays disabled
    # and core CRUD/search keeps working.
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""

    # Reranker — cross-encoder re-scoring of hybrid top-N candidates.
    rerank_enabled: bool = False
    rerank_provider: str = "cohere"
    rerank_model: str = "cohere/rerank-v3.5"
    rerank_base_url: str = ""                  # blank → falls back to llm_base_url
    rerank_api_key: str = ""                   # blank → falls back to llm_api_key
    rerank_prefetch: int = 30
    # RRF k used when fusing the first-stage hybrid rank with cross-encoder
    # rerank rank. 60 is the common RRF default; lower values make top ranks
    # sharper, higher values flatten the contribution curve.
    rerank_fusion_k: int = Field(default=60, ge=1)
    rerank_timeout_seconds: float = 3.0
    # First-stage unique source pool before final `limit` is applied. 0 keeps
    # the legacy behavior (prefetch only when rerank is enabled). Raising this
    # lets rerank-off searches dedup over a wider dense+BM25 candidate set.
    search_prefetch: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_model_api_governance(self) -> "Settings":
        if self.document_revision_backend == "native_ledger_m1":
            if not self.native_revision_m1_measurement_only:
                raise ValueError(
                    "native_ledger_m1 requires native_revision_m1_measurement_only=true"
                )
            if self.db_name != NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME:
                raise ValueError(
                    "native_ledger_m1 requires dedicated measurement database "
                    f"{NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME!r}"
                )

        if self.model_api_governance_mode != "platform_hard":
            return self

        gateway = self.platform_gateway_base_url.strip().rstrip("/")
        if not gateway:
            raise ValueError(
                "platform_gateway_base_url is required in platform_hard mode"
            )

        routes = [
            ("embed_base_url", self.embed_base_url, "embed_api_key", self.embed_api_key),
        ]
        if self.llm_base_url:
            routes.append(
                ("llm_base_url", self.llm_base_url, "llm_api_key", self.llm_api_key)
            )
        if self.rerank_enabled:
            routes.append(
                (
                    "rerank_base_url",
                    self.rerank_base_url or self.llm_base_url,
                    "rerank_api_key",
                    self.rerank_api_key or self.llm_api_key,
                )
            )

        for url_name, url, key_name, key in routes:
            if not url or url.strip().rstrip("/") != gateway:
                raise ValueError(
                    f"{url_name} must exactly match platform_gateway_base_url "
                    "in platform_hard mode"
                )
            if not key.strip():
                raise ValueError(f"{key_name} is required in platform_hard mode")
        return self

    @model_validator(mode="after")
    def validate_external_git(self) -> "Settings":
        if self.external_git_poll_interval_min < 60:
            raise ValueError(
                "external_git_poll_interval_min must be >= 60 (code-owned security floor)"
            )
        if self.external_git_poll_interval_max < self.external_git_poll_interval_min:
            raise ValueError(
                "external_git_poll_interval_max must be >= external_git_poll_interval_min"
            )
        if self.external_git_resolver_timeout <= 0:
            raise ValueError("external_git_resolver_timeout must be > 0")
        if self.external_git_max_concurrent_resolutions < 1:
            raise ValueError("external_git_max_concurrent_resolutions must be >= 1")
        if self.external_git_blob_max_bytes < 1:
            raise ValueError("external_git_blob_max_bytes must be >= 1")
        # min-git-version is a code-owned security FLOOR: parse it and refuse a
        # value below the hard floor (an operator may raise it, never lower it).
        parsed_min = parse_git_version(self.external_git_min_git_version)
        if parsed_min is None:
            raise ValueError(
                "external_git_min_git_version must be a dotted version like '2.37'"
            )
        if parsed_min[:2] < EXTERNAL_GIT_MIN_GIT_VERSION_FLOOR:
            floor = ".".join(str(n) for n in EXTERNAL_GIT_MIN_GIT_VERSION_FLOOR)
            raise ValueError(
                f"external_git_min_git_version must be >= {floor} "
                "(code-owned security floor: the DNS pin needs "
                "http.curloptResolve, documented since git 2.37)"
            )
        for cidr in self.external_git_deny_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as e:
                raise ValueError(
                    f"external_git_deny_cidrs: invalid CIDR '{cidr}': {e}"
                ) from e
        # Coherence: with http disabled, no allowlist rule may open port 80 — an
        # http-only exception is dead config under an https-only policy.
        if not self.external_git_allow_http:
            for rule in self.external_git_host_allowlist:
                if 80 in rule.ports:
                    raise ValueError(
                        f"external_git_allow_http is False but host allowlist rule "
                        f"'{rule.host}' lists port 80"
                    )
        # Normalize cluster suffixes once for case-insensitive suffix matching.
        self.external_git_cluster_dns_suffixes = [
            s.strip().lower() for s in self.external_git_cluster_dns_suffixes if s.strip()
        ]
        return self

    # Hard server-side ceiling on a search/grep `limit`. The MCP tool schema
    # advertises max 50 but that is client-side only — a direct REST call or a
    # non-validating client can pass an arbitrary limit that propagates into the
    # vector-store prefetch (issue #189). Clamped at the service entry so every
    # caller (MCP, REST, internal) is bounded uniformly.
    search_limit_max: int = Field(default=50, ge=1)

    # Push the ACL filter down to VAULT granularity in the vector store (issue
    # #189 Phase 2). When True AND the driver is pgvector AND a search has no
    # doc-level filter (collection/doc_type/tags/source_uris), search filters by
    # the user's accessible vault ids (a small set) instead of materializing
    # every accessible source id (O(corpus)). Correctness-equivalent to the
    # source-id path (AKB's ACL is purely per-vault).
    #
    # Safe to leave ON: search self-gates on `vault_backfill.is_ready()`, so the
    # vault path only activates once every pre-upgrade point has its `vault_id`
    # (the auto-backfill worker fills them on startup). Until then search
    # transparently uses the source-id path — no under-fetch. Set False to opt
    # out of the optimization entirely (byte-identical legacy behavior).
    vault_filter_enabled: bool = True

    # S3-compatible object storage (for vault files)
    s3_endpoint_url: str = ""       # Internal endpoint (server → S3)
    s3_public_url: str = ""         # External endpoint for presigned URLs (client → S3). Falls back to s3_endpoint_url.
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "akb-files"
    s3_region: str = ""
    # boto3/botocore default to a 60 s connect AND 60 s read timeout with NO
    # retries. A stalled MinIO/S3 (network blip, cold bucket, dead endpoint)
    # then blocks the caller for up to 60 s — and several S3 primitives run on
    # the single event loop (public raw-file read, snapshot put/read, HEAD
    # confirm, bucket cold-start), so one stall starves /livez → probe timeout
    # → 503. Bound every S3 call instead: connect ≤ 3 s, read ≤ 10 s, with a
    # short retry for transient blips.
    # NOTE: botocore's `max_attempts` is the RETRY count — total attempts =
    # s3_max_attempts + 1. So the default 2 = 3 total attempts, i.e. a
    # worst-case read hang of 3 × 10 s = 30 s (down from an unbounded 60 s,
    # and now recoverable), and a connect failure bounded to 3 × 3 s = 9 s.
    s3_connect_timeout_secs: float = 3.0
    s3_read_timeout_secs: float = 10.0
    s3_max_attempts: int = 2

    # Auth — jwt_secret must be set (validated at startup in lifecycle.init_storage)
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24

    # Server-side gate for every username/password lifecycle operation.
    # Defaults true so standalone OSS deployments retain registration,
    # login, password change, and administrator/CLI reset behavior. Managed
    # SSO-only tenants set false; this is independent from the SPA-only
    # keycloak_sso_only redirect hint below.
    local_auth_enabled: bool = True

    # Auth — Keycloak OIDC (OPTIONAL external IdP). Disabled by default.
    #
    # When `keycloak_enabled` is false NONE of these are read and AKB uses
    # local username/password + PAT exactly as before — the SSO routes
    # return 404 and zero Keycloak code runs. Enabling adds an SSO login
    # path that, on success, resolves an exact issuer/subject binding. The
    # default open mode may JIT-provision by verified email once and then
    # persists that binding. It issues a normal AKB JWT; the internal user model, PG-native
    # RBAC, and PATs are all unchanged. Keycloak is authentication only —
    # it never drives AKB authorization.
    #
    # Flat `keycloak_*` keys (matching jwt_* / s3_* / embed_*) so the
    # secret can live in secret.yaml without the shallow app.yaml+secret.yaml
    # merge clobbering a nested block. Derived OIDC endpoints are computed
    # properties off `keycloak_issuer` — no .well-known fetch needed.
    #
    # See docs/designs/keycloak-oidc/00-overview.md.
    keycloak_enabled: bool = False
    # When true AND `keycloak_enabled` is also true, the AKB sign-in page
    # bypasses the local username/password form entirely and redirects an
    # unauthenticated browser straight to Keycloak. Use this on
    # deployments where every account is provisioned through SSO and the
    # local form is more confusing than useful.
    #
    # The login page still honours `?local=1` as a presentation escape hatch.
    # It cannot bypass `local_auth_enabled=false`; keycloak_sso_only is never
    # an authorization control.
    #
    # Ignored when `keycloak_enabled = false`.
    keycloak_sso_only: bool = False
    keycloak_server_url: str = ""          # e.g. https://auth.example.com (no /realms suffix)
    # Optional backchannel base URL for server→Keycloak calls (token
    # exchange + JWKS). Defaults to keycloak_server_url. Set this only
    # when the backend reaches Keycloak at a different address than the
    # browser does — split-horizon ingress in prod, or the
    # localhost-vs-container-DNS gap in local docker. The issuer and the
    # browser-facing authorization/logout endpoints always use
    # keycloak_server_url, so the `iss` claim stays the public URL.
    keycloak_internal_url: str = ""
    keycloak_realm: str = "akb"
    keycloak_client_id: str = "akb-web"
    keycloak_client_secret: str = ""       # secret.yaml — blank for public (PKCE) clients
    keycloak_public_client: bool = False   # true → PKCE (no client_secret); false → confidential
    keycloak_verify_ssl: bool = True       # set false only for local self-signed Keycloak
    # Exact identity is issuer/subject and does not require email. During the
    # open-mode JIT/link fallback we REQUIRE the id_token's `email_verified`
    # claim to be true before provisioning / adopting an AKB user — otherwise
    # an IdP that allows unverified or
    # self-asserted emails (open self-registration, social federation)
    # becomes an account-spoofing vector. Set false ONLY for a trusted
    # realm where every account's email is controlled out-of-band.
    keycloak_require_verified_email: bool = True
    # OIDC account admission policy. `open` preserves historical verified-email
    # JIT/link behavior. `invite_only` accepts only an exact pre-provisioned
    # (issuer, subject) binding. `disabled` rejects external login entirely.
    keycloak_enrollment_mode: Literal["open", "invite_only", "disabled"] = "open"
    # Link an SSO login to a pre-existing AKB account that has the SAME
    # email but a different auth_provider (e.g. a local/password account).
    #
    # Default false → such a collision is rejected (no silent identity
    # merge; the OSS-safe default). Set true for a MANAGED deployment where
    # the control plane intentionally pre-provisions an AKB user (+ PAT) for
    # a member and that same person then logs in via SSO — without linking,
    # every pre-provisioned member is locked out of SSO. Linking keeps the
    # existing user_id, so the member's PAT, vault ownership and grants all
    # survive. SAFE ONLY with verified emails: a cross-provider link is
    # refused unless the id_token's email_verified is true, regardless of
    # keycloak_require_verified_email, so a relaxed realm can't be used to
    # take over an existing account by asserting its email.
    keycloak_link_by_email: bool = False
    # Absolute URL Keycloak redirects the browser back to after login.
    # Must point at the AKB backend callback route and be registered as a
    # valid redirect URI on the Keycloak client, e.g.
    #   http://localhost:3000/api/v1/auth/keycloak/callback
    keycloak_redirect_uri: str = ""
    # SPA path the callback bounces the browser to with a one-time code.
    # Relative → resolves against the request origin (same host the user
    # is already on), so it works for both :3000 dev proxy and prod ingress.
    keycloak_post_login_path: str = "/auth/callback"
    # Companion-app post-login origins for cross-origin SSO delegation.
    #
    # Empty (default) → the post-login one-time code is ALWAYS delivered to
    # the same-site keycloak_post_login_path (AKB's own SPA). Behaviour is
    # then 100% identical to before this option existed; no other origin can
    # ever receive the code.
    #
    # When set, a first-party companion app served on a listed origin (e.g.
    # reef at https://reef-<slug>.<domain>) can ride THIS akb's Keycloak
    # client without owning its own client/realm/secret. It starts SSO via
    #   GET /auth/keycloak/login?redirect=<absolute-callback-URL-on-that-origin>
    # and akb delivers the one-time code to that URL (which the companion
    # then exchanges server-side via POST /auth/keycloak/exchange). This is
    # what makes a single per-instance keycloak_post_login_path stop being a
    # bottleneck: akb's own SPA and the companion can both complete SSO,
    # selected per request by the redirect target rather than one global path.
    #
    # Open-redirect protection is preserved: a redirect whose origin is NOT
    # in this list collapses to the safe same-site path. Each entry must be a
    # full origin matched as scheme://host[:port], e.g.
    #   ["https://reef-acme.example.com"]
    keycloak_post_login_allowed_origins: list[str] = Field(default_factory=list)
    # Optional per-companion OIDC client selection. Keys are companion origins
    # (normalized as scheme://host[:port]); values are Keycloak client ids.
    # The mapping is consulted only after the origin passes the allowlist above,
    # so adding a mapping can never grant a new redirect destination. The AKB
    # backend remains the sole OIDC client and uses its backend-only secret for
    # the selected confidential client. Empty preserves the historical single
    # `keycloak_client_id` flow.
    keycloak_companion_client_ids_by_origin: dict[str, str] = Field(
        default_factory=dict
    )
    # One-time exchange-code TTL (seconds). The callback hands the SPA a
    # short-lived opaque code; the SPA trades it for the AKB JWT over a
    # POST so the token never rides in a URL. Keep this small.
    keycloak_exchange_code_ttl_secs: int = 60

    # === MCP OAuth Resource Server (optional, separate from SSO) ===
    # When true, AKB's /mcp endpoint accepts Keycloak-issued access tokens
    # (RS256) in addition to the existing PAT (`akb_*`) and AKB JWT (HS256)
    # paths. Web-hosted LLM clients (claude.ai / ChatGPT Custom Connectors,
    # Claude Code's HTTP transport) discover the authorization server via
    # `/.well-known/oauth-protected-resource` (RFC 9728), register
    # themselves via DCR (RFC 7591) against Keycloak, and obtain an access
    # token with the `akb:vault:read` / `akb:vault:write` scopes.
    #
    # Requires `keycloak_enabled = true` — AKB is a Resource Server only;
    # the Authorization Server (DCR / authorize / consent / token /
    # refresh) is the OIDC IdP. AKB never registers clients or issues
    # OAuth access tokens itself.
    #
    # Disabled (the default) keeps /mcp on PAT-only behaviour
    # bit-for-bit — stdio clients (Claude Desktop, Codex CLI via
    # akb-mcp) are unaffected even when this is left off.
    #
    # See docs/designs/mcp-oauth-dcr/00-overview.md.
    mcp_oauth_enabled: bool = False
    # Audience claim the access token must carry to be usable at /mcp.
    # Defaults to `<public_base_url>/mcp`; override only if you front the
    # MCP endpoint at a separate hostname.
    mcp_oauth_audience: str = ""

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Ingress origin (e.g. https://akb.example.com). REQUIRED at startup —
    # publication responses always carry an absolute ``share_url`` built
    # from this, so MCP clients and agents never have to guess the host.
    # ``lifecycle._validate_required_settings`` fails the app launch if
    # this is empty.
    public_base_url: str = ""

    # Vector store (hybrid dense + BM25). Driver-pluggable.
    #
    # The two `seahorse-*` drivers are intentionally separate:
    #   - `seahorse-cloud` talks to the managed Seahorse Cloud BFF +
    #     per-table data-plane host (zero infrastructure to run).
    #   - `seahorse-db`    talks to a self-hosted SeahorseDB Coral
    #     coordinator (single HTTP URL; you run Coral + Writer +
    #     Reader(s) + Redis + Kafka + sparse-embedding yourself).
    # Pre-0.7.0 there was only one `seahorse` enum value that meant
    # cloud — config migration is `seahorse` → `seahorse-cloud`.
    #
    # BM25-only resilience: only `pgvector` (and the
    # similarly-permissive `qdrant`) tolerate ``embed_base_url``
    # being unset or unreachable — the embed_worker's BM25 fallback
    # writes ``dense=NULL`` rows and ``hybrid_search`` serves them
    # from the sparse leg alone. Both `seahorse-cloud` and
    # `seahorse-db` reject the catalog migration that would allow a
    # NULL embedding column, so on those drivers an embed-API
    # outage stalls the indexing queue. Pick a driver that matches
    # your embedding-availability assumptions.
    vector_store_driver: Literal[
        "qdrant", "pgvector", "seahorse-cloud", "seahorse-db", "seahorse-db-grpc"
    ] = "qdrant"

    # Pgvector driver settings.
    vector_store_dsn: str = ""              # blank = reuse main PG pool
    vector_store_schema: str = "vector_index"
    # `posting` (separate term_id table, indexed lookups) is the
    # production-recommended shape. `arrays` is retained for the bench
    # harness only — slower at scale.
    vector_store_sparse_shape: Literal["posting", "arrays"] = "posting"

    # Qdrant driver settings.
    vector_url: str = ""                    # e.g. http://qdrant:6333
    vector_api_key: str = ""
    vector_collection: str = "chunks"

    # Seahorse Cloud driver settings. Two-plane API: management (BFF)
    # for table lifecycle + per-table data-plane host. The driver
    # discovers the data-plane host from the management lookup; only
    # set the management URL + token + tenant + table identifier.
    seahorse_cloud_management_url: str = "https://console.seahorse.dnotitia.ai/bff"
    seahorse_cloud_token: str = ""          # secret.yaml — Bearer (shsk_...)
    seahorse_cloud_tenant_uuid: str = ""
    seahorse_cloud_table_name: str = ""     # one of (table_name, table_uuid) required
    seahorse_cloud_table_uuid: str = ""
    seahorse_cloud_auto_create: bool = False  # auto-provision the AKB-shaped table

    # SeahorseDB (self-hosted) driver settings. Single-URL entry: the
    # Coral coordinator's HTTP API. Coral handles routing to the
    # underlying Writer/Reader cluster, so the driver does not need
    # to know about individual nodes. `seahorsedb_table_name` is the
    # logical table the AKB chunks go into; the driver auto-creates
    # it with the AKB sparse+dense shape when `seahorsedb_auto_create`
    # is true and the table is absent on startup.
    seahorsedb_coordinator_url: str = "http://localhost:3003"
    seahorsedb_table_name: str = "akb_chunks"
    # SeahorseDB's HNSW supports `l2` and `ip` only (cosine produces
    # "cosinespace" which the HNSW backend rejects at segment build
    # time with `Hnsw index does not support cosinespace`). For
    # cosine-equivalent retrieval, normalize embeddings to unit norm
    # at the caller and use `ip`.
    seahorsedb_distance: Literal["l2", "ip"] = "ip"
    seahorsedb_auto_create: bool = True
    # HTTP timeout for Coral calls. Inserts go through Kafka (async)
    # so the request itself is fast; raise this if upstream Kafka
    # broker latency spikes on your deployment.
    seahorsedb_request_timeout_secs: float = 30.0

    # Indexing worker — claim size per batch. Larger = fewer round-trips
    # to the embedding API but longer per-batch wall clock and bigger
    # transaction footprint. 16 is a safe default at OpenAI-compatible
    # endpoint latencies; tune up to ~64 for fast self-hosted endpoints.
    indexing_batch_size: int = 16
    # Parallel embed_worker tasks draining the same chunks queue. Workers
    # coordinate via FOR UPDATE SKIP LOCKED, so N can be raised until the
    # embedding API's rate limit or PG pool budget caps it. 1 keeps the
    # legacy single-task behavior; 4-8 is the typical production knob.
    indexing_concurrency: int = 1

    # ── Write-lane admission (design/proposal/command-lane-write-path,
    # round-05). Git-committing writes pass a two-stage gate BEFORE
    # acquiring any scarce resource (pool connection, executor thread):
    # per-vault gate (hardwired to 1 — git serializes per vault anyway)
    # then a global lane semaphore. Waiting happens as suspended
    # coroutines, so a hot vault's backlog costs memory only and cannot
    # starve reads or other vaults.
    #
    # Global concurrency for git-committing writes. Also sizes the
    # dedicated commit ThreadPoolExecutor. Bounds the pool connections
    # writes can hold at once — keep well under pg_pool_max_size (30) so
    # reads always have headroom.
    write_lane_concurrency: int = 8
    # Total admission deadline (both gate stages). Exceeded → 429 with
    # Retry-After; the request performed no work. Sized so a normal
    # multi-file ingest burst (commits are ~50-200ms) rides it out,
    # while nothing ever reaches the ingress 120s proxy timeout.
    write_lane_queue_timeout_secs: float = 10.0
    # Global cap on concurrently-waiting writers — a memory/socket
    # backstop, not a policy knob. Normal operation never reaches it;
    # arrivals beyond it fail fast instead of accumulating state.
    write_lane_max_waiters: int = 512
    # kill_after_timeout for git commands on the write path (reset/add/
    # rm/mv/write-tree/commit-tree/update-ref). A wedged git process
    # otherwise pins a lane slot + commit thread + pool connection until
    # the 60s idle-in-transaction reaper fires.
    git_write_timeout_secs: float = 30.0

    # BM25 corpus tuning (driver-neutral; lives in main PG vocab).
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    # How often to recompute `bm25_stats(total_docs, avgdl)` + per-term
    # df from the live chunks corpus. The recompute also runs once at
    # startup, so this controls the steady-state cadence. recompute_stats
    # tokenizes every chunk and gets expensive on large corpora; the
    # refresher skips ticks when the chunk count hasn't moved (see
    # `_should_recompute` in sparse_encoder), so an aggressive interval
    # is cheap when nothing's changing. 6 h matches the slow drift of
    # avgdl/df on a steady-state corpus.
    bm25_recompute_interval_secs: int = 21600

    # Periodic PG-RBAC reconcile cadence. Lifecycle hooks emit role
    # DDL online; this timer is the belt-and-suspenders that catches
    # any silent hook failure (logged + counted in metrics_snapshot
    # but otherwise not auto-recovered). Set to 0 to disable.
    role_sync_reconcile_interval_secs: int = 3600

    # Event stream — optional Redis Streams fanout. PG outbox (`events`
    # table) is always the source of truth; when redis_url is set the
    # events_publisher worker drains the outbox to a Redis Stream so
    # external consumers can subscribe. Empty redis_url disables the
    # publisher entirely (no worker started, events still accumulate
    # in PG and are sweepable).
    redis_url: str = ""                     # e.g. redis://redis:6379/0
    redis_password: str = ""
    redis_event_stream: str = "akb:events"
    redis_stream_maxlen: int = 100_000      # XADD MAXLEN ~ ceiling

    # Audit log — its own nested section so the surface can grow without
    # littering the flat top level. See AuditSettings above.
    audit: AuditSettings = Field(default_factory=AuditSettings)

    # MCP tool-usage analytics — separate sink, separate flag. See
    # ToolUsageSettings above for why it is not folded into `audit`.
    tool_usage: ToolUsageSettings = Field(default_factory=ToolUsageSettings)

    # ── Keycloak OIDC derived endpoints ───────────────────────────
    # All computed off the realm issuer so only server_url + realm are
    # configured. Standard Keycloak OIDC paths under /realms/<realm>.

    @property
    def keycloak_issuer(self) -> str:
        # Public issuer — must equal the `iss` claim Keycloak stamps on
        # tokens (driven by the browser-facing hostname).
        return f"{self.keycloak_server_url.rstrip('/')}/realms/{self.keycloak_realm}"

    @property
    def _keycloak_backchannel_issuer(self) -> str:
        # Internal realm base for server→Keycloak calls; falls back to the
        # public URL when no separate backchannel address is configured.
        base = (self.keycloak_internal_url or self.keycloak_server_url).rstrip("/")
        return f"{base}/realms/{self.keycloak_realm}"

    @property
    def keycloak_authorization_endpoint(self) -> str:
        # Browser-facing → public issuer.
        return f"{self.keycloak_issuer}/protocol/openid-connect/auth"

    @property
    def keycloak_token_endpoint(self) -> str:
        # Server→Keycloak → backchannel issuer.
        return f"{self._keycloak_backchannel_issuer}/protocol/openid-connect/token"

    @property
    def keycloak_jwks_uri(self) -> str:
        # Server→Keycloak → backchannel issuer.
        return f"{self._keycloak_backchannel_issuer}/protocol/openid-connect/certs"

    @property
    def keycloak_end_session_endpoint(self) -> str:
        # Browser-facing → public issuer.
        return f"{self.keycloak_issuer}/protocol/openid-connect/logout"

    @property
    def mcp_oauth_audience_effective(self) -> str:
        """Resolved audience claim required on Keycloak access tokens
        presented at /mcp. Empty string when MCP-OAuth is off."""
        if not self.mcp_oauth_enabled:
            return ""
        if self.mcp_oauth_audience:
            return self.mcp_oauth_audience
        # Default: <public_base_url>/mcp. public_base_url is required at
        # startup (lifecycle validates it), so by the time this property
        # is read in a request path it will be non-empty.
        return f"{self.public_base_url.rstrip('/')}/mcp"

    @property
    def database_url(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def asyncpg_dsn(self) -> str:
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


_CONFIG_CANDIDATES = [Path("./config"), Path("/etc/akb")]


def _find_config_dir() -> Path:
    for candidate in _CONFIG_CANDIDATES:
        if (candidate / "app.yaml").exists():
            return candidate
    searched = ", ".join(str(c.resolve()) for c in _CONFIG_CANDIDATES)
    raise RuntimeError(
        "AKB config not found. Looked for app.yaml in: " + searched + ". "
        "Copy config/app.yaml.example → config/app.yaml and "
        "config/secret.yaml.example → config/secret.yaml, then fill in values."
    )


def _load_settings() -> Settings:
    cfg_dir = _find_config_dir()
    merged: dict = {}
    for name in ("app.yaml", "secret.yaml"):
        path = cfg_dir / name
        if not path.exists():
            continue
        with path.open() as f:
            try:
                data = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                raise RuntimeError(f"Failed to parse {path}: {e}") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"{path} must be a YAML mapping at the top level")
        merged.update(data)
    return Settings(**merged)


settings = _load_settings()
