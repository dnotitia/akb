"""AKB runtime configuration.

Single source: two YAML files merged at import time.

Lookup order (first hit wins):
  1. ./config/app.yaml + ./config/secret.yaml   (CWD-relative; local dev)
  2. /etc/akb/app.yaml + /etc/akb/secret.yaml   (containerised deploys)

The split exists so that `app.yaml` is safe to commit/share (no
secrets) and `secret.yaml` stays out of source control. Both files
are flat YAML mappings using the same keys as the Settings model
below — no environment variables are read.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


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

    # Git storage root (bare repos live here)
    git_storage_path: str = "/data/vaults"

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

    # Auth — jwt_secret must be set (validated at startup in lifecycle.init_storage)
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24

    # Auth — Keycloak OIDC (OPTIONAL external IdP). Disabled by default.
    #
    # When `keycloak_enabled` is false NONE of these are read and AKB uses
    # local username/password + PAT exactly as before — the SSO routes
    # return 404 and zero Keycloak code runs. Enabling adds an SSO login
    # path that, on success, JIT-provisions an AKB user (keyed by email)
    # and issues a normal AKB JWT; the internal user model, PG-native
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
    # Identity is keyed on the verified email. By default we REQUIRE the
    # id_token's `email_verified` claim to be true before provisioning /
    # adopting an AKB user — otherwise an IdP that allows unverified or
    # self-asserted emails (open self-registration, social federation)
    # becomes an account-spoofing vector. Set false ONLY for a trusted
    # realm where every account's email is controlled out-of-band.
    keycloak_require_verified_email: bool = True
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
