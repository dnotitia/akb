"""Vector store factory — selects the driver from settings.

Driver matrix:

| `vector_store_driver` | required settings                                   |
| --------------------- | --------------------------------------------------- |
| `qdrant`              | `vector_url` (+ optional `vector_api_key`)          |
| `pgvector`            | `vector_store_dsn` blank reuses main PG             |
| `seahorse`            | `seahorse_token` + `seahorse_tenant_uuid` + one of  |
|                       | `seahorse_table_name` / `seahorse_table_uuid`       |

The driver and sparse-shape values are validated at config load
(pydantic Literals); the factory only needs to dispatch.
"""

from __future__ import annotations

from app.config import settings

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
        from app.db.postgres import get_pool

        _singleton = PgvectorStore(
            dsn=settings.vector_store_dsn or None,
            schema=settings.vector_store_schema,
            dense_dim=settings.embed_dimensions,
            sparse_shape=settings.vector_store_sparse_shape,
            get_main_pool=get_pool,
        )
    elif driver == "seahorse":
        from .seahorse import SeahorseStore
        if not settings.seahorse_token:
            raise RuntimeError(
                "vector_store_driver=seahorse requires seahorse_token (Bearer) "
                "in secret.yaml."
            )
        if not settings.seahorse_tenant_uuid:
            raise RuntimeError(
                "vector_store_driver=seahorse requires seahorse_tenant_uuid."
            )
        if not (settings.seahorse_table_name or settings.seahorse_table_uuid):
            raise RuntimeError(
                "vector_store_driver=seahorse requires either "
                "seahorse_table_name or seahorse_table_uuid."
            )
        _singleton = SeahorseStore(
            management_url=settings.seahorse_management_url,
            token=settings.seahorse_token,
            tenant_uuid=settings.seahorse_tenant_uuid,
            table_name=settings.seahorse_table_name or None,
            table_uuid=settings.seahorse_table_uuid or None,
            dense_dim=settings.embed_dimensions,
            auto_create=settings.seahorse_auto_create,
        )
    else:
        # Unreachable given the Literal at config load; kept for safety
        # against future config refactors.
        raise RuntimeError(f"Unknown vector_store_driver: {driver!r}")

    return _singleton


def reset_singleton_for_tests() -> None:
    """Test-only helper. Clears the singleton so subsequent
    get_vector_store() calls rebuild from current settings."""
    global _singleton
    _singleton = None
