"""Vector store factory — selects the driver from settings.

Driver matrix:

| `vector_store_driver` | required settings                                          |
| --------------------- | ---------------------------------------------------------- |
| `qdrant`              | `vector_url` (+ optional `vector_api_key`)                 |
| `pgvector`            | `vector_store_dsn` blank reuses main PG                    |
| `seahorse-cloud`      | `seahorse_cloud_token` + `seahorse_cloud_tenant_uuid` +    |
|                       | one of `seahorse_cloud_table_name` / `..._table_uuid`      |
| `seahorse-db`         | `seahorsedb_coordinator_url` (single Coral HTTP URL)       |

The driver and sparse-shape values are validated at config load
(pydantic Literals); the factory only needs to dispatch. The one exception is
`vector_store_sparse_shape: auto`, a property of the database rather than of
the configuration: `decide_sparse_shape_for_settings` settles it at startup,
before the store is built, and building one from an undecided `auto` is refused.
"""

from __future__ import annotations

from app.config import Settings, settings
from app.services.sparse_shapes import SparseShapeDecision

from .base import VectorStore


_singleton: VectorStore | None = None


def get_vector_store() -> VectorStore:
    """Return the shared VectorStore. Raises if config is incomplete."""
    global _singleton
    if _singleton is not None:
        return _singleton

    driver = settings.vector_store_driver

    if driver == "qdrant":
        from .qdrant import QdrantStore
        if not settings.vector_url:
            raise RuntimeError(
                "vector_store_driver=qdrant requires vector_url to be set."
            )
        _singleton = QdrantStore(
            url=settings.vector_url,
            api_key=settings.vector_api_key or None,
            collection=settings.vector_collection,
            dense_dim=settings.embed_dimensions,
        )
    elif driver == "pgvector":
        from .pgvector import PgvectorStore

        shape = settings.effective_sparse_shape
        # While anything reads posting's statistics — `required`, or `auto`
        # over a database that has a `posting` table — the posting rows follow
        # every write, or the way back decays from the day of the flip
        # (akb#615). A new vchord database has no such table and no reader.
        posting_weights = None
        if shape == "vchord" and settings.bm25_external_stats_consumers:
            from app.services.sparse_encoder import saturate_for_posting

            posting_weights = saturate_for_posting

        from app.db.postgres import get_pool

        _singleton = PgvectorStore(
            dsn=settings.vector_store_dsn or None,
            schema=settings.vector_store_schema,
            dense_dim=settings.embed_dimensions,
            sparse_shape=shape,
            get_main_pool=get_pool,
            posting_weights=posting_weights,
        )
    elif driver == "seahorse-cloud":
        from .seahorse_cloud import SeahorseCloudStore
        if not settings.seahorse_cloud_token:
            raise RuntimeError(
                "vector_store_driver=seahorse-cloud requires "
                "seahorse_cloud_token (Bearer) in secret.yaml."
            )
        if not settings.seahorse_cloud_tenant_uuid:
            raise RuntimeError(
                "vector_store_driver=seahorse-cloud requires "
                "seahorse_cloud_tenant_uuid."
            )
        if not (
            settings.seahorse_cloud_table_name
            or settings.seahorse_cloud_table_uuid
        ):
            raise RuntimeError(
                "vector_store_driver=seahorse-cloud requires either "
                "seahorse_cloud_table_name or seahorse_cloud_table_uuid."
            )
        _singleton = SeahorseCloudStore(
            management_url=settings.seahorse_cloud_management_url,
            token=settings.seahorse_cloud_token,
            tenant_uuid=settings.seahorse_cloud_tenant_uuid,
            table_name=settings.seahorse_cloud_table_name or None,
            table_uuid=settings.seahorse_cloud_table_uuid or None,
            dense_dim=settings.embed_dimensions,
            auto_create=settings.seahorse_cloud_auto_create,
        )
    elif driver == "seahorse-db":
        from .seahorse_db import SeahorseDbStore
        if not settings.seahorsedb_coordinator_url:
            raise RuntimeError(
                "vector_store_driver=seahorse-db requires "
                "seahorsedb_coordinator_url (Coral HTTP API)."
            )
        _singleton = SeahorseDbStore(
            coordinator_url=settings.seahorsedb_coordinator_url,
            table_name=settings.seahorsedb_table_name,
            dense_dim=settings.embed_dimensions,
            distance=settings.seahorsedb_distance,
            auto_create=settings.seahorsedb_auto_create,
            timeout=settings.seahorsedb_request_timeout_secs,
        )
    elif driver == "seahorse-db-grpc":
        # gRPC sibling of `seahorse-db`. Same Coral coordinator on the
        # same port (Coral merges REST + gRPC into one listener), so we
        # reuse the seahorsedb_* settings. Opt-in only — `seahorse-db`
        # is the documented production path.
        from .seahorse_db_grpc import SeahorseDbGrpcStore
        if not settings.seahorsedb_coordinator_url:
            raise RuntimeError(
                "vector_store_driver=seahorse-db-grpc requires "
                "seahorsedb_coordinator_url (Coral host:port — the same "
                "endpoint the REST driver uses; scheme is stripped)."
            )
        _singleton = SeahorseDbGrpcStore(
            coordinator_url=settings.seahorsedb_coordinator_url,
            table_name=settings.seahorsedb_table_name,
            dense_dim=settings.embed_dimensions,
            distance=settings.seahorsedb_distance,
            auto_create=settings.seahorsedb_auto_create,
            timeout=settings.seahorsedb_request_timeout_secs,
        )
    else:
        # Unreachable given the Literal at config load; kept for safety
        # against future config refactors.
        raise RuntimeError(f"Unknown vector_store_driver: {driver!r}")

    return _singleton


async def decide_sparse_shape_for_settings(
    configured: Settings | None = None,
) -> SparseShapeDecision | None:
    """Settle the sparse shape for this database before the store is built.

    pgvector only; any other driver returns None. Reads the vector database:
    `vector_store_dsn` when set, the main pool otherwise. Also learns whether a
    `posting` table exists, which the external-statistics policy needs even
    when the shape is configured. Safe to call again; it re-reads."""
    target = configured if configured is not None else settings
    if target.vector_store_driver != "pgvector":
        return None
    from .sparse_shape_state import decide_sparse_shape

    if target.vector_store_dsn:
        import asyncpg

        conn = await asyncpg.connect(target.vector_store_dsn)
        try:
            decision = await decide_sparse_shape(
                conn, schema=target.vector_store_schema, configured=target.vector_store_sparse_shape
            )
        finally:
            await conn.close()
    else:
        from app.db.postgres import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            decision = await decide_sparse_shape(
                conn, schema=target.vector_store_schema, configured=target.vector_store_sparse_shape
            )
    target.apply_sparse_shape_decision(decision)
    return decision


def reset_singleton_for_tests() -> None:
    """Test-only helper. Clears the singleton so subsequent
    get_vector_store() calls rebuild from current settings."""
    global _singleton
    _singleton = None
