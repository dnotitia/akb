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
from pydantic import BaseModel, ConfigDict


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

    # Embedding — required for indexing/search
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
    rerank_timeout_seconds: float = 3.0

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

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Unset disables absolute URLs in publication responses (MCP clients only
    # get the relative /p/{slug} path). Set to the Ingress domain in prod.
    public_base_url: str = ""

    # Vector store (hybrid dense + BM25). Driver-pluggable.
    vector_store_driver: Literal["qdrant", "pgvector", "seahorse"] = "qdrant"

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
    seahorse_management_url: str = "https://console.seahorse.dnotitia.ai/bff"
    seahorse_token: str = ""                # secret.yaml — Bearer (shsk_...)
    seahorse_tenant_uuid: str = ""
    seahorse_table_name: str = ""           # one of (table_name, table_uuid) required
    seahorse_table_uuid: str = ""
    seahorse_auto_create: bool = False      # auto-provision the AKB-shaped table

    # Indexing worker — claim size per batch. Larger = fewer round-trips
    # to the embedding API but longer per-batch wall clock and bigger
    # transaction footprint. 16 is a safe default at OpenAI-compatible
    # endpoint latencies; tune up to ~64 for fast self-hosted endpoints.
    indexing_batch_size: int = 16

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
